"""Where a missed item goes next: Thirties decides, so the user never has to ask "should I get to this tomorrow?".

The first piece of the trickle-down planner, at the day and week levels. Each missed item is sorted by kind (most
of it by Python, kinds.py and carry.py):

- a one-time event, or anything fixed to a time: it ends (carry.py already ends it);
- a routine or a meal: moot, nothing rolls over (carry.py already ends it);
- a one-off task: the first day from tomorrow with room for it ("mail the warranty card": tomorrow; a
  reading session with the next two days full: the day after those), never past its deadline;
- time on something ongoing (a project): the missed time is spread over its next sessions this week, an extra
  half hour or hour each where there's room, else a session on the first day with room.

The result is a concrete day (and for ongoing time, the sessions it was added to), which carry.py records on the
leftover so that day's morning puts it on the plan. The model judges only what Python can't (an unclear kind).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple

WEEK_DAYS = 7          # how far a one-off without a deadline is looked ahead (the week level)
SPREAD_SESSIONS = 2    # missed time on something ongoing goes over at most this many of its next sessions
STEP = 30              # minutes


@dataclass
class Placement:
    day: Optional[date]
    why: str
    extended: List[str] = field(default_factory=list)  # ongoing: the sessions (span ids) that got the time


def _free_minutes(pt, plan, minutes: int) -> bool:
    """The day has one free stretch of ``minutes`` before bedtime."""
    from thirties_core.if_time import _waking_end
    end = _waking_end(plan)
    need = timedelta(minutes=minutes)
    for a, b in pt.open_windows_for(plan):
        if min(b, end) - a >= need:
            return True
    return False


def first_day_with_room(pt, minutes: int, start: date, last: date) -> Optional[date]:
    d = start
    while d <= last:
        try:
            plan = pt.plan_for(d)
        except Exception:
            plan = None
        if plan is not None and _free_minutes(pt, plan, minutes):
            return d
        d += timedelta(days=1)
    return None


def _day_word(d: date, today: date) -> str:
    gap = (d - today).days
    if gap == 1:
        return "tomorrow"
    if gap < 7:
        return d.strftime("%A")
    return d.strftime("%a %b %-d")


def place_once(pt, title: str, minutes: int, today: date, due: Optional[date] = None) -> Placement:
    """A missed one-off task: the first day from tomorrow with room, never past its deadline."""
    last = due if due is not None else today + timedelta(days=WEEK_DAYS)
    d = first_day_with_room(pt, minutes, today + timedelta(days=1), last)
    if d is None:
        if due is not None:
            return Placement(None, f"no day before it's due ({due.strftime('%a %b %-d')}) has room")
        return Placement(None, "no day this week has room")
    why = _day_word(d, today)
    if d != today + timedelta(days=1):
        why += " (the days before are full)"
    return Placement(d, why)


def spread_ongoing(pt, title: str, minutes: int, today: date) -> Placement:
    """Missed time on something ongoing: added to its next sessions this week where there's room (an extra half
    hour or hour each), else a session on the first day with room."""
    from thirties_core.models import SpanStatus
    left = max(STEP, int(round(minutes / STEP)) * STEP)
    extended: List[str] = []
    per = max(STEP, int(round(left / SPREAD_SESSIONS / STEP)) * STEP)
    d = today + timedelta(days=1)
    while d <= today + timedelta(days=WEEK_DAYS) and left > 0 and len(extended) < SPREAD_SESSIONS:
        plan = pt.plan_for(d)
        sessions = [s for s in plan.spans if s.status == SpanStatus.PLANNED and s.title.strip().lower() ==
                    title.strip().lower()]
        for s in sessions:
            add = min(per, left)
            if _extend(pt, plan, s, add):
                extended.append(s.id)
                left -= add
                if left <= 0 or len(extended) >= SPREAD_SESSIONS:
                    break
        d += timedelta(days=1)
    if extended and left <= 0:
        return Placement(None, f"its next {len(extended)} session{'s' if len(extended) > 1 else ''} got the time",
                         extended)
    rest = place_once(pt, title, left, today)
    if extended:
        rest.why = f"{len(extended)} session{'s' if len(extended) > 1 else ''} got some of the time; the rest {rest.why}"
        rest.extended = extended
    return rest


def _extend(pt, plan, span, minutes: int) -> bool:
    """Lengthen a planned session by ``minutes`` if the time after it is free."""
    new_end = span.end_dt + timedelta(minutes=minutes)
    clash = [o for o in plan.spans if o.id != span.id and not o.is_envelope and o.status.value == "planned"
             and o.start_dt < new_end and o.end_dt > span.end_dt]
    if clash:
        return False
    saved = pt.day_plan
    pt.day_plan = plan
    try:
        out = pt.update_event(event=span.id, end=new_end.strftime("%I:%M %p"))
    except Exception:
        return False
    finally:
        pt.day_plan = saved
    return not str(out).startswith("NOT_DONE")


def replan(assistant, title: str, kind: str, minutes: int, today: date, due: Optional[date] = None) -> Placement:
    """Where a missed item goes next (kinds.py: once, ongoing, daily)."""
    from thirties_core.kinds import DAILY, ONGOING
    pt = assistant.pt
    if kind == DAILY:
        return Placement(None, "a daily thing: today's simply ends")
    if kind == ONGOING:
        return spread_ongoing(pt, title, minutes, today)
    return place_once(pt, title, minutes, today, due)


__all__ = ["Placement", "replan", "place_once", "spread_ongoing", "first_day_with_room"]
