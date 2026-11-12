"""Meal windows: a meal (and a daily anchor) has a sensible window, and resists being pushed out of it.

A late meal isn't wrong, only late. So a meal isn't held to a hard latest time; it has a window (lunch about
11:30 to 2:00) and stays inside it when the day is rearranged:

- a redraw places meals first, at the earliest free stretch inside their window, and fits everything else around
  them; only when the window has no room left (the day is genuinely full) does a meal give way, to the first room
  after it, and the reply says so ("Lunch is late today: …");
- a move the user didn't ask for (pushing something else into lunch, a cascade of moves) never takes a meal past
  its window; the user naming the meal is what lets it go later.

A daily anchor at a usual time (the cat's dinner at 6) has a window around that time. Meals with someone ("lunch
with Sam") are appointments, not meals here (kinds.MEAL_RE vs _MEAL_WITH_RE).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Optional, Tuple

# (opens, closes): where the meal sensibly goes. The further past "closes", the less sensible.
MEAL_WINDOWS = {
    "breakfast": (time(6, 0), time(10, 30)),
    "brunch": (time(9, 30), time(13, 0)),
    "lunch": (time(11, 30), time(14, 0)),
    "dinner": (time(17, 0), time(20, 30)),
    "supper": (time(17, 0), time(20, 30)),
}
ANCHOR_BEFORE = timedelta(hours=1)        # a daily anchor's window around its usual time
ANCHOR_AFTER = timedelta(minutes=90)
_MEAL_WORD = re.compile(r"\b(breakfast|brunch|lunch|dinner|supper)\b", re.IGNORECASE)


def meal_word(title: str) -> Optional[str]:
    """The meal a title is (``None`` for a meal with someone, which is an appointment)."""
    from thirties_core.kinds import _MEAL_WITH_RE
    m = _MEAL_WORD.search(title or "")
    if m is None or _MEAL_WITH_RE.search(title or ""):
        return None
    return m.group(1).lower()


def window(title: str, day_start: datetime, usual: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
    """The item's window on the day starting at ``day_start``: a meal's own, else (for an anchor with a usual time)
    around that time; None for anything else."""
    word = meal_word(title)
    if word is not None:
        opens, closes = MEAL_WINDOWS[word]
        d = day_start.date()
        return (datetime.combine(d, opens, tzinfo=day_start.tzinfo), datetime.combine(d, closes, tzinfo=day_start.tzinfo))
    if usual is not None:
        return usual - ANCHOR_BEFORE, usual + ANCHOR_AFTER
    return None


def late_line(title: str, closes: datetime, why: str) -> str:
    """The reply's word when a meal gives way: "Lunch is late today: no free half hour before 2:00."."""
    from thirties_core.clock import format_clock
    return f"{title} is late today: {why} before {format_clock(closes)}."


__all__ = ["MEAL_WINDOWS", "meal_word", "window", "late_line"]
