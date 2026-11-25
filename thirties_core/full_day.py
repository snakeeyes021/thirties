"""Everything on today has a time; a day that is really full moves its lowest item on.

Every item on today's list gets a time on today's plan, except what the user asked to keep in the "If there's
time" section (if_time.py): the model may name a time, otherwise Python finds the room (the first free stretch from
now that fits, or alongside one flexible item, if_time.place). When the day is truly full even so, Python
moves the lowest-priority item on to the next day that has room and isn't a holiday (holidays.next_open_day), and
the answer says so in one plain line:
"No room today: <item> goes to <day>."

Which item moves is decided by the items' own facts, never their wording:

- never one the user asked for this turn or placed at a time themselves (nor, while anything else can go, a line
  the user has on the Today note: they want it today), never a calendar event, a firm or fixed
  item, a routine or one of its parts, work or sleep, and never something due today or overdue (due_today.py);
- among the rest, the one with the latest deadline (none at all counts as latest), then the lowest urgency, then
  the longest horizon. On a tie the item still to be placed goes on, so the plan already made isn't disturbed.

A planned item that moves is a normal move (update_event's own move to another day, at the first free stretch
there), so it can be moved back the usual way. An item that had no time yet waits for that day (not_today.set_aside:
the open pool with that day as its window) and leaves today's list.

Joplin is the truth: every list lives in the task backend, the day's list included, and the user edits any of it
by hand. A line on the Today note with no plan item (outside If there's time) means the user wants it today: when
today's plan is built (``settle_loose``, the morning) and after each Joplin sync once the day is built
(``mirror_today``), it gets its time, with one line saying so, or the day being full, the lowest item moves on
(written to Joplin too). It is never shown without a time and never taken off for want of a plan item. What waited
for today (a later day's ask, in the open pool with this day as its window) gets its time the same way. A line the
user deletes from Today by hand takes its item off today's plan (``unlisted_by_hand``).

Example (invented): at the pottery studio the afternoon is booked solid; the user adds "Wedge the new clay" for
today. Nothing is free even doubled up, so "Sort the glaze samples" (no deadline, urgency whenever) goes to
Thursday, the next day with room, and "Wedge the new clay" takes its time at 2:40 PM.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_URGENCY = {"now": 3, "soon": 2, "": 1, "whenever": 1, "someday": 0}
_HORIZON = {"day": 4, "week": 3, "": 2, "month": 2, "year": 1, "someday": 0}
_FAR = 10 ** 4  # no deadline: as far off as a deadline can be


def moved_line(title: str, day: date, today: date) -> str:
    """The one line the answer gives for an item moved off a full day."""
    from thirties_core.not_today import day_words
    return f"No room today: {title} goes to {day_words(day, today)}."


def _task_of(assistant: Any, title: str, task_id: Optional[str] = None) -> Any:
    tasks = list(getattr(assistant.s, "tasks", []) or [])
    found = next((t for t in tasks if task_id and t.id == task_id), None)
    if found is not None:
        return found
    try:
        from thirties_core.not_today import home_task
        return home_task(assistant, title, task_id) or assistant._exact_task(title)
    except Exception:
        return None


def importance(assistant: Any, title: str, task: Any = None) -> Tuple[int, int, int]:
    """How much the item matters today, from its own facts (higher matters more): how soon it's due, how urgent
    it is (a stated can't-wait counts as now), and how short its horizon is."""
    today = assistant.pt.today()
    due = (getattr(task, "due", "") or "")[:10]
    try:
        days = (date.fromisoformat(due) - today).days if due else _FAR
    except ValueError:
        days = _FAR
    urgency = _URGENCY.get(getattr(task, "urgency", "") or "", 1)
    try:
        from thirties_core.backoff import stated_cant_wait
        if assistant._db is not None and stated_cant_wait(assistant._db, task, title):
            urgency = _URGENCY["now"]
    except Exception:
        pass
    horizon = _HORIZON.get(getattr(task, "horizon", "") or "", 2)
    return -days, urgency, horizon


def asked_now(pt: Any, title: str, others: Iterable[str]) -> bool:
    """The user's words this turn name the item (nobody talking: no one asked for anything)."""
    said = getattr(pt, "user_message", None)
    if not said:
        return False
    from thirties_core.item_facts import mentions
    return mentions(said, title, list(others))


def _movable(assistant: Any, span: Any, titles: List[str]) -> bool:
    """A planned item Python may move off a full day by itself."""
    from thirties_core import kinds
    from thirties_core.due_today import must_stay
    from thirties_core.models import SpanCategory, SpanStatus
    pt = assistant.pt
    if span.status != SpanStatus.PLANNED or span.start_dt < pt._now() or span.shown_only:
        return False
    if span.is_envelope or span.is_firm or span.source in ("calendar", "routine"):
        return False
    if span.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK):
        return False
    if span.given_start is not None or span.given_end is not None:  # a time the user gave it
        return False
    try:
        if pt._is_fixed(span) or pt._is_routine(span):
            return False
    except Exception:
        return False
    low = span.title.strip().lower()
    try:
        if low in assistant.routines.daily_titles() or assistant._routine_of(span.title) is not None:
            return False
    except Exception:
        pass
    if kinds.is_meal(span.title) or must_stay(pt, span.title, span.task_id):
        return False
    return not asked_now(pt, span.title, titles)


