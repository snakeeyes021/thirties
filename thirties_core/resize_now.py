"""An item that has not been started, whose planned block has already begun, is resized from now.

Seen in simulated days: the user asked for an item planned at a block that had begun, but that they hadn't started,
to take four blocks. The item was stretched from its old start, ran over the next item, and kept its start in the
past. A thing that hasn't been started can't have been running since its planned block, and two things don't run at
once, so the new length counts from the current block and what follows is pushed back.

What Python supplies (AGENTS.md §3: Python states the facts and does the arithmetic; the model reads what the user
meant):

- **The fact.** Wherever the model reads the item (the schedule, the update_event result): "planned from 10:30 AM,
  which has begun; nothing says it started", and what comes next (``fact``). "Under way" is the plan's own status
  (under_way.real_start, or still going: in_progress.py), never this fact's guess.
- **The rule.** When the model gives an item that is planned, not under way and not done a new length (and no start
  or end of its own), and its planned start has passed, the length counts from the current block (``new_start``).
  A start the model passes wins; an item under way keeps the start it began at (user_placed.start_refused).
- **What follows.** Anything that would now overlap is pushed back the way a push is (PlanTools._push_following):
  items that can't move (a fixed item, a calendar event, an anchor) are never moved; the resize that would run into
  one is refused and says which, and nothing changes. Never two flexible items at once unless the user said so.
- **The result says what moved** (``moved_line``): "True the rear hub now 2:00 PM – 4:00 PM; Sort the cable bins moved to 4:00 PM".

Only the model's own call for the user's message is held to this: the app's own controls and the background keep the
times they give.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from thirties_core.clock import format_clock, thirty_of

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools


def not_started(span: "EventSpan") -> bool:
    """Still on the plan, unmarked, with no recorded start and no sign it is going on."""
    from thirties_core.models import SpanStatus
    return (span.status == SpanStatus.PLANNED and not span.is_envelope and not span.tentative
            and not getattr(span, "shown_only", False) and span.source != "calendar"
            and span.actual_start is None and getattr(span, "actual_end", None) is None
            and span.time_source not in ("running", "auto_done"))


def begun(pt: "PlanTools", span: "EventSpan") -> bool:
    """An unmarked item with no recorded start, planned from a time that has passed, on the day being lived."""
    if not not_started(span) or not pt.is_current(pt.day_plan) or span.start_dt >= pt._now():
        return False
    return not pt._is_fixed(span)


def following(pt: "PlanTools", span: "EventSpan") -> Optional["EventSpan"]:
    """The next planned item after ``span`` on the plan, or None."""
    from thirties_core.models import SpanStatus
    after = [s for s in pt.day_plan.active_spans()
             if s.id != span.id and not s.is_envelope and s.status == SpanStatus.PLANNED
             and not getattr(s, "shown_only", False) and s.start_dt >= span.start_dt
             and (s.start_dt, s.end_dt) != (span.start_dt, span.end_dt)]
    return min(after, key=lambda s: (s.start_dt, s.end_dt), default=None)


def fact(pt: "PlanTools", span: "EventSpan") -> str:
    """"planned from 10:30 AM, which has begun; nothing says it started; next: Count the spares from 11:00 AM", or ""
    when it doesn't apply (it isn't the day's, or something says it started). Blocks only."""
    if not begun(pt, span):
        return ""
    text = f"planned from {format_clock(thirty_of(span.start_dt))}, which has begun; nothing says it started"
    nxt = following(pt, span)
    if nxt is not None:
        text += f"; next: {pt._title(nxt)} from {format_clock(thirty_of(nxt.start_dt))}"
    return text


def new_start(pt: "PlanTools", span: "EventSpan", start: Any, end: Any, shift: Any, duration: Any,
              title: Any) -> Optional[datetime]:
    """Where an item given only a new length starts: the current block, when nothing says it started and its planned
    start has passed. None when this doesn't apply (the model gave a start, an end or a shift; or the item is under
    way, over, fixed or not today's; or it isn't the model's call)."""
    if duration in (None, "") or any(v not in (None, "") for v in (start, end, shift)) or title:
        return None
    from thirties_core.move_scope import _talking
    if _talking(pt) is None or not begun(pt, span):
        return None
    return max(thirty_of(pt._now()), span.start_dt)


def together(pt: "PlanTools") -> bool:
    """The user's words say the items happen together: nothing is pushed for the new length."""
    from thirties_core.sharing import said_together
    return said_together(getattr(pt, "user_message", None))


def refuse_fixed(pt: "PlanTools", span: "EventSpan", start: datetime, end: datetime) -> Optional[str]:
    """Why the item can't take this time from now: it would run into something that can't move; None when it's clear.
    Nothing is changed when this says why."""
    from thirties_core.models import EventSpan
    probe = EventSpan(span.id, span.title, start, end, span.category, source=span.source, task_id=span.task_id,
                      status=span.status)
    hard = pt._conflicts(probe)
    if not hard:
        return None
    first = min(hard, key=lambda s: s.start_dt)
    return (f"{pt._title(span)} hasn't been started, so its new length counts from {format_clock(start)}, which would "
            f"run it into {pt._title(first)} ({format_clock(first.start_dt)}), which can't move. Nothing was "
            f"changed: tell the user it doesn't fit before {pt._title(first)}, and ask what they'd like.")


def moved_line(pt: "PlanTools", span: "EventSpan", old_start: datetime, pushed: List[str], sharing: List[str]) -> str:
    """The line the model reads: where the item now runs, and what moved."""
    new = f"{pt._title(span)} hadn't been started, so it now runs {format_clock(span.start_dt)} – " \
          f"{format_clock(span.end_dt)} (it was planned from {format_clock(thirty_of(old_start))})."
    if pushed:
        new += " " + "; ".join(pushed) + "."
    if sharing:
        new += " " + "; ".join(f"it shares time with {t}" for t in sharing) + "."
    return new


__all__ = ["begun", "fact", "following", "moved_line", "new_start", "not_started", "refuse_fixed", "together"]
