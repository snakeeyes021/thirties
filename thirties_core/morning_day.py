"""The morning hands over the whole day, and what the user asked for that day is on it.

- **The whole day** (11-07: a morning message left the routines out): Python's own
  morning lists it (day_lists.planned_ahead_text); the model's morning reply is checked for each routine step and
  anchor still to come, sent back once if it leaves some out, and given the day's lines if it still does.
- **Asked for, so on it** (10-30, 10-31: an item the user wanted to be sure of on a day wasn't offered): an item pinned to the
  day goes on the day's plan before the morning is written, in the first free time, so it can't be left out.
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def place_pinned(assistant: Any) -> List[str]:
    """Items pinned to today that aren't on its plan yet: each placed now. Returns "Title at 3:00 PM" for each."""
    pt = assistant.pt
    if pt.scheduler is None or not pt.is_current(pt.day_plan):
        return []
    from thirties_core import if_time
    from thirties_core.clock import format_clock
    from thirties_core.horizons import candidates_for_day
    try:
        pinned = [c for c in candidates_for_day(pt, pt.day_plan.target_date, limit=None) if c.source == "pinned"]
    except Exception as e:  # the morning never fails for want of the plans
        logger.warning("Couldn't read what's pinned to today: %s", e)
        return []
    out = []
    for c in pinned:
        try:
            spot, _ = if_time.place(assistant, c.title, c.task)
        except Exception as e:
            logger.warning("Couldn't place %s: %s", c.title, e)
            continue
        if spot is not None:
            out.append(f"{c.title} at {format_clock(spot)}")
    return out


MARKERS = ("[Morning, not from the user]", "This is the user's first message of the day, and the day is being built")


def is_morning_turn(brain: Any, turn_start: int) -> bool:
    """The turn is the day's morning (the morning instruction, or the first message of the day built in its reply)."""
    for m in brain.messages[max(0, turn_start - 3):]:
        if m.get("role") == "user" and any(k in str(m.get("content") or "") for k in MARKERS):
            return True
    return False


def left_out(brain: Any, reply: str) -> List[str]:
    """What a morning reply leaves out of the day: each routine part (by its first step) and each anchor still to
    come today that the reply doesn't name."""
    from thirties_core.item_facts import mentions
    from thirties_core.models import SpanStatus
    assistant = getattr(brain.tools, "assistant", None)
    if assistant is None:
        return []
    pt = brain.tools
    now = pt._now()
    daily = assistant.routines.daily_titles()
    titles = [s.title for s in pt.day_plan.spans if not s.is_envelope]
    out = []
    for s in sorted(pt.day_plan.spans, key=lambda s: s.start_dt):
        if s.is_envelope or s.status != SpanStatus.PLANNED or s.end_dt <= now:
            continue
        if s.title.strip().lower() in daily and not mentions(reply, s.title, titles):
            out.append(s.title)
    return out


def missing_line(assistant: Any) -> str:
    """Python's lines for the day, added under a morning reply that still omits routine steps or anchors."""
    from thirties_core.day_lists import planned_ahead_text
    try:
        return planned_ahead_text(assistant)
    except Exception:
        return ""


__all__ = ["place_pinned", "is_morning_turn", "left_out", "missing_line"]