def lowest(assistant: Any, title: str, task: Any, minutes: int,
           protected: bool) -> Optional[Any]:
    """What goes on when the day is full: a planned span, the item being placed itself (``title``), or None when
    nothing may move. ``protected``: the item being placed was asked for by the user (never the one picked)."""
    pt = assistant.pt
    titles = [s.title for s in pt.day_plan.spans if not s.is_envelope]
    spans = [s for s in pt.day_plan.spans if _movable(assistant, s, titles)
             and (s.end_dt - s.start_dt) >= timedelta(minutes=min(minutes, 30))]
    ranked: List[Tuple[Tuple[int, int, int], int, Any]] = []
    for s in spans:
        ranked.append((importance(assistant, s.title, _task_of(assistant, s.title, s.task_id)),
                       -int(s.start_dt.timestamp()), s))
    if not protected:
        from thirties_core.due_today import must_stay
        if not must_stay(pt, title, getattr(task, "id", None)):
            ranked.append((importance(assistant, title, task), -(10 ** 12), title))  # ties: this one goes
    if not ranked:
        return None
    return min(ranked, key=lambda r: (r[0], r[1]))[2]


def _next_day(assistant: Any, title: str, minutes: int) -> date:
    from thirties_core.holidays import next_open_day
    return next_open_day(assistant.pt, assistant.pt.today(), title, minutes)


def move_span(assistant: Any, span: Any) -> Optional[str]:
    """A planned item moved off today to the next day with room that isn't a holiday, at the first free stretch
    there (a normal move: it can be moved back). The answer's line, or None when it couldn't move."""
    from thirties_core.clock import format_clock
    from thirties_core.if_time import _waking_end
    pt = assistant.pt
    minutes = max(30, span.duration_minutes)
    day = _next_day(assistant, span.title, minutes)
    try:
        plan = pt.plan_for(day)
        need = timedelta(minutes=minutes)
        start = next((a for a, b in pt.open_windows_for(plan) if min(b, _waking_end(plan)) - a >= need), None)
        pt._move_to_day(span, day.isoformat(), format_clock(start) if start is not None else None,
                        None, None, None)
    except Exception as e:
        logger.warning("Couldn't move %s off a full day: %s", span.title, e)
        return None
    try:
        pt._save_plan_after_change()
    except Exception:
        pass
    return moved_line(span.title, day, pt.today())


