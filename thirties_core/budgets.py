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
LONG_SESSION_MINUTES = 120  # a session this long goes on days off first, where there's one with room
CHILD = {"years": "year", "year": "quarter", "quarter": "month", "month": "week", "week": "day"}
# where a period looks for the budget it takes a share of: the nearest level above that has one
UP = {"day": ("week",), "week": ("month", "quarter", "year", "years"), "month": ("quarter", "year", "years"),
      "quarter": ("year", "years"), "year": ("years",)}


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
    order = {lv: i for i, lv in enumerate(("day", "week", "month", "quarter", "year", "years"))}
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
    due = _due(task, planner)
    if due is not None:  # a deadline inside the period pulls the pieces earlier: only the periods before it count
        if child.start > due:
            return 0
        rest = [c for c in rest if c.start <= due]
    return share_for(left, max(1, len(rest)))


def trickle_line(planner: "HorizonPlanner", task: "TaskItem", d: Optional[date] = None) -> str:
    """How an effort's budget trickles down to the day, worked out by Python: "6 sessions in Q4 2026 → 2 this
    month (October) → 1 this week → Thursday". Each level shows its own budget if it has one, else its share of
    the level above; the week ends in the days its sessions go on. "" with no budget."""
    from thirties_core.horizons import period_of
    d = d or planner.today()
    parts: List[str] = []
    for level in ("years", "year", "quarter", "month", "week"):
        own = budget_of(planner, task, d, level=level)
        if level == "years":  # a span of years has no fixed shape: only its own budget says which
            if own is None:
                continue
            p = own.period
        else:
            p = period_of(level, d, planner.week_start)
        if own is not None:
            n = own.sessions
        elif parts:
            n = share_this(planner, task, p)
            if n is None:
                continue
        else:
            continue  # nothing budgeted this high up yet
        label = planner.label(p)
        parts.append(f"{n} session{'s' if n != 1 else ''} " + (label if label.startswith(("this ", "next ")) else
                                                                f"in {label}"))
        if level == "week" and own is not None:
            days = session_days(planner, task, p)
            if days:
                parts.append(", ".join(planner.label(period_of("day", x)) for x in days))
    return " → ".join(parts)


def session_days(planner: "HorizonPlanner", task: "TaskItem", week: "Period") -> List[date]:
    """The days this week's budget puts a session of ``task`` on: Python's week plan, worked out afresh from the
    room each day has, so it bends as the week fills. Days that already have one count first; the rest of what's
    left goes on the days ahead with a free stretch long enough, spread out (one a day, as far apart as they can
    be), never past a due date. Calendar sense: something outdoors needs its stretch in daylight, and a long
    session goes on days off first (a weekend, or a holiday that took Work off)."""
    from thirties_core.replan import _free_minutes, is_day_off, outdoors
    b = budget_of(planner, task, week.start, level="week")
    if b is None:
        return []
    pt, today = planner.pt, planner.today()
    due = _due(task, planner)
    have, room, off = [], [], set()
    daylight = outdoors(task.title, pt)
    for d in week.days():
        if d < today or (due is not None and d > due):
            continue
        plan = pt.plan_for(d)
        if any(_same(s.title, task.title) and s.status in (SpanStatus.PLANNED, SpanStatus.DONE) for s in plan.spans):
            have.append(d)
        elif _free_minutes(pt, plan, b.minutes, daylight):
            room.append(d)
            if is_day_off(plan):
                off.add(d)
    k = min(b.left, len(room))
    if k <= 0:
        return have
    if b.minutes >= LONG_SESSION_MINUTES and off:  # long sessions: days off first, then the rest if needed
        rest = [d for d in room if d not in off]
        room = sorted(off) if len(off) >= k else sorted(off) + rest[:k - len(off)]
    if k == 1:
        picks = [room[0]]  # front-loaded: the first day with room
    else:
        picks = [room[round(i * (len(room) - 1) / (k - 1))] for i in range(k)]
    return sorted(set(have) | set(picks))


def _due(task, planner: Optional["HorizonPlanner"] = None) -> Optional[date]:
    """The last day work on ``task`` goes on: its deadline, or for a deadline that repeats, a few days before it
    (deadlines.work_by: its buffer)."""
    if planner is not None:
        from thirties_core.deadlines import work_by
        by = work_by(getattr(planner.pt.scheduler, "state_db", None) if planner.pt.scheduler else None, task)
        if by is not None:
            return by
    try:
        return date.fromisoformat(task.due[:10]) if getattr(task, "due", "") else None
    except ValueError:
        return None


__all__ = ["Budget", "budget_of", "soft_deadline", "share_for", "share_this", "periods_left", "count_sessions",
           "session_days", "trickle_line"]
