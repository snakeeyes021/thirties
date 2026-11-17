"""Things added for today, and the "If there's time" section.

Anything the user asks to have on today gets a time on the plan at once, placed by Python: the first free stretch
from now that fits it (whole thirties, never across a block; the morning routine and everything else already on
the plan come first). That is its time, like any other item: it moves only when the user moves it, or with
everything else.

The one exception is the user asking for it untimed, in so many words ("if there's time", "if I get to it", "no
rush, sometime today", "don't schedule it", "squeeze it in if you can", "on deck"). Python checks the wording; the
model can't decide on its own to leave something untimed. Overflow (the day really is full, even doubled up) lands
there too, with one line saying so.

Untimed items live in the Today note under a plain "If there's time" heading at the bottom, so Joplin shows the
same picture, and moving a line under or out of the heading by hand works too. Each has its actions: done,
schedule it (the next gap, or a time the user picks), and not today (tomorrow, later this week, or drop). What's
still there at the day's end goes to the next morning's Before today with "this week" as its default.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX

HEADING = "If there's time"

UNTIMED_RE = re.compile(
    r"\bif (?:there'?s|there is|i have|i've got|i get|we have) (?:the |some |any )?time\b"
    r"|\bif i (?:get|can get) (?:to|round to|around to) (?:it|them|that|those)\b"
    r"|\bno rush\b|\bsome ?time today\b|\bwhenever (?:today|i can|i get a chance)\b"
    r"|\b(?:don'?t|do not|no need to) (?:schedule|time|plan) (?:it|that|them|this|those)\b"
    r"|\bsqueeze (?:it|that|them|this|those) in\b|\bon deck\b|\bno (?:set|specific|particular) time\b"
    r"|\bpencil in\b|\bpencil(?:led|ed)?(?: \w+){1,3} in\b",
    re.IGNORECASE)

# Other plain ways of saying the item gets no time today ("no clock time on the gutters at all", "don't give it a
# time", "whenever", "unscheduled is fine", "it doesn't need a time"). Each says it outright; a time of day ("sometime
# this afternoon") is a time, and so is "whenever you think fits" (Thirties picks one).
_KIND = r"(?:clock |set |specific |particular |fixed |exact |real )?"
NO_TIME_RE = re.compile(
    rf"\bno {_KIND}(?:clock )?time on\b|\bno (?:clock|set|specific|particular|fixed|exact) times?\b"
    r"|\bno (?:time ?slot|slot)\b"
    r"|\b(?:don'?t|dont|do not|no need to|doesn'?t|does not|needn'?t|won'?t need|shouldn'?t)\s+(?:need to\s+)?"
    r"(?:give|put|set|assign|attach|add|pick|have|need|get|want)\b(?:\s+[\w'&-]+){0,4}?\s+(?:a|any)\s+"
    rf"{_KIND}(?:time|time ?slot|slot)\b"
    r"|\bwhenever\b(?!\s+(?:you (?:think|want|like|see fit|decide|pick|choose|prefer)\b|it (?:fits|suits)\b|"
    r"that (?:fits|suits)\b|there'?s a gap\b))"
    r"|\b(?:some ?time|any ?time|at some point|at any point)(?:\s+(?:today|is fine|works|is good|is ok|is okay))\b"
    r"|\bat some point\b|\bun-?scheduled\b|\buntimed\b"
    rf"|\bwithout (?:a |any )?{_KIND}(?:time|time ?slot|slot)\b|\bnot at (?:a|any) {_KIND}time\b"
    r"|\b(?:leave|keep) (?:it|that|them|this|those|[\w'&-]+) (?:off the (?:clock|timeline)|floating)\b"
    r"|\b(?:the )?(?:time|when) (?:doesn'?t|does not|don'?t) matter\b|\bdoesn'?t matter when\b"
    r"|\b(?:i )?don'?t (?:care|mind) when\b",
    re.IGNORECASE)
# a "not" in front of it turns it round: "it can't be whenever", "don't leave it unscheduled"
_TURNED_RE = re.compile(r"\b(?:not|never|can'?t|cannot|shouldn'?t|won'?t|don'?t|dont|do not|isn'?t)\b[^.;!?\n,]*$",
                        re.IGNORECASE)
_STARTS_NEGATIVE_RE = re.compile(r"^(?:no\b|don'?t|dont|do not|doesn'?t|does not|needn'?t|won'?t|shouldn'?t|"
                                 r"without|not at|(?:i )?don'?t)", re.IGNORECASE)


def asked_untimed(message: Optional[str]) -> bool:
    """The user's own words ask for something to have no time today. Only their message counts: never the model's."""
    if not message:
        return False
    if UNTIMED_RE.search(message):
        return True
    for m in NO_TIME_RE.finditer(message):
        before = message[:m.start()]
        if _STARTS_NEGATIVE_RE.match(m.group(0)) or not _TURNED_RE.search(before):
            return True
    return False


def _is_heading(line: str) -> bool:
    m = HEADING_REGEX.match(line)
    return bool(m and m.group(1).strip().rstrip(":").lower() == HEADING.lower())


def items(note: Optional[Dict[str, Any]], ticked: bool = False) -> List[str]:
    """The open top-level lines under the note's "If there's time" heading (``ticked``: the ticked ones)."""
    out, inside = [], False
    for line in ((note or {}).get("body") or "").splitlines():
        if HEADING_REGEX.match(line):
            inside = _is_heading(line)
            continue
        if not inside:
            continue
        m = (DONE_CHECKBOX_REGEX if ticked else CHECKBOX_REGEX).match(line)
        if m and not m.group(1):
            out.append(m.group(2).strip())
    return out


