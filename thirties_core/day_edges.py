"""Times at the edges of a day's plan: an early-morning time never lands on the wrong day without saying so.

A day's plan runs from the thirty its sunrise falls in to the same time the next morning. A user who is up before
that (out with the dog at 6:15 when the plan starts at 7:00) names a time that is this morning, before the plan
begins; read on the plan, that clock time is the next morning's, at the plan's very end. Moving an item there
moved it to tomorrow while the tool said "6:15 AM", and shifting it to before the plan's start took it off the day
altogether: the item vanished from the schedule but was still "already planned" when added again.

Now:

- an early-morning time while that morning is now (within a few hours) is refused with the facts: this morning
  is before the plan begins, and on the plan it would be tomorrow; unless the user's words say tomorrow;
- a change that would leave the day's plan altogether is refused;
- a result that does land on the next morning says so ("tomorrow morning, Thursday").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from thirties_core.clock import format_clock

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# how near "now" this morning's time must be for "6:15" to mean this morning
NEAR_NOW = timedelta(hours=3)
# from this hour on, a time after midnight is the next morning, not late tonight
MORNING_FROM = 4
TOMORROW_RE = re.compile(r"\b(?:tomorrow|tmrw|tmr|tomorow|next morning)\b", re.IGNORECASE)


def next_morning(pt: "PlanTools", dt: datetime) -> bool:
    """dt is on the next calendar morning, in the plan's last hours (after MORNING_FROM, before its sunrise)."""
    start = pt.day_plan.day_start
    local = dt.astimezone(start.tzinfo)
    return local.date() > start.date() and MORNING_FROM <= local.hour and local < pt.day_plan.day_end


def this_morning_meant(pt: "PlanTools", dt: datetime) -> Optional[datetime]:
    """The same clock time this morning, when that's what the user means: the plan is the day being lived, its
    morning is now, and the user's words don't say tomorrow. None otherwise."""
    if not next_morning(pt, dt):
        return None
    try:
        if not pt.is_current(pt.day_plan):
            return None
        now = pt._now()
    except Exception:
        return None
    message = getattr(pt, "user_message", None) or ""
    if TOMORROW_RE.search(message):
        return None
    earlier = dt - timedelta(days=1)
    return earlier if abs(now - earlier) <= NEAR_NOW else None


def refuse_early(pt: "PlanTools", title: str, dt: datetime) -> Optional[str]:
    """Why an item can't go at dt: the user means this morning, before the plan begins. None when it can."""
    earlier = this_morning_meant(pt, dt)
    if earlier is None:
        return None
    begins = pt.day_plan.day_start
    return (f"{format_clock(earlier)} this morning is before today's plan begins ({format_clock(begins)}), so "
            f"{title} can't go there; on this plan {format_clock(dt)} is tomorrow morning "
            f"({dt.strftime('%A')}). Put it at {format_clock(begins)} or later; if it's already over, record it "
            "with happened=true; if it's for tomorrow morning, say tomorrow.")


def refuse_off_the_day(pt: "PlanTools", title: str, start: datetime, end: datetime) -> Optional[str]:
    """Why a change can't leave the day's plan altogether (it would vanish from it), or None."""
    plan = pt.day_plan
    if end <= plan.day_start or start >= plan.day_end:
        return (f"That would put {title} at {format_clock(start)}, outside this day's plan "
                f"({format_clock(plan.day_start)} to {format_clock(plan.day_end)} the next morning), so it would "
                "drop off it. Keep it within the day, or move it to another day with to_day.")
    return None


def day_note(pt: "PlanTools", span: "EventSpan") -> str:
    """" (tomorrow morning, Thursday)" for an item that lands on the next morning, so the result says the real day."""
    try:
        if next_morning(pt, span.start_dt):
            return f" (tomorrow morning, {span.start_dt.strftime('%A')})"
    except Exception:
        pass
    return ""


__all__ = ["this_morning_meant", "refuse_early", "refuse_off_the_day", "day_note", "next_morning"]
