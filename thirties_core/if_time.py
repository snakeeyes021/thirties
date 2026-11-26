"""Things on today: each gets its time, except what the user asked to have only if there's time.

Anything on today gets a time on the plan, placed by Python: the first free stretch from now that fits it (whole
thirties, never across a block; the morning routine and everything else already on the plan come first), or
alongside one flexible item. That is its time, like any other item: it moves only when the user moves it, or with
everything else. When the day is full even doubled up, the lowest-priority item moves on to the next day with room
(full_day.py).

The one exception is the "If there's time" section: what the user explicitly asked to keep without a time ("if
there's time", "if I get to it", "on deck", "no rush", "pencil it in"). The model says the user asked
(add_to_today's only_if_time, or pencil_in for today). When one item is added, the plainest words are a fast yes
(``asked_untimed``), never a no; with several in one message the model's flag alone decides each. Nothing else ever
lands there: no leftovers, no removed items, no lines the plan lacks, no later day's items.

It lives in Joplin, the truth: an "If there's time" heading at the bottom of the Today note (``keep`` writes under
it), and lines the user moves in or out of the heading by hand count. ``items`` reads it; ``open_items`` is the
section's clean list for every view. Each item has its actions (``act``): done, schedule it (the next gap, or a time
the user picks), not today (later this week, or tomorrow), and drop. A start or check-in notice being sent anyway,
in a free stretch that fits one, may name it once (``gap_line``). What's left at the day's end goes to the one
leftovers question with the rest of the day's open lines (carry.py); nothing rolls it on by itself.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX

HEADING = "If there's time"  # the Today note's heading for the section, at the bottom
GAP_NAMED_KEY = "if_time_gap_named"  # {day: [(gap end, title)]}: one line per free stretch, each item once

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
    """A fast yes: the user's own words plainly ask for something only if there's time. Never a no: whether they
    asked is the model's call (add_to_today's only_if_time); this only spares it the call on the plainest words."""
    if not message:
        return False
    if UNTIMED_RE.search(message):
        return True
    for m in NO_TIME_RE.finditer(message):
        before = message[:m.start()]
        if _STARTS_NEGATIVE_RE.match(m.group(0)) or not _TURNED_RE.search(before):
            return True
    return False



_CLAUSE_RE = re.compile(r"[.;!?\n]+|\band\b|\bthen\b|\balso\b|\bbut\b", re.IGNORECASE)


def asked_untimed_for(message: Optional[str], title: str) -> bool:
    """The fast yes for one item: the plain words sit in the same part of the message as that item ("the bank at
    3 and the ferns if there's time" says it of the ferns only). Never a no."""
    if not message:
        return False
    from thirties_core.item_facts import mentions
    return any(asked_untimed(part) and mentions(part, title) for part in _CLAUSE_RE.split(message) if part.strip())


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
    # --- also on today's plan: the line here is a leftover; the planned one stays (relations.py) ---
    planned = on_plan_today(assistant.pt, title, task)
    if planned is not None and action in ("tomorrow", "week", "drop") and not drop_said(assistant.pt):
        _remove(writer, note, title)
        return (f"{title} stays on today's plan at {format_clock(planned.start_dt)}, so it's off "
                f"{HEADING}.")
    # --- end also on today's plan ---
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
        _ticked_or_say(_tick(writer, note, title), title)
        if task is not None and assistant._db is not None:
            assistant.pt.scheduler.state_db.record_task_completed(task.id, assistant._today())
        return f"Done: {title}."
    # --- tomorrow only on the user's word: otherwise a later day this week, by fit (not_today.py) ---
    from thirties_core.not_today import tomorrow_unasked
    if action == "tomorrow" and tomorrow_unasked(assistant.pt, assistant._today() + timedelta(days=1)):
        action = "week"
    # --- end tomorrow ---
    if action == "tomorrow":
        from thirties_core.day_lists import AheadLists
        AheadLists(assistant._db).add(assistant._today() + timedelta(days=1), title, task)
        _remove(writer, note, title)
        return f"{title} is on tomorrow's list; it goes on Today in the morning." + due
    if action == "week" or not drop_said(assistant.pt):  # not today, not dropped: it waits for a later day
        when = _set_aside(assistant, title, task)
        _remove(writer, note, title)
        return (f"{title} waits for {when}." if action == "week" else f"{title} is off today; it waits for {when}.") \
            + due
    from thirties_core.carry import DROPPED, WEEK, OpenPool
    pool = OpenPool(assistant._db)
    pool.add([{"title": title, "task_id": task.id if task is not None else "", "horizon": WEEK}], assistant._today())
    pool.place(title, DROPPED)  # drop: it stops coming back; its own list (if it has one) is untouched
    _remove(writer, note, title)
    return f"Dropped {title} from today: it won't come back by itself (it's in Before today's archive)." + due


def _set_aside(assistant, title: str, task) -> str:
    """Not today and not dropped: it waits for a later day this week, picked by fit, or the day the user named
    (not_today.py). The day, as the answer says it."""
    from thirties_core.not_today import minutes_for, set_aside, when_words, where_to
    day, named = where_to(assistant.pt, title, minutes_for(assistant, title, task))
    set_aside(assistant, title, task, day, named)
    return when_words(assistant.pt, day, named)


def _due_today_note(assistant, title: str, task, action: str) -> str:
    """An item due today taken off today (tomorrow, week, drop): a vague word keeps it (refused, saying it's due
    today); a clear one naming the item and a day (or a drop naming it) goes ahead, and the answer names the
    deadline. "" when the item isn't due today or the action keeps it today."""
    from thirties_core import due_today
    from thirties_core.assistant_tools import ToolError
    pt = assistant.pt
    held = due_today.held_due(pt, title, task.id if task is not None else None) \
        if action in ("tomorrow", "week", "drop") else None  # overdue too
    if held is None:
        return ""
    others = due_today.others_on(pt, title)
    by_hand = bool(getattr(pt, "by_hand", False))
    if (action == "week" and not by_hand) or (action == "tomorrow" and not due_today.clear_move(pt, title, others)):
        raise ToolError(due_today.stays_line(title, held, pt.today()))
    # a drop: the model's call stands, and the answer says it was due (off_today.leave_list says it on today's list)
    if action == "tomorrow":
        return " " + due_today.moved_line(title, pt.today() + timedelta(days=1), pt.today(), held)
    return " " + due_today.moved_line(title, None, pt.today(), held)


def _act_on_listed(assistant, item: str, action: str, time: Optional[str]) -> str:
    """The actions on an item on today's list that isn't under If there's time (a line with a time on the plan, or
    one with none yet). Taking it off today (tomorrow, week, drop) takes it off the plan today as well; its home list
    line is untouched, and nothing is ticked. A dropped item that lives only on Today is filed in the month's Dropped
    note; one for tomorrow goes on tomorrow's Today list; one for later this week waits in Before today's pool."""
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import format_clock
    from thirties_core.leave_today import has_home, top_line
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
        _ticked_or_say(_tick(assistant._writer(), assistant._today_note(), line), line)
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
    if action == "drop":  # off today's list, and it doesn't come back by itself; dropped for good only on the
        # user's drop word (off_today.leave_list); its deadline, if any, is said, never a hold
        from thirties_core.off_today import leave_list
        said = leave_list(pt, line, task.id if task is not None else None, "drop", also=False).strip()
        due = ""
    else:
        from thirties_core.leave_today import remove_copy_why
        why = remove_copy_why(assistant, line)
        if why:
            raise ToolError(f"{line} is still on today's list: {why}.")
        # --- tomorrow only on the user's word: otherwise a later day this week, by fit (not_today.py) ---
        from thirties_core.not_today import tomorrow_unasked
        if action == "tomorrow" and tomorrow_unasked(pt, assistant._today() + timedelta(days=1)):
            action = "week"
        # --- end tomorrow ---
        if action == "tomorrow":
            from thirties_core.day_lists import AheadLists
            AheadLists(assistant._db).add(assistant._today() + timedelta(days=1), line,
                                          task if homed else None)
            said = f"{line} is on tomorrow's list; it goes on Today in the morning."
        elif action == "week" or (action == "drop" and not homed):  # off today without a drop word: still to do
            said = f"{line} waits for {_set_aside(assistant, line, task if homed else None)}."
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


def _tick(writer, note, title: str) -> Tuple[str, str]:
    if writer is not None and note is not None:
        return writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": note["id"], "title": title})
    return "failed", "Joplin isn't connected"


def _ticked_or_say(result: Tuple[str, str], title: str) -> None:
    """A tick Joplin didn't take: the done stops there, nothing else about the item is written, and the answer says
    plainly that it isn't ticked."""
    from thirties_core.assistant_tools import ToolError
    status, msg = result
    if status not in ("done", "queued"):
        raise ToolError(f"{title} isn't ticked done ({msg}); nothing else about it changed.")


def drop_said(pt) -> bool:
    """A drop goes ahead as a drop on the app's own controls, or on the user's drop words (write_guard.py); "take it
    off" alone takes it off today and it stays to do."""
    message = getattr(pt, "user_message", None)
    if getattr(pt, "by_hand", False) or message is None:
        return True
    from thirties_core.write_guard import asks_to_drop
    return asks_to_drop(message)


def planned_out(assistant, title: str) -> bool:
    """An item now on the plan another way (a follow-up named like it): its line leaves the heading, as when it's
    scheduled from here. True when it was there."""
    note = assistant._today_note()
    line = next((t for t in items(note) if t.strip().lower() == str(title).strip().lower()), None)
    if line is None:
        return False
    _move_out(assistant._writer(), note, line)
    return True


def _move_out(writer, note, title: str) -> None:
    """Scheduled: the line leaves the heading for the timed part of the note."""
    if writer is not None and note is not None:
        writer.submit({"op": "move", "note_id": note["id"], "to_note_id": note["id"], "title": title, "section": "",
                       "open_only": True})


# --- the section: the Today note's "If there's time" heading, filled only when the user asked ---

def keep(assistant, names, because=None) -> str:
    """The user asked for these only if there's time (the model's call: add_to_today's only_if_time, or
    pencil_in for today): each goes under the Today note's "If there's time" heading, at the bottom, with no time.
    Nothing else ever lands there. A line already on Today moves under the heading; one already on today's plan
    keeps its time."""
    from thirties_core.add_source import refused as uncited
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import format_clock
    from thirties_core.today import TodayLinks, note_items
    note = assistant._today_note()
    if note is None:
        raise ToolError("There's no Today note yet: make a note for today's list in Joplin, or mark one as Today in "
                        "Profile → Lists.")
    writer = assistant._writer()
    if writer is None:
        raise ToolError("No Joplin link: the If there's time section lives in the Today note (Preferences → "
                        "Tasks).")
    on_today = {text.lower(): text for text, _ in note_items(note)}
    kept, planned, queued = [], [], False
    for name in names:
        task = assistant._exact_task(str(name))
        from thirties_core.titles import title_of
        title = title_of(task.title) if task is not None else str(name).strip()
        if not title:
            continue
        if task is None and title.lower() not in on_today:
            why = uncited(assistant.pt, "add_to_today", title, because)
            if why:
                raise ToolError(why)
        span = on_plan_today(assistant.pt, title, task)
        if span is not None:
            planned.append(f"{title} is already on today's plan at {format_clock(span.start_dt)}")
            continue
        if is_listed(note, title):
            kept.append(title)
            continue
        if title.lower() in on_today:  # on Today already: its line moves under the heading
            op = {"op": "move", "note_id": note["id"], "to_note_id": note["id"], "title": on_today[title.lower()],
                  "section": HEADING, "open_only": True}
        else:
            op = {"op": "add_task", "note_id": note["id"], "title": title, "section": HEADING}
        status, msg = writer.submit(op)
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        queued = queued or status == "queued"
        if task is not None and task.parent_note_id != note["id"] and assistant._db is not None:
            TodayLinks(assistant._db).link(title, task)
        kept.append(title)
    parts = []
    if kept:
        later = " (Joplin will get it when it's open)" if queued else ""
        parts.append(f"Under {HEADING} on today's list, no set time: {', '.join(kept)}{later}. When a free stretch "
                     "fits, I'll mention it once.")
    parts += [p + "." for p in planned]
    return " ".join(parts) or "Nothing to keep."


def open_items(assistant) -> List[str]:
    """The section's clean list, for every view that shows it: the open lines under the Today note's "If there's
    time" heading, and the ones Thirties has put there and are still waiting to reach Joplin (``queued``), minus any
    that have a time on today's plan by now. Nothing else, ever."""
    try:
        note = assistant._today_note()
        listed = items(note)
    except Exception:
        return []
    seen = {t.lower() for t in listed}
    for title in queued(assistant, note):
        if title.lower() not in seen:
            seen.add(title.lower())
            listed.append(title)
    return [t for t in listed if on_plan_today(assistant.pt, t) is None]


def queued(assistant, note: Optional[Dict[str, Any]]) -> List[str]:
    """Lines Thirties has written under the Today note's heading that wait in the outbox for Joplin (closed, or
    not caught up yet): they're in the section as far as the user is concerned, so every view shows them."""
    import json
    db = getattr(assistant, "_db", None)
    if db is None or not note:
        return []
    try:
        rows = db.outbox_pending()
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            op = json.loads(row.get("op") or "{}")
        except ValueError:
            continue
        target = op.get("to_note_id") if op.get("op") == "move" else op.get("note_id")
        if op.get("op") in ("add_task", "move") and target == note.get("id") and op.get("section") == HEADING \
                and str(op.get("title") or "").strip():
            out.append(str(op["title"]).strip())
    return out


def text_for(assistant) -> str:
    """The section as the tools' schedule text gives it to the model: after the day's timed items."""
    listed = open_items(assistant)
    return ("\n\nIf there's time:\n" + "\n".join(listed)) if listed else ""


def gap_line(assistant, now: datetime) -> str:
    """For a start or check-in notice being sent anyway (never a message of its own): when the free stretch the user
    is in fits one of the section's items, a line naming it. One line per free stretch, and each item named once."""
    import json
    from thirties_core.clock import format_clock, thirty_of
    from thirties_core.day_proposal import usual_minutes
    db = assistant._db
    if db is None:
        return ""
    listed = open_items(assistant)
    if not listed:
        return ""
    end = next((b for a, b in assistant.pt.open_windows(after=now) if a <= thirty_of(now) < b), None)
    if end is None:
        return ""
    day = assistant._today().isoformat()
    try:
        named = json.loads(db.get_sync_meta(GAP_NAMED_KEY) or "{}").get(day, [])
    except ValueError:
        named = []
    if any(g == end.isoformat() for g, _ in named):
        return ""  # this free stretch has had its line
    said = {t for _, t in named}
    for title in listed:
        if title in said:
            continue
        task = assistant._exact_task(title)
        if end - now < timedelta(minutes=usual_minutes(assistant, task, title) or 30):
            continue
        db.set_sync_meta(GAP_NAMED_KEY, json.dumps({day: named + [[end.isoformat(), title]]}))
        return f"Free until {format_clock(end)}; if there's time: {title}."
    return ""


__all__ = ["HEADING", "asked_untimed", "items", "is_listed", "first_slot", "doubled_slot", "place", "act", "ACTIONS",
           "planned_out", "keep", "open_items", "text_for", "gap_line", "asked_untimed_for"]
