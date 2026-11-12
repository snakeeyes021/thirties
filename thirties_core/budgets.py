"""Effort budgets: how much time an ongoing effort gets in a period, and what's left of it.

The trickle-down planner's arithmetic (Python's side of it). A week, month, quarter or year plan can say how much
an effort gets there ("Shed clear-out: 6 sessions this quarter", "2 sessions this week"), stored on its pin
(horizons.py). From that and what actually happened, Python works out:

- what's left of a budget: sessions budgeted, less the ones done in the period, less the ones already planned
  ahead in it;
- a level's share for the next level down: what's left, spread evenly over the periods left in it (6 left this
  quarter with 2 months to go: 3 this month);
- a deadline pulls the pieces earlier: with a due date inside the period, what's left is spread only over the
  periods before it (and none after);
- a soft deadline: the end of the period a budget belongs to. A missed session is re-spread within what's left of
  that period if there's room; if there isn't, it's let go at that level, and the next level up has one more
  session left, so its next share grows.

The model proposes budgets and splits; it never counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, List, Optional

from thirties_core.models import SpanStatus

if TYPE_CHECKING:
    from thirties_core.horizons import HorizonPlanner, Period
    from thirties_core.models import TaskItem

DEFAULT_SESSION_MINUTES = 60
CHILD = {"year": "quarter", "quarter": "month", "month": "week", "week": "day"}
UP = {"day": ("week",), "week": ("month",), "month": ("quarter", "year"), "quarter": ("year",)}


def budget_period(r, week_start: int = 0) -> "Period":
    """The period a plan row's budget covers: a quarter of a year plan when it's slotted there, else the row's own."""
    from thirties_core.horizons import period_of, row_period
    if r["level"] == "year" and r.get("slot"):
        return period_of("quarter", date.fromisoformat(r["slot"]), week_start)
    return row_period(r, week_start)


@dataclass
class Budget:
    title: str
    period: "Period"
    sessions: int
    minutes: int          # per session
    done: int = 0
    planned: int = 0      # planned ahead (today and later) within the period

    @property
    def left(self) -> int:
        """Sessions still to find a place for."""
        return max(0, self.sessions - self.done - self.planned)

    def line(self) -> str:
        return (f"{self.sessions} session{'s' if self.sessions != 1 else ''} of {self.minutes} min: {self.done} done, "
                f"{self.planned} planned, {self.left} left")


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def count_sessions(pt, title: str, start: date, end: date, today: date) -> tuple:
    """(done, planned ahead) sessions of ``title`` on the days start..end: done ones anywhere in it, planned ones
    from today on."""
    try:
        a = pt.plan_for(start).day_start
        b = pt.plan_for(end).day_end
    except Exception:
        return 0, 0
    if end < today:
        ahead_from = None                      # all over: nothing planned ahead counts
    elif start > today:
        ahead_from = a                         # all ahead
    else:
        ahead_from = pt.plan_for(today).day_start
    done = planned = 0
    for s in pt.scheduler.state_db.get_spans_between(a, b):
        if not _same(s.title, title):
            continue
        if s.status == SpanStatus.DONE:
            done += 1
        elif s.status == SpanStatus.PLANNED and ahead_from is not None and s.start_dt >= ahead_from:
            planned += 1
    return done, planned


def budget_of(planner: "HorizonPlanner", task: "TaskItem", d: date, level: Optional[str] = None
              ) -> Optional[Budget]:
    """The finest budget pinned for ``task`` whose period holds day ``d`` (or the one at ``level``), with what
    happened in it."""
    order = {lv: i for i, lv in enumerate(("day", "week", "month", "quarter", "year"))}
    best = None
    for r in planner.plans.rows(item_id=task.id):
        if not int(r.get("sessions") or 0):
            continue
        p = budget_period(r, planner.week_start)
        if not p.contains(d) or (level and p.level != level):
            continue
        if best is None or order.get(p.level, 9) < order.get(best[0].level, 9):
            best = (p, r)
    if best is None:
        return None
    p, r = best
    done, planned = count_sessions(planner.pt, task.title, p.start, p.end, planner.today())
    return Budget(task.title, p, int(r["sessions"]), int(r.get("minutes") or 0) or DEFAULT_SESSION_MINUTES,
                  done, planned)


def soft_deadline(planner: "HorizonPlanner", task: "TaskItem", d: date) -> Optional[date]:
    """The end of the finest budgeted period holding day ``d``: missed time is re-spread before it, or let go."""
    b = budget_of(planner, task, d)
    return b.period.end if b is not None else None


def share_for(left: int, periods_left: int) -> int:
    """A level's share for one period of the next level down: what's left, spread evenly (rounded up, so the
    effort is front-loaded rather than left to the last period)."""
    if left <= 0 or periods_left <= 0:
        return 0
    return math.ceil(left / periods_left)


def periods_left(p: "Period", child_level: str, today: date, week_start: int = 0) -> List["Period"]:
    """The periods of ``child_level`` inside ``p`` that aren't over (a week counts if it starts in ``p``)."""
    from thirties_core.horizons import period_of
    out = []
    d = max(p.start, today)
    while d <= p.end:
        c = period_of(child_level, d, week_start)
        if c.end >= today and (c.start >= p.start or c.start <= d) and c not in out:
            out.append(c)
        d = c.end + timedelta(days=1)
    return out


def share_this(planner: "HorizonPlanner", task: "TaskItem", child: "Period") -> Optional[int]:
    """What ``task``'s budget one level up gives the period ``child`` (a month's budget for this week, say)."""
    up = None
    for parent_level in UP.get(child.level, ()):  # the nearest level up with a budget (a month: its quarter, else
        up = budget_of(planner, task, child.start, level=parent_level) or \
            budget_of(planner, task, child.end, level=parent_level)  # the year)
        if up is not None:
            break
    if up is None:
        return None
    # what's already done or planned inside this child period counts toward its share
    done_here, planned_here = count_sessions(planner.pt, task.title, child.start, child.end, planner.today())
    left = up.left + done_here + planned_here
    rest = periods_left(up.period, child.level, max(planner.today(), child.start), planner.week_start)
    due = _due(task)
    if due is not None:  # a deadline inside the period pulls the pieces earlier: only the periods before it count
        if child.start > due:
            return 0
        rest = [c for c in rest if c.start <= due]
    return share_for(left, max(1, len(rest)))


def _due(task) -> Optional[date]:
    try:
        return date.fromisoformat(task.due[:10]) if getattr(task, "due", "") else None
    except ValueError:
        return None


__all__ = ["Budget", "budget_of", "soft_deadline", "share_for", "share_this", "periods_left", "count_sessions"]
