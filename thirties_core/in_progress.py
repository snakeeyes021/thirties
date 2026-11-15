"""Still doing it: an item the user is in the middle of, and one that finishes by itself after a length the user
gave.

The user says "still in the shower, out in five": the item is **in progress**. It isn't done (starting isn't
finishing), it isn't late, and it isn't skipped: it stretches to when it will really end, and what comes after it
moves along only as far as it has to (placement.make_way: fixed things and what's inside them stay, meals stay
roughly in place, a gap later absorbs it). While it's going, a time that passes never ends it: each time the clock
passes its end with nothing said, it stretches to the end of the current thirty (``tick``), up to a limit, so the
plan follows what's really happening.

"It'll take three hours, mark it done then" makes it **finish by itself**: at its end it's logged done, with the
times the user gave, unless they said otherwise first.

The state lives on the item itself, so it syncs with it: ``actual_start`` is when it started, ``actual_end`` stays
empty, and ``time_source`` is ``running`` (in progress) or ``auto_done`` (finishes by itself at its end); the plan it
had is kept in ``planned_start`` / ``planned_end``. Logging an outcome ends the state (log_outcome sets its own
time_source).

Other modules call ``start_now`` (the user says they're doing something now, before its time), ``still_doing`` (it
runs long) and ``tick`` (each check: the background service and each turn, via the assistant's settle_moot).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import logging

from thirties_core.clock import format_clock, format_duration, thirty_ceil, thirty_of
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

logger = logging.getLogger(__name__)

RUNNING, AUTO_DONE = "running", "auto_done"
ALMOST_DONE = timedelta(minutes=10)    # "done soon" with less than this left in the thirty: it runs into the next one
# How far an item in progress stretches by itself, past its planned end, before it's left for the user to say (it
# then shows as not marked yet): twice its planned length, and never more than two hours more.
MAX_OVERRUN = timedelta(hours=2)

IN_PROGRESS_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "still_doing",
    "description": "The user is in the middle of something on the plan (\"still in the shower\", \"I'm doing the dishes "
                   "now\", \"not done yet\"): it's in progress, not done. It stretches to when it will end and what "
                   "follows moves along only as far as it has to (fixed things and meals stay). Also for \"mark it "
                   "done by itself in 3 hours\" (auto_done). Never log_outcome for something still going.",
    "parameters": {"event": "The item's id or title.",
                   "until": "When they expect to finish (a clock time), if said.",
                   "length": "How much longer, or how long it takes in all from when it started (e.g. '3 hours'), "
                             "if said.",
                   "auto_done": "true when the user says to mark it done by itself at that time."},
    "required": ["event"],
}]


def is_running(span: EventSpan) -> bool:
    return span.status == SpanStatus.PLANNED and span.time_source in (RUNNING, AUTO_DONE)


def in_progress(plan) -> List[EventSpan]:
    return [s for s in plan.spans if is_running(s)]


def _keep_plan(span: EventSpan) -> None:
    if span.planned_start is None:
        span.planned_start, span.planned_end = span.start_dt, span.end_dt


def _followers(tools: "PlanTools", plan, span: EventSpan, was_end: datetime) -> List[EventSpan]:
    """What may have to move along: the flexible items from where it started on (not itself, not other things in
    progress)."""
    from thirties_core.placement import flexible
    return sorted((s for s in plan.active_spans() if s.id != span.id and not is_running(s)
                   and flexible(tools, plan, s) and s.start_dt >= span.start_dt),
                  key=lambda s: s.start_dt)


def _stretch(tools: "PlanTools", plan, span: EventSpan, new_end: datetime, why: str) -> Tuple[List[EventSpan], Any]:
    """Make ``span`` end at ``new_end`` and move what follows along. Returns (moved, flow)."""
    from thirties_core.placement import apply_flow, make_way
    was = (span.start_dt, span.end_dt)
    span.end_dt = new_end
    chain = _followers(tools, plan, span, was[1])
    flow = make_way(tools, plan, chain, {}, wall=span, wall_was=was)
    moved = apply_flow(tools, plan, flow, extra_undo={span.id: was})
    tools._persist(span)
    return moved, flow


def _expected_end(tools: "PlanTools", span: EventSpan, now: datetime, until: Any, length: Any) -> datetime:
    from thirties_core.clock import parse_duration_minutes
    if until not in (None, ""):
        end = tools._end(until, max(span.start_dt, now - timedelta(hours=12)))
        if end <= now:
            from thirties_core.assistant_tools import ToolError
            raise ToolError(f"{format_clock(end)} has already gone by; when do they expect to finish?")
        return end
    if length not in (None, ""):
        minutes = timedelta(minutes=parse_duration_minutes(length))
        end = span.start_dt + minutes
        return end if end > now else now + minutes  # "another 20 minutes" said after its start: from now
    if span.end_dt - now > ALMOST_DONE:
        return span.end_dt  # there's time left as planned
    end = thirty_ceil(now)
    return end if end - now > ALMOST_DONE else end + timedelta(minutes=30)


def still_doing(tools: "PlanTools", event: Any = None, until: Any = None, length: Any = None, auto_done: Any = None,
                **_: Any) -> str:
    """The user is in the middle of ``event``: in progress, stretched to when it will end, the rest moved along."""
    from thirties_core.assistant_tools import ToolError
    span = tools._find_span(event)
    if span.is_envelope or span.source == "calendar" or span.category not in (SpanCategory.ACTIVITY,
                                                                                 SpanCategory.TASK):
        raise ToolError(f"{span.title} isn't something to mark as in progress.")
    if span.status != SpanStatus.PLANNED:
        raise ToolError(f"{span.title} is already marked {span.status.value}.")
    now = tools._now()
    if span.start_dt > now:
        return start_now(tools, span, until=until, length=length, auto_done=auto_done)
    finish = str(auto_done).strip().lower() in ("true", "yes", "1") if isinstance(auto_done, str) else bool(auto_done)
    if finish and until in (None, "") and length in (None, ""):
        raise ToolError("Say when it's done by itself: until (a time) or length.")
    _keep_plan(span)
    if span.actual_start is None:
        span.actual_start = span.start_dt
    span.time_source = AUTO_DONE if finish else RUNNING
    end = _expected_end(tools, span, now, until, length)
    moved, flow = ([], None)
    if end != span.end_dt:
        moved, flow = _stretch(tools, tools.day_plan, span, end, "still going")
    else:
        tools._persist(span)
    return _said(tools, span, moved, flow, finish, started=False)


def start_now(tools: "PlanTools", span: EventSpan, until: Any = None, length: Any = None, auto_done: Any = None) -> str:
    """The user is doing ``span`` now, before its planned time: it starts now (keeping its length, or the one given),
    and what it now covers moves along after it. The hook for "the plan makes way when the user says what they're
    doing now"."""
    from thirties_core.placement import apply_flow, flexible, make_way
    now = tools._now().replace(second=0, microsecond=0)
    plan = tools.day_plan
    finish = str(auto_done).strip().lower() in ("true", "yes", "1") if isinstance(auto_done, str) else bool(auto_done)
    _keep_plan(span)
    was = (span.start_dt, span.end_dt)
    span.start_dt = now
    span.actual_start = now
    span.end_dt = now + (was[1] - was[0])
    span.end_dt = _expected_end(tools, span, now, until, length) if (until or length) else span.end_dt
    span.time_source = AUTO_DONE if finish else RUNNING
    chain = sorted((s for s in plan.active_spans() if s.id != span.id and not is_running(s)
                    and flexible(tools, plan, s) and s.start_dt >= thirty_of(now)), key=lambda s: s.start_dt)
    flow = make_way(tools, plan, chain, {}, wall=span, wall_was=(now, now))
    moved = apply_flow(tools, plan, flow, extra_undo={span.id: was})
    tools._persist(span)
    return _said(tools, span, moved, flow, finish, started=True)