def take_line_off(assistant: Any, line: str) -> bool:
    """Python's own fix to the Today note: the line goes from the Today note only (never a home list, never a
    routine note), and its link with it. True when the removal was sent."""
    from thirties_core.today import TodayLinks
    note, writer = assistant._today_note(), assistant._writer()
    if note is None or writer is None or not getattr(writer, "allowed", True):
        return False
    status, _ = writer.submit({"op": "remove", "note_id": note["id"], "title": line, "state": "open",
                               "_moved_on": True})
    if status not in ("done", "queued"):
        return False
    assistant.s.tasks = [x for x in assistant.s.tasks if not (x.parent_note_id == note["id"] and x.title == line)]
    if assistant._db is not None:
        TodayLinks(assistant._db).unlink(line)
    return True


def move_unplaced(assistant: Any, title: str, task: Any, minutes: int) -> str:
    """An item with no time yet goes on to the next day with room that isn't a holiday: it waits for that day
    (not_today.set_aside), and its line leaves today's list. The answer's line."""
    from thirties_core.not_today import set_aside
    day = _next_day(assistant, title, minutes)
    set_aside(assistant, title, task, day, False)
    try:
        from thirties_core.leave_today import top_line
        line = top_line(assistant.pt, title, getattr(task, "id", None))
        if line is not None:
            take_line_off(assistant, line)
    except Exception as e:
        logger.warning("Couldn't take %s off today's list: %s", title, e)
    return moved_line(title, day, assistant.pt.today())


def place_or_move(assistant: Any, title: str, task: Any = None,
                  protected: bool = False) -> Tuple[Optional[datetime], bool, List[str]]:
    """Give ``title`` its time today; when the day is full even doubled up, move the lowest item on (this one
    included unless ``protected``). Returns (its start or None, whether doubled up, the lines saying what moved).
    When nothing else may give way, this one goes on to the next day with room even if asked for: it always has
    a time or a day."""
    from thirties_core import if_time
    from thirties_core.day_proposal import usual_minutes
    said: List[str] = []
    for _ in range(4):  # each pass frees one item's time; a few are plenty
        spot, doubled = if_time.place(assistant, title, task)
        if spot is not None:
            return spot, doubled, said
        minutes = usual_minutes(assistant, task, title)
        # --- a refused write is not a full day: with room left, nothing moves (it was refused for its own reason) ---
        if if_time.first_slot(assistant.pt, minutes) is not None or if_time.doubled_slot(assistant.pt, minutes) is not None:
            logger.warning("%r wasn't put on today's plan though there's room; nothing was moved for it", title)
            return None, False, said
        # --- end refused ---
        pick = lowest(assistant, title, task, minutes, protected)
        if pick is None:
            break
        if isinstance(pick, str):
            said.append(move_unplaced(assistant, title, task, minutes))
            return None, False, said
        line = move_span(assistant, pick)
        if line is None:
            break
        said.append(line)
    # nothing else may give way: this one goes on too, asked for or not, so it still has a day (never limbo)
    said.append(move_unplaced(assistant, title, task, usual_minutes(assistant, task, title)))
    return None, False, said


# --- Joplin is the truth: a line on the Today note means the user wants it today ---

def _names(assistant: Any, line: str, task: Any = None) -> Tuple[set, set]:
    """The ways a Today line can be named on the plan (its wording, short or whole, and its home item's), and the
    ids that stand for it (its home item, and its own line on the Today note)."""
    from thirties_core.titles import title_of
    names = {line.strip().lower(), title_of(line).strip().lower()}
    ids = set()
    if task is not None:
        ids.add(task.id)
        names |= {task.title.strip().lower(), title_of(task.title).strip().lower()}
    try:
        from thirties_core.today import TodayLinks
        link = TodayLinks(assistant._db).home_of(line)
    except Exception:
        link = None
    if link:
        ids.add(link.get("home_id") or "")
        names |= {str(link.get("home_title") or "").strip().lower(),
                  title_of(str(link.get("home_title") or "")).strip().lower()}
    note = None
    try:
        note = assistant._today_note()
    except Exception:
        pass
    tasks = list(getattr(assistant.s, "tasks", []) or [])
    if note is not None:
        ids |= {t.id for t in tasks
                if t.parent_note_id == note.get("id") and t.title.strip().lower() == line.strip().lower()}
    # --- one title, several ids: every item worded the same stands for the line (titles.py: raw ids too) ---
    from thirties_core.titles import is_raw_id, resolve_raw
    if is_raw_id(line):
        real = resolve_raw(assistant._db, line, tasks)
        if real and not is_raw_id(real):
            names |= {real.strip().lower(), title_of(real).strip().lower()}
        ids.add(line.strip())
    names = {n for n in names if n}
    ids |= {t.id for t in tasks if t.title.strip().lower() in names or title_of(t.title).strip().lower() in names}
    # --- end one title, several ids ---
    return names, {i for i in ids if i}


