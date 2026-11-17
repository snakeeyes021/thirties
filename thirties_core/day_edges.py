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

Something under way at this moment is never refused for being early. Up at 5:50 and out feeding the hens while the
plan starts at 7:00, it's recorded on today in the block it's happening in (the 5:30 AM block), started now; the
plan's first block stays where it is. Such a record of this morning, before the plan begins, is part of the day:
it's loaded with the day's plan and listed first in its record by blocks (``this_mornings``).
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


# --- what's happening now, before the plan begins ---

# a clause saying the user is at it right now: "out on a run now", "I'm feeding the hens right now"
_NOW_RE = re.compile(r"\b(?:right now|now|currently|at the moment|as we speak)\b", re.IGNORECASE)
_AT_IT_RE = re.compile(r"\b(?:i'?m|i am|im|we'?re|we are|currently|just)\s+(?:\w+\s+){0,2}?\w+ing\b|"
                       r"\b\w{2,}ing\s+(?:\w+\s+){0,3}?(?:right now|now)\b|"
                       r"^\W*(?:out|off|busy|in the middle of)\b", re.IGNORECASE)
_CLAUSES_RE = re.compile(r"[.;!?\n,]+|\b(?:and|but|so|then|while)\b", re.IGNORECASE)


def says_doing_now(message: Optional[str]) -> bool:
    """The user's words say they're doing something right now ("out on a run now", "I'm sorting the mail right now")."""
    from thirties_core.started import STARTED_RE, _asks
    for clause in _CLAUSES_RE.split(message or ""):
        if clause.strip() and not _asks(clause) and _NOW_RE.search(clause) and (
                _AT_IT_RE.search(clause) or STARTED_RE.search(clause)):
            return True
    return False


def before_the_plan(pt: "PlanTools") -> bool:
    """It's this morning, before the day's plan begins, and the plan is the day being lived."""
    plan = pt.day_plan
    try:
        now = pt._now()
        return pt.is_current(plan) and plan.day_start - NEAR_NOW <= now < plan.day_start
    except Exception:
        return False


def now_this_morning(pt: "PlanTools", dt: datetime) -> bool:
    """dt, read as this morning, is in the block it is now, before the plan begins, and the user's words say they're
    at it now: what they're doing right now, to be recorded as happening now (doing_now), never refused."""
    if not before_the_plan(pt) or not says_doing_now(getattr(pt, "user_message", None)):
        return False
    from thirties_core.clock import thirty_of
    now = pt._now()
    when = this_morning_meant(pt, dt) or dt
    return thirty_of(now) <= when <= now + timedelta(minutes=5)


def _morning_from(plan) -> datetime:
    start = plan.day_start
    return start.replace(hour=MORNING_FROM, minute=0, second=0, microsecond=0)


def is_this_mornings(plan, span: "EventSpan") -> bool:
    """A record of this morning before the plan begins: something the user did, or is doing, between MORNING_FROM
    and the plan's first block, on the plan's own date (never sleep, work or a calendar event); or something planned
    there at a time the user gave for it (stated_times.this_morning_ahead)."""
    from thirties_core.models import SpanCategory, SpanStatus
    theirs = span.status == SpanStatus.PLANNED and span.given_start is not None and \
        span.given_start.timestamp() == span.start_dt.timestamp()
    return ((span.actual_start is not None or theirs) and span.source != "calendar"
            and span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
            and _morning_from(plan) <= span.start_dt < plan.day_start
            and span.end_dt.timestamp() <= plan.day_start.timestamp())


def this_mornings(plan) -> list:
    """This morning's records before the plan begins that the plan holds (``is_this_mornings``), in order."""
    return sorted((s for s in plan.spans if is_this_mornings(plan, s)), key=lambda s: s.start_dt.timestamp())


def load_this_mornings(db, plan) -> None:
    """Load this morning's records from before the plan begins into the day (build_day_plan): they belong to it."""
    if db is None or _morning_from(plan) >= plan.day_start:
        return
    held = {s.id for s in plan.spans}
    plan.spans.extend(s for s in db.get_spans_between(_morning_from(plan), plan.day_start)
                      if s.id not in held and is_this_mornings(plan, s))


def day_note(pt: "PlanTools", span: "EventSpan") -> str:
    """" (tomorrow morning, Thursday)" for an item that lands on the next morning, so the result says the real day."""
    try:
        if next_morning(pt, span.start_dt):
            return f" (tomorrow morning, {span.start_dt.strftime('%A')})"
    except Exception:
        pass
    return ""


__all__ = ["this_morning_meant", "refuse_early", "refuse_off_the_day", "day_note", "next_morning", "says_doing_now",
           "before_the_plan", "now_this_morning", "is_this_mornings", "this_mornings", "load_this_mornings"]
