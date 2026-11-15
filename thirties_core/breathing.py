"""Breathing room: the day isn't packed tight.

Finishing one thing doesn't mean being ready for the next at once, and items packed back to back pile up the
moment one runs long. So wherever Thirties picks the time itself:

- nothing starts the moment Work ends: the first half hour after it stays clear (``AFTER_WORK``);
- a long run of items back to back gets a free thirty after it (``Pace``: after ``STRETCH`` of them).

A time the user gives is theirs: these never move what the user put somewhere, only where Thirties places things
(open time, slot options, the redraw, the day's proposal).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

AFTER_WORK = timedelta(minutes=30)   # clear after Work ends
STRETCH = timedelta(minutes=90)      # this much back to back...
SLACK = timedelta(minutes=30)        # ...and then this much free


def after_work(plan: DayPlan) -> List[Tuple[datetime, datetime]]:
    """The stretches kept clear after Work ends, on this day's canvas."""
    out = []
    for s in plan.spans:
        if s.category == SpanCategory.WORK and s.status != SpanStatus.SKIPPED and not s.tentative \
                and plan.day_start < s.end_dt < plan.day_end:
            out.append((s.end_dt, min(s.end_dt + AFTER_WORK, plan.day_end)))
    return out


def clip(plan: DayPlan, windows: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """Free stretches (whole thirties) as Thirties may fill them: without the clear time after Work. The free time
    the user is told about stays as it is; only where Thirties places things leaves the room."""
    out = list(windows)
    for a, b in after_work(plan):
        clipped = []
        for w0, w1 in out:
            if a < w1 and w0 < b:
                if w0 < a:
                    clipped.append((w0, a))
                if b < w1:
                    clipped.append((b, w1))
            else:
                clipped.append((w0, w1))
        out = [(w0, w1) for w0, w1 in clipped if w1 - w0 >= timedelta(minutes=30)]
    return out


def gap_spans(plan: DayPlan, after: Optional[datetime] = None) -> List[EventSpan]:
    """The clear time after Work as stand-in items (never stored), for the redraw to place around."""
    return [EventSpan(f"breathing-{i}", "Breathing room", a, b, SpanCategory.ACTIVITY, source="breathing")
            for i, (a, b) in enumerate(after_work(plan)) if after is None or b > after]


class Pace:
    """Where the next item may start in a run Thirties lays out: right after the last, except after a long stretch
    back to back, which gets a free thirty."""

    def __init__(self) -> None:
        self.run_start: Optional[datetime] = None
        self.run_end: Optional[datetime] = None

    def next(self, start: datetime, end: datetime) -> datetime:
        if self.run_end is None or start > self.run_end:
            self.run_start = start
        self.run_end = max(end, self.run_end or end)
        if self.run_end - self.run_start >= STRETCH:
            self.run_start = None
            out, self.run_end = self.run_end + SLACK, None
            return out
        return self.run_end


__all__ = ["AFTER_WORK", "STRETCH", "SLACK", "after_work", "clip", "gap_spans", "Pace"]