def _said(tools, span: EventSpan, moved: List[EventSpan], flow, finish: bool, started: bool) -> str:
    from thirties_core.placement import flow_lines, plain_lines
    head = (f"{span.title} is going now, until about {format_clock(span.end_dt)}" if started else
            f"{span.title} is still going, until about {format_clock(span.end_dt)}")
    head += (" (it's marked done by itself then)." if finish else " (in progress, not done).")
    out = [head]
    if moved:
        out.append("Moved along:")
        out += plain_lines(moved)
    if flow is not None:
        out += flow_lines(flow, moved)
    return "\n".join(out)


# --- each check: what's still going keeps going; what finishes by itself, finishes ---

def tick(tools: "PlanTools", plan, now: Optional[datetime] = None) -> List[str]:
    """Run on every check (the background service, each turn). An item in progress whose end has passed with
    nothing said stretches to the end of the current thirty (what follows moves along), up to MAX_OVERRUN past its
    plan; one that finishes by itself is logged done at its end. Returns a line per change, for the log."""
    now = now or tools._now()
    out: List[str] = []
    saved = tools.day_plan
    tools.day_plan = plan
    try:
        for span in sorted(in_progress(plan), key=lambda s: s.start_dt):
            if span.end_dt > now:
                continue
            if span.time_source == AUTO_DONE:
                try:
                    out.append(_finish(tools, span))
                except Exception as e:  # one that can't be logged never stops the others
                    logger.warning("Couldn't mark %s done by itself: %s", span.title, e)
                continue
            planned = (span.planned_end or span.end_dt) - (span.planned_start or span.start_dt)
            limit = (span.planned_end or span.end_dt) + min(max(planned, timedelta(minutes=30)), MAX_OVERRUN)
            new_end = thirty_ceil(now + timedelta(minutes=1))
            if new_end > limit:
                if span.end_dt < limit:
                    new_end = limit
                else:
                    continue  # it's run long enough on its own: it's the user's to say now (not marked yet)
            _stretch(tools, plan, span, new_end, "still going")
            out.append(f"{span.title} still going: now until {format_clock(new_end)}")
    finally:
        tools.day_plan = saved
    return out


def _finish(tools: "PlanTools", span: EventSpan) -> str:
    """Log it done at its end, as the user asked, through the one way outcomes are recorded (list items are
    ticked off as any done is). Not a tool call of the turn: it isn't counted as one the model made."""
    if tools.assistant is None:
        raise ValueError("outcomes can't be recorded here")
    said, by_hand = tools.user_message, tools.by_hand
    tools.user_message, tools.by_hand = None, False
    try:
        result = tools.assistant.log_outcome(span.id, "done", started=format_clock(span.actual_start or span.start_dt),
                                             finished=format_clock(span.end_dt))
    finally:
        tools.user_message, tools.by_hand = said, by_hand
    return f"{span.title} done by itself at {format_clock(span.end_dt)}: {result}"


__all__ = ["IN_PROGRESS_TOOL_SPECS", "still_doing", "start_now", "tick", "is_running", "in_progress", "RUNNING",
           "AUTO_DONE"]