def is_listed(note: Optional[Dict[str, Any]], title: str) -> bool:
    return title.strip().lower() in {t.lower() for t in items(note)}


# --- placing something today ---

def _waking_end(plan) -> datetime:
    """When the day's waking time ends: the start of the night's sleep, else the end of the canvas."""
    from thirties_core.models import SpanCategory
    sleeps = [s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start]
    return min(sleeps, default=plan.day_end)


def first_slot(pt, minutes: int, after: Optional[datetime] = None) -> Optional[datetime]:
    """The first free stretch from now that fits ``minutes`` (whole thirties; the current one only if what's left
    of it is enough), before bedtime."""
    from thirties_core.clock import first_fitting_thirty
    now = after or pt._now()
    need = timedelta(minutes=minutes)
    end = _waking_end(pt.day_plan)
    for a, b in pt.open_windows(after=now):
        a = max(a, first_fitting_thirty(now, minutes))
        b = min(b, end)
        if b - a >= need:
            return a
    return None


def doubled_slot(pt, minutes: int, after: Optional[datetime] = None) -> Optional[datetime]:
    """With no free stretch left: the first stretch from now where it can go alongside one flexible item (never
    beside a calendar event, work, sleep or another doubled-up pair)."""
    from thirties_core.clock import first_fitting_thirty
    from thirties_core.models import SpanCategory, SpanStatus
    now = after or pt._now()
    end = _waking_end(pt.day_plan)
    t = first_fitting_thirty(now, minutes)
    need = timedelta(minutes=minutes)
    while t + need <= end:
        inside = [s for s in pt.day_plan.spans if s.status == SpanStatus.PLANNED and not s.shown_only
                  and s.start_dt < t + need and s.end_dt > t]
        # nor beside something that keeps its time (a rest the user asked for is a gap, not room to share)
        fixed = [s for s in inside if s.category in (SpanCategory.CALENDAR, SpanCategory.WORK, SpanCategory.SLEEP)
                 or s.is_firm or s.is_envelope or pt._is_fixed(s)]
        if not fixed and len(inside) <= 1:
            return t
        t += timedelta(minutes=30)
    return None


