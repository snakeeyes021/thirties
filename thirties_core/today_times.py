"""Anything asked for today gets a real time: whatever the user asks for today has its time on the plan.

add_to_today already gives each thing its time. These cover the other ways something is asked for today:

- an item added to one of the lists for today (add_task with today as its day or due date, or the user's words
  saying today or tonight about it) is placed on today's plan at once, in the first free time (if_time.place), as
  add_to_today places what it adds;
- a leftover moved to today says the time it got, so the reply never treats it as still to be placed.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools
    from thirties_core.models import TaskItem

TODAY_WORDS_RE = re.compile(r"\b(?:today|tonight|this (?:morning|afternoon|evening)|later today|before bed)\b",
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
    if not said or not TODAY_WORDS_RE.search(said):
        return False
    from thirties_core.item_facts import mentions
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


def time_of(assistant: "AssistantTools", title: str) -> str:
    """The time something has on today's plan, as said ('' when it has none)."""
    from thirties_core.clock import format_clock
    from thirties_core.models import SpanStatus
    low = title.strip().lower()
    span = next((s for s in assistant.pt.day_plan.spans if s.status == SpanStatus.PLANNED
                 and s.title.strip().lower() == low), None)
    return format_clock(span.start_dt) if span is not None else ""


__all__ = ["for_today", "place_new_item", "time_of", "TODAY_WORDS_RE"]