def stands_for(assistant: Any, line: str, task: Any = None) -> Any:
    """The item on today's plan that stands for this Today line, whatever its state: planned (past or not, unmarked
    included: unknown, never re-placed), done, skipped. None when nothing on today's plan stands for it."""
    from thirties_core.titles import title_of
    names, ids = _names(assistant, line, task)
    for s in assistant.pt.day_plan.spans:
        if s.is_envelope:
            continue
        if (s.task_id and s.task_id in ids) or s.title.strip().lower() in names \
                or title_of(s.title).strip().lower() in names:
            return s
    return None


def had_item_today(assistant: Any, line: str, task: Any = None) -> bool:
    """Its item was on today's plan earlier and left it (removed, cleared, moved off, skipped or dropped), by the
    plan's history or Before today's records. Such a line is no hand add: it is never given a time."""
    pt = assistant.pt
    db = assistant._db
    day = pt.today().isoformat()
    names, _ = _names(assistant, line, task)
    try:
        from thirties_core.plan_log import changes_for
        if any(changes_for(db, title=n, plan_date=day) for n in names):
            return True
    except Exception:
        pass
    try:
        from thirties_core.carry import OpenPool
        pool = OpenPool(db).all()
        for n in names:
            rec = pool.get(n) or {}
            if day in (rec.get("set_aside"), rec.get("dropped_on")):
                return True
    except Exception:
        pass
    return False

def loose_today(assistant: Any) -> List[str]:
    """Lines on today's list that no planned item stands for (day_lists.pencilled_in, which takes in any left
    under an older note's untimed heading). Never shown: each gets its time, or moves on when the day is full."""
    from thirties_core.day_lists import pencilled_in
    try:
        return list(pencilled_in(assistant))
    except Exception:
        return []


def _ahead_rows(assistant: Any) -> List[Any]:
    """What was kept for today ahead of time (day_lists.AheadLists, with the open pool's window on this day)."""
    from thirties_core.day_lists import AheadLists
    try:
        return AheadLists(assistant._db).for_day(assistant.pt.today())
    except Exception:
        return []


def _place_ahead_rows(assistant: Any) -> List[str]:
    """Each thing kept for today ahead of time gets its time on today's plan (a day the user named for it is
    theirs: never the one moved on while anything else can go). One Python set aside by itself that is resting
    after missed days (backoff.py) isn't put on: it waits in the open pool. The lines saying what moved."""
    from thirties_core.backoff import MissStreaks, stated_cant_wait
    from thirties_core.carry import OpenPool
    from thirties_core.day_lists import AheadLists
    pt = assistant.pt
    store = AheadLists(assistant._db)
    pool = OpenPool(assistant._db).all()
    streaks = MissStreaks(assistant._db)
    lines: List[str] = []
    for row in _ahead_rows(assistant):
        title = row["title"]
        task = _task_of(assistant, title, row.get("home_id") or None)
        rec = pool.get(title.strip().lower()) or {}
        theirs = bool(rec.get("named_day")) or not rec.get("set_aside")
        if stands_for(assistant, title, task) is not None:  # already on today's plan, whatever its state
            store.set_state(pt.today(), title, "listed")
            continue
        if not theirs and streaks.held(pt.today(), title, getattr(task, "id", "") or "",
                                       due=getattr(task, "due", "") or "",
                                       urgent=stated_cant_wait(assistant._db, task, title)):
            store.set_state(pt.today(), title, "resting")
            continue
        try:
            spot, _, said = place_or_move(assistant, title, task, protected=theirs)
        except Exception as e:
            logger.warning("Couldn't give %s its time today: %s", title, e)
            continue
        lines += said
        store.set_state(pt.today(), title, "listed" if spot is not None else "moved")
    return lines