def on_plan_today(pt, title: str, task=None):
    """The planned item today that already stands for this, if any (scheduled some other way first)."""
    from thirties_core.models import SpanStatus
    low = title.strip().lower()
    for s in pt.day_plan.spans:
        if s.status != SpanStatus.PLANNED or s.end_dt <= pt._now():
            continue
        if s.title.strip().lower() == low or (task is not None and s.task_id and s.task_id == task.id):
            return s
    return None


def place(assistant, title: str, task=None) -> Tuple[Optional[datetime], bool]:
    """Give ``title`` its time today: (its start, or None if the day is full even doubled up; whether doubled up)."""
    from thirties_core.clock import format_clock
    from thirties_core.day_proposal import usual_minutes
    from thirties_core.plan_tools import NOT_DONE
    pt = assistant.pt
    existing = on_plan_today(pt, title, task)
    if existing is not None:
        return existing.start_dt, False
    minutes = usual_minutes(assistant, task, title)
    for doubled, spot in ((False, first_slot(pt, minutes)), (True, None)):
        if doubled:
            spot = doubled_slot(pt, minutes)
        if spot is None:
            continue
        args = {"title": title, "start": format_clock(spot), "duration": f"{minutes} minutes"}
        if doubled:
            args["alongside"] = True
        if task is not None:
            args.update(task_id=task.id, category="task")
        out = pt.execute("add_event", args)
        if not out.startswith(NOT_DONE):
            return spot, doubled
    return None, False


# --- the actions on an untimed item ---

ACTIONS = ("done", "schedule", "tomorrow", "week", "drop")


def act(assistant, item: str, action: str, time: Optional[str] = None) -> str:
    """One action on an item under If there's time. ``time``: for schedule, a time the user picked."""
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import format_clock
    action = (action or "").strip().lower()
    action = {"later this week": "week", "this week": "week", "not today": "week", "schedule it": "schedule"}.get(
        action, action)
    if action not in ACTIONS:
        raise ToolError(f"Say what to do with it: one of {', '.join(ACTIONS)}.")
    note = assistant._today_note()
    title = next((t for t in items(note) if t.strip().lower() == str(item).strip().lower()), None)
    if title is None:
        # --- any other item on today's list: the same actions take it off today (leave_today.py) ---
        return _act_on_listed(assistant, item, action, time)
        # --- end any other item ---
    writer = assistant._writer()
    task = assistant._exact_task(title)
    due = _due_today_note(assistant, title, task, action)  # due today: it goes only on a clear word (due_today.py)
    if action == "schedule":
        if time:
            args = {"title": title, "start": str(time), "duration": "30 minutes"}
            if task is not None:
                args.update(task_id=task.id, category="task")
            out = assistant.pt.execute("add_event", args)
            if out.startswith("NOT_DONE"):
                return out
            at = time
        else:
            spot, doubled = place(assistant, title, task)
            if spot is None:
                return f"There's no time left today for {title}, even alongside something else; it stays here."
            at = format_clock(spot) + (" (alongside another item)" if doubled else "")
        _move_out(writer, note, title)
        return f"{title} is on the plan at {at}."
    if action == "done":
        _tick(writer, note, title)
        if task is not None and assistant._db is not None:
            assistant.pt.scheduler.state_db.record_task_completed(task.id, assistant._today())
        return f"Done: {title}."
    if action == "tomorrow":
        from thirties_core.day_lists import AheadLists
        AheadLists(assistant._db).add(assistant._today() + timedelta(days=1), title, task)
        _remove(writer, note, title)
        return f"{title} is on tomorrow's list; it goes on Today in the morning." + due
    from thirties_core.carry import DROPPED, WEEK, OpenPool
    pool = OpenPool(assistant._db)
    pool.add([{"title": title, "task_id": task.id if task is not None else "", "horizon": WEEK}], assistant._today())
    if action == "week":
        pool.place(title, WEEK)
        _remove(writer, note, title)
        return f"{title} waits for a day later this week." + due
    pool.place(title, DROPPED)  # drop: it stops coming back; its own list (if it has one) is untouched
    _remove(writer, note, title)
    return f"Dropped {title} from today: it won't come back by itself (it's in Before today's archive)." + due


