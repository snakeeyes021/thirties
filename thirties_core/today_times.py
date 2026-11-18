"""Anything asked for today gets a real time: whatever the user asks for today has its time on the plan.

add_to_today already gives each thing its time. These cover the other ways something is asked for today:

- an item added to one of the lists for today (add_task with today as its day or due date, or the user's words
  saying today or tonight about it) is placed on today's plan at once, in the first free time (if_time.place), as
  add_to_today places what it adds;
- a leftover moved to today says the time it got, so the reply never treats it as still to be placed;
- "add X to the list" said while the day is under way, naming no other list or day, is for today: "the list" is
  today's list;
- an item held on today (due today or overdue: due_today.py) that loses its time, by a skip or a vague "not now",
  gets a new block later today (``retime_held``), never an untimed line. Only when the day is full even doubled up
  does it wait on today's list, and the answer says so.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools
    from thirties_core.models import TaskItem

TODAY_WORDS_RE = re.compile(r"\b(?:today|tonight|this (?:morning|afternoon|evening)|later today|before bed)\b",
                            re.IGNORECASE)
# "add it to the list", "put X on my to-do list", "on today's list": the list, said plainly, is today's
THE_LIST_RE = re.compile(r"\b(?:to|on|onto|in|into)\s+(?:the|my|today'?s)\s+(?:to-?do\s+|todo\s+)?list\b|"
                         r"\b(?:to|on|onto)\s+(?:the|my)\s+(?:to-?dos?|todos?)\b", re.IGNORECASE)
# a day other than today, or a horizon, said with it: then it isn't for today
OTHER_DAY_RE = re.compile(r"\b(?:tomorrow|tmrw|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|this "
                          r"(?:week|weekend|month|year)|later this week|someday|some day|eventually|sometime)\b",
                          re.IGNORECASE)


def for_today(assistant: "AssistantTools", title: str, day: Any = None, due_day: Optional[date] = None) -> bool:
    """Whether this item was asked for today: its day or due date is today, or the user's words say today about
    it (a message naming one thing, or naming this one)."""
    pt = assistant.pt
    if not pt.is_current(pt.day_plan):
        return False
    today = assistant._today()
    if due_day is not None and due_day == today:
        return True
    if day not in (None, ""):
        try:
            from thirties_core.clock import parse_day
            if parse_day(str(day), today) == today:
                return True
        except Exception:
            pass
    said = pt.user_message
    if not said:
        return False
    from thirties_core.item_facts import mentions
    if THE_LIST_RE.search(said) and not OTHER_DAY_RE.search(said) and day in (None, "") and due_day is None:
        return mentions(said, title)  # "add the clay order to the list" while the day is on: today's list
    if not TODAY_WORDS_RE.search(said):
        return False
    return mentions(said, title)


def place_new_item(assistant: "AssistantTools", task: "TaskItem", day: Any = None,
                   due_day: Optional[date] = None) -> str:
    """For an item just added to a list: its time today when it was asked for today ('' otherwise)."""
    if not for_today(assistant, task.title, day, due_day):
        return ""
    from thirties_core import if_time
    from thirties_core.clock import format_clock
    spot, doubled = if_time.place(assistant, task.title, task)
    if spot is None:
        return " Today is full, so it has no time yet: say where it should go."
    return f" On today's plan at {format_clock(spot)}" + (" (alongside another item)." if doubled else ".")


def retime_held(assistant: "AssistantTools", line: str, task_id: Optional[str] = None,
                after: Optional[datetime] = None, due: Optional[date] = None, said: str = "") -> str:
    """A held item (its date is today or already past) lost its block: it gets another one later today, from
    ``after`` (where the lost block ended) or now, whichever comes later. Returns the note for the tool's answer;
    "" when no block is left at all, so the caller keeps its own line. ``due``: the date holding it here.
    ``said``: the caller's own words for why it's back on, before the time."""
    if assistant is None:
        return ""
    from thirties_core import if_time
    from thirties_core.due_today import deadline_words
    from thirties_core.clock import format_clock
    from thirties_core.day_proposal import usual_minutes
    from thirties_core.plan_tools import NOT_DONE
    pt = assistant.pt
    task = next((t for t in assistant.s.tasks if task_id and t.id == task_id), None)
    start = max(after, pt._now()) if after is not None else None
    try:
        minutes = usual_minutes(assistant, task, line)
        for doubled in (False, True):
            spot = (if_time.doubled_slot if doubled else if_time.first_slot)(pt, minutes, start)
            if spot is None:
                continue
            args = {"title": line, "start": format_clock(spot), "duration": f"{minutes} minutes"}
            if doubled:
                args["alongside"] = True
            if task is not None:
                args.update(task_id=task.id, category="task")
            if not pt.execute("add_event", args).startswith(NOT_DONE):
                why = deadline_words(due, pt.today()) if due is not None else "due today"
                if said:  # the caller's own words for why it's back on ("can't wait, so it's on tonight at")
                    return f" ({line} {said} {format_clock(spot)}" + (", alongside another item)" if doubled else ")")
                return (f" ({line} is {why}, so it's on today's plan again at {format_clock(spot)}"
                        + (", alongside another item)" if doubled else ")"))
    except Exception:
        return ""
    return ""


def time_of(assistant: "AssistantTools", title: str) -> str:
    """The time something has on today's plan, as said ('' when it has none)."""
    from thirties_core.clock import format_clock
    from thirties_core.models import SpanStatus
    low = title.strip().lower()
    span = next((s for s in assistant.pt.day_plan.spans if s.status == SpanStatus.PLANNED
                 and s.title.strip().lower() == low), None)
    return format_clock(span.start_dt) if span is not None else ""


__all__ = ["for_today", "place_new_item", "retime_held", "time_of", "TODAY_WORDS_RE", "THE_LIST_RE"]