def _place_lines(assistant: Any, told: List[str]) -> List[str]:
    """Every line the user added to the Today note, one that never had a plan item today: the user wants it
    today, so it gets its time (one line saying so, once), or the day being full, the lowest item moves on.

    - A line something on today's plan stands for, in any state (an unmarked past one included: unknown), is
      left alone: never re-placed, never moved.
    - A line whose item left today's plan earlier today (removed, cleared, moved off, skipped, dropped) is no
      hand add: it is taken off the Today note quietly (the user took the item off), logged, never told.
    - One resting after missed days (backoff.py) waits for the leftovers question.
    - A line that is only an item's id (an old write put one there) is matched by its item's title first, and is
      never written to; one whose id can't be resolved is left as it is, logged, never placed or shown."""
    from thirties_core import if_time
    from thirties_core.backoff import MissStreaks, stated_cant_wait
    from thirties_core.clock import format_clock, thirty_of
    from thirties_core.titles import is_raw_id, resolve_raw
    streaks = MissStreaks(assistant._db)
    lines: List[str] = []
    for title in loose_today(assistant):
        # --- a line that is only an item's id (an old write put one there): matched by its item's title, never
        # written to; one that can't be resolved is left as it is, logged, never placed or shown ---
        id_line = is_raw_id(title)
        if id_line:
            real = resolve_raw(assistant._db, title, getattr(assistant.s, "tasks", []) or [])
            if not real or is_raw_id(real):
                logger.info("Today sync: id-only line with no known item, untouched")
                continue
            if stands_for(assistant, real) is not None or had_item_today(assistant, real):
                logger.info("Today sync: id-only line resolves to %r, which today's plan already holds or held; "
                            "untouched", real)
                continue
            title = real
        # --- end a line that is only an id ---
        task = _task_of(assistant, title)
        if stands_for(assistant, title, task) is not None:
            continue
        if had_item_today(assistant, title, task):
            if id_line:
                continue  # never written to
            if take_line_off(assistant, title):
                logger.info("Today note: %r left today's plan earlier today; its leftover line was taken off", title)
            continue
        if streaks.held(assistant.pt.today(), title, getattr(task, "id", "") or "", due=getattr(task, "due", "") or "",
                        urgent=stated_cant_wait(assistant._db, task, title)):
            continue
        try:
            spot, doubled, said = place_or_move(assistant, title, task, protected=True)
        except Exception as e:
            logger.warning("Couldn't give %s a time today: %s", title, e)
            continue
        lines += said
        if spot is None:
            continue
        logger.info("Today note: %r had no plan item; placed at %s", title, spot.isoformat())
        try:
            if not id_line:
                if_time.planned_out(assistant, title)  # off an older note's untimed heading, if it was there
        except Exception:
            pass
        if title not in told:
            told.append(title)
            lines.append(f"{title}, on today's list, is on today's plan at {format_clock(thirty_of(spot))}"
                         + (", alongside another item." if doubled else "."))
    return lines


MIRROR_TOLD_KEY = "today_mirror_told"  # {day: [titles]}: each line given a time is told about once


def _told(db: Any, day: str) -> List[str]:
    import json
    try:
        return json.loads(db.get_sync_meta(MIRROR_TOLD_KEY) or "{}").get(day, [])
    except ValueError:
        return []


def _save_told(db: Any, day: str, told: List[str]) -> None:
    import json
    db.set_sync_meta(MIRROR_TOLD_KEY, json.dumps({day: told}))