def _due_today_note(assistant, title: str, task, action: str) -> str:
    """An item due today taken off today (tomorrow, week, drop): a vague word keeps it (refused, saying it's due
    today); a clear one naming the item and a day (or a drop naming it) goes ahead, and the answer names the
    deadline. "" when the item isn't due today or the action keeps it today."""
    from thirties_core import due_today
    from thirties_core.assistant_tools import ToolError
    pt = assistant.pt
    if action not in ("tomorrow", "week", "drop") or \
            not due_today.is_due_today(pt, title, task.id if task is not None else None):
        return ""
    others = due_today.others_on(pt, title)
    by_hand = bool(getattr(pt, "by_hand", False))
    if (action == "week" and not by_hand) or (action == "tomorrow" and not due_today.clear_move(pt, title, others)) \
            or (action == "drop" and not due_today.clear_ask(pt, title, others)):
        raise ToolError(due_today.stays_line(title))
    if action == "tomorrow":
        return " " + due_today.moved_line(title, pt.today() + timedelta(days=1), pt.today())
    return " " + due_today.moved_line(title, None, pt.today())


def _act_on_listed(assistant, item: str, action: str, time: Optional[str]) -> str:
    """The actions on an item on today's list that isn't under If there's time (a line with a time on the plan, or
    one with none yet). Taking it off today (tomorrow, week, drop) takes it off the plan today as well; its home list
    line is untouched, and nothing is ticked. A dropped item that lives only on Today is filed in the month's Dropped
    note; one for tomorrow goes on tomorrow's Today list; one for later this week waits in Before today's pool."""
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import format_clock
    from thirties_core.leave_today import has_home, remove_copy, top_line
    from thirties_core.models import SpanStatus
    pt = assistant.pt
    task = assistant._exact_task(str(item)) or next((t for t in assistant.s.tasks if t.id == str(item)), None)
    line = top_line(pt, task.title if task is not None else str(item), task.id if task is not None else None)
    if line is None:
        raise ToolError(f"{item} isn't on today's list.")
    from thirties_core.today import TodayLinks
    link = TodayLinks(assistant._db).home_of(line) if assistant._db is not None else None
    task = next((t for t in assistant.s.tasks if link and t.id == link["home_id"]), None) or \
        assistant._exact_task(line) or task
    due = _due_today_note(assistant, line, task, action)  # due today: it goes only on a clear word (due_today.py)
    ahead = [s for s in pt.day_plan.spans if s.status == SpanStatus.PLANNED and s.end_dt > pt._now()
             and not s.is_envelope and s.source != "calendar"
             and (s.title.strip().lower() == line.strip().lower() or (task is not None and s.task_id == task.id))]
    if action == "done":
        if ahead:
            return assistant.log_outcome(event=ahead[0].id, status="done")
        _tick(assistant._writer(), assistant._today_note(), line)
        if task is not None and assistant._db is not None:
            pt.scheduler.state_db.record_task_completed(task.id, assistant._today())
        return f"Done: {line}."
    if action == "schedule":
        if time:
            args = {"title": line, "start": str(time), "duration": "30 minutes"}
            if ahead:
                return pt.execute("update_event", {"event": ahead[0].id, "start": str(time)})
            if task is not None:
                args.update(task_id=task.id, category="task")
            out = pt.execute("add_event", args)
            return out if out.startswith("NOT_DONE") else f"{line} is on the plan at {time}."
        if ahead:
            return f"{line} is already on the plan at {format_clock(ahead[0].start_dt)}."
        spot, doubled = place(assistant, line, task)
        if spot is None:
            return f"There's no time left today for {line}, even alongside something else; it stays on the list."
        return f"{line} is on the plan at {format_clock(spot)}" + (" (alongside another item)." if doubled else ".")
    homed = has_home(assistant, line)
    if action == "drop" and not homed:
        from thirties_core.off_today import take_off_list
        if not take_off_list(pt, line):
            raise ToolError(f"{line} couldn't be taken off today's list just now.")
        said = f"Dropped {line}: it's filed in this month's Dropped note."
    else:
        if not remove_copy(assistant, line):
            raise ToolError(f"{line} couldn't be taken off today's list just now.")
        if action == "tomorrow":
            from thirties_core.day_lists import AheadLists
            AheadLists(assistant._db).add(assistant._today() + timedelta(days=1), line,
                                          task if homed else None)
            said = f"{line} is on tomorrow's list; it goes on Today in the morning."
        elif action == "week":
            from thirties_core.carry import WEEK, OpenPool
            pool = OpenPool(assistant._db)
            pool.add([{"title": line, "task_id": task.id if homed and task is not None else "", "horizon": WEEK}],
                     assistant._today())
            pool.place(line, WEEK)
            said = f"{line} waits for a day later this week."
        else:
            said = f"Took {line} off today's list; it stays on its own list."
    for s in ahead:  # not today: off today's plan too
        pt._drop(s)
    if ahead:
        pt._save_plan_after_change()
    return said + due


