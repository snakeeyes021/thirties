"""Holidays: the days on a calendar the user marked "holidays" (calendar_engine.holidays_on), as plain facts.

A holiday never receives what Python or the model places by itself: a leftover carried in the morning, an item
skipped or put off, the model's own pick of a day for a leftover, or a pool item drawn into the day's proposal. Each
takes the next day with room instead. What the user explicitly puts on that day is theirs and stays: a day their
words name, a placement they made, a due date they set.

Python only says which days are holidays (``coming``, ``ahead_text`` for the morning model). Whether an item is about
getting ready for one, and so has to be done before it, is the model's reading of the item, never a pattern here.

Example (invented): "Lantern Day" on a festival calendar falls on Saturday. A bench job skipped on Thursday would come
back on Saturday, the first later day with room; it comes back on Sunday instead. Told "do it Saturday", it waits for
Saturday.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

AHEAD_DAYS = 7  # how far the morning model is told about coming holidays
SEARCH_DAYS = 14  # placements search this many days beyond a holiday


def names(pt: Any, day: date) -> List[str]:
    """The holidays on ``day``, by name ([] when none, or when the day can't be read)."""
    try:
        return list(getattr(pt.plan_for(day), "holidays", None) or [])
    except Exception:
        return []


def is_holiday(pt: Any, day: date) -> bool:
    return bool(names(pt, day))


def coming(pt: Any, today: date, days: int = AHEAD_DAYS) -> List[Tuple[date, str]]:
    """(day, name) for each holiday from today through the coming week."""
    return [(today + timedelta(days=n), name) for n in range(days + 1) for name in names(pt, today + timedelta(days=n))]


def ahead_text(pt: Any, today: date, days: int = AHEAD_DAYS) -> str:
    """For the morning model: the coming week's holidays by name and day ("" when there are none)."""
    found = coming(pt, today, days)
    if not found:
        return ""

    def when(d: date) -> str:
        gap = (d - today).days
        return ("today" if gap == 0 else "tomorrow" if gap == 1 else d.strftime("%A")) + d.strftime(", %b %-d")
    return ("HOLIDAYS IN THE COMING WEEK (on calendars the user marked as holidays; nothing is placed on them by "
            "itself): " + "; ".join(f"{name} ({when(d)})" for d, name in found) + ".")


def user_named(pt: Any, day: date) -> bool:
    """The user's words this turn name ``day``: the day itself ("Saturday", "tomorrow", "today" for today, a date)
    or one of its holidays by name."""
    message = getattr(pt, "user_message", None)
    if not message:
        return False
    low = message.lower()
    if any(name.strip().lower() in low for name in names(pt, day) if name.strip()):
        return True
    today = pt.today()
    if day == today:
        return bool(re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", low))
    from thirties_core.not_today import named_window
    window = named_window(pt, message, today)
    return window is not None and window[0] <= day <= window[1]


def next_open_day(pt: Any, day: date, title: str, minutes: int = 30) -> date:
    """The first day after ``day`` that isn't a holiday and has room for the item (by fit, replan.py); failing that,
    the first day after it that isn't a holiday."""
    from thirties_core.replan import first_day_with_room, outdoors
    start, last = day + timedelta(days=1), day + timedelta(days=SEARCH_DAYS)
    try:
        found = first_day_with_room(pt, max(30, int(minutes or 30)), start, last, daylight=outdoors(title, pt))
    except Exception:
        found = None
    return found or first_workable(pt, start)


def first_workable(pt: Any, day: date) -> date:
    """``day``, or the first day after it that isn't a holiday."""
    for n in range(SEARCH_DAYS):
        if not is_holiday(pt, day + timedelta(days=n)):
            return day + timedelta(days=n)
    return day


def off_holiday(pt: Any, window: Dict[str, str], title: str, minutes: int = 30, given: bool = False) -> Dict[str, str]:
    """A leftover's window ({"when", "start", "end"}) of one day that is a holiday, picked without the user's word
    for that day: the next day with room instead. ``given``: the placement is the user's own (the app's controls, or
    nobody talking); it stands. Any other window as it is (a holiday inside a longer one draws nothing by itself:
    day_proposal.py)."""
    if given or getattr(pt, "by_hand", False) or not window.get("start") or window.get("start") != window.get("end"):
        return window
    try:
        day = date.fromisoformat(window["start"])
    except ValueError:
        return window
    if not is_holiday(pt, day) or user_named(pt, day):
        return window
    d = next_open_day(pt, day, title, minutes)
    return {**window, "when": d.strftime("%A"), "start": d.isoformat(), "end": d.isoformat()}


__all__ = ["names", "is_holiday", "coming", "ahead_text", "user_named", "next_open_day", "first_workable",
           "off_holiday"]