def settle_loose(assistant: Any) -> List[str]:
    """Today's plan being built (the morning, after the routines): what was kept for today ahead of time and every
    line on the Today note with no plan item get a time, or (the day full) the lowest item moves on. Returns the
    lines for the user: what got a time, and what moved."""
    pt = assistant.pt
    db = assistant._db
    if db is None or not pt.is_current(pt.day_plan):
        return []
    day = pt.today().isoformat()
    told = _told(db, day)
    lines = _place_ahead_rows(assistant) + _place_lines(assistant, told)
    _save_told(db, day, told)
    return lines


def mirror_today(assistant: Any) -> List[str]:
    """After a Joplin sync, once the day is built: a line on the Today note with no plan item gets its time (or
    moves on, the day full), with one line saying so. Nothing before the day is built."""
    from thirties_core import morning
    pt = assistant.pt
    db = assistant._db
    if db is None or not pt.is_current(pt.day_plan):
        return []
    if not morning.is_done(db, pt.today()):
        return []  # nothing is planned until the user is up and the day is built (morning.py)
    return settle_loose(assistant)


SEEN_KEY = "today_note_seen"  # {"note": id, "day": iso, "lines": [lowercased open and ticked lines]}


def _writes_pending(assistant: Any, note_id: str) -> bool:
    """A write of Thirties' own to this note is queued in the outbox or being applied right now."""
    import json
    from thirties_core.joplin_writeback import writing_to
    if writing_to(note_id):
        return True
    try:
        rows = assistant._db.outbox_pending()
    except Exception:
        return True  # can't tell: treat it as busy, and wait
    for row in rows:
        try:
            op = json.loads(row.get("op") or "{}")
        except ValueError:
            continue
        if note_id in (str(op.get("note_id") or ""), str(op.get("to_note_id") or "")):
            return True
    return False


def hand_deleted(assistant: Any, note: Any, lines_now: Any, gone: List[Tuple[Any, str]]) -> List[Any]:
    """Of the planned items whose Today line is missing (``gone``: (span, its line, lowercased)), those whose line
    the user deleted by hand: no write of Thirties' own to the note is queued or in flight, and the line was there
    in the last note text Thirties saw itself. Anything else waits for the next sync. The note as seen now is kept
    for next time (only while no write of Thirties' own is pending, so a line it is about to write or remove never
    counts)."""
    import json
    db = assistant._db
    if db is None or not note:
        return []
    note_id = str(note.get("id") or "")
    day = assistant.pt.today().isoformat()
    if _writes_pending(assistant, note_id):
        return []
    try:
        seen = json.loads(db.get_sync_meta(SEEN_KEY) or "{}")
    except ValueError:
        seen = {}
    before = set(seen.get("lines") or []) if seen.get("note") == note_id and seen.get("day") == day else set()
    db.set_sync_meta(SEEN_KEY, json.dumps({"note": note_id, "day": day,
                                           "lines": sorted(str(t).lower() for t in lines_now)}))
    return [span for span, line in gone if line and str(line).lower() in before]


def unlisted_by_hand(assistant: Any, spans: List[Any]) -> List[str]:
    """Lines the user deleted from the Today note by hand: their items come off today's plan (Joplin wins). Their
    home lists are never touched. One line each, for the user."""
    pt = assistant.pt
    out: List[str] = []
    for gone in spans:
        span = next((s for s in pt.day_plan.spans if s.id == gone.id), None)
        if span is None:
            continue
        pt._drop(span)
        out.append(f"{span.title} came off today's list, so it's off today's plan too.")
        logger.info("Plan follows the Today note: %r is gone from it, so its plan item left today", span.title)
    if out:
        pt._save_plan_after_change()
    return out


__all__ = ["moved_line", "importance", "lowest", "move_span", "move_unplaced", "place_or_move", "take_line_off",
           "loose_today", "settle_loose", "mirror_today", "hand_deleted", "unlisted_by_hand"]