def _remove(writer, note, title: str) -> None:
    if writer is not None and note is not None:
        writer.submit({"op": "remove", "note_id": note["id"], "title": title, "state": "open"})


def _tick(writer, note, title: str) -> None:
    if writer is not None and note is not None:
        writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": note["id"], "title": title})


def _move_out(writer, note, title: str) -> None:
    """Scheduled: the line leaves the heading for the timed part of the note."""
    if writer is not None and note is not None:
        writer.submit({"op": "move", "note_id": note["id"], "to_note_id": note["id"], "title": title, "section": "",
                       "open_only": True})


GAP_NAMED_KEY = "if_time_gap_named"  # {day: [(gap end, title)]}: one item named per gap, never the same gap twice


def gap_line(assistant, now: datetime) -> str:
    """When the day has a real gap now (half an hour or more free from now), a line naming one item from If there's
    time, for a notice that's being sent anyway (never a message of its own). Once per gap; a different item for
    each gap until the list comes round again."""
    import json
    from thirties_core.clock import format_clock, thirty_of
    db = assistant._db
    if db is None:
        return ""
    try:
        listed = items(assistant._today_note())
    except Exception:
        return ""
    if not listed:
        return ""
    end = next((b for a, b in assistant.pt.open_windows(after=now) if a <= thirty_of(now) < b), None)
    if end is None or end - now < timedelta(minutes=30):
        return ""
    day = assistant._today().isoformat()
    try:
        named = json.loads(db.get_sync_meta(GAP_NAMED_KEY) or "{}").get(day, [])
    except ValueError:
        named = []
    if any(g == end.isoformat() for g, _ in named):
        return ""  # this gap has had its line
    said = [t for _, t in named]
    last = {t: i for i, t in enumerate(said)}  # when each was last named
    pick = next((t for t in listed if t not in last), None) or min(listed, key=lambda t: last[t])
    db.set_sync_meta(GAP_NAMED_KEY, json.dumps({day: named + [[end.isoformat(), pick]]}))
    return f"Free until {format_clock(end)}; if there's time: {pick}."


def text_for(assistant) -> str:
    """The section in a schedule message: plain lines, after the day's timed items."""
    try:
        listed = items(assistant._today_note())
    except Exception:
        return ""
    return ("\n\nIf there's time:\n" + "\n".join(listed)) if listed else ""


__all__ = ["HEADING", "asked_untimed", "items", "is_listed", "first_slot", "doubled_slot", "place", "act",
           "ACTIONS", "text_for", "gap_line"]
