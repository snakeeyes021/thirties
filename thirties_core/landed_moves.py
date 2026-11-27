"""An item that sits where a move this turn asked for it is moved, whatever happened to the calls that got it there.

The reply checks read each tool result on its own: a second call that put an item where the first one already had
put it came back as a refusal ("nothing to change", or another guard's answer), and the reply was given a line
saying the item "is still at 4:40; I couldn't move it", about an item that was exactly where the user wanted. The
same went for an item the model moved to a place and then back to where it began, or one the model asked to move
to the time it already had.

A line about a move that didn't happen has to be true at the end of the turn, so it is read from the final plan:
an item whose start (and end, when the call gave one) is what a call this turn asked for has landed, and nothing
is said about it as a move that failed (own_up.failed_items, FrontierBrain._unmade_moves,
time_claims.unmade_time_claims).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional, Sequence, Set

# the tool whose arguments name a start (and end) to compare with the plan at the end of the turn
_PLACING = ("update_event",)


def _span_for(tools: Any, ref: str) -> Optional[Any]:
    plan = getattr(tools, "day_plan", None)
    if plan is None or not ref:
        return None
    try:
        span = plan.get_span(ref)
    except Exception:
        span = None
    if span is not None:
        return span
    from thirties_core.plain_admit import _span
    return _span(tools, ref)


def _asked_for(tools: Any, span: Any, args: Dict[str, Any]) -> bool:
    """The call asked for a start (and an end, when it gave one) the item has now."""
    start, end = args.get("start"), args.get("end")
    if start in (None, "") or args.get("shift") not in (None, "") or args.get("to_day") not in (None, "") \
            or args.get("title"):
        return False
    try:
        want_start = tools._start(start)
        want_end = tools._end(end, want_start) if end not in (None, "") else None
    except Exception:
        return False
    shown = (span.start_dt, getattr(span, "given_start", None))
    if want_start not in shown:
        return False
    from thirties_core.shown_times import shown_start, start_exact
    if not (start_exact(span) or shown_start(span) == span.start_dt):
        return False  # it has that time under the hood but shows in its block: the user's time isn't on it yet
    if end in (None, "") and args.get("duration") not in (None, ""):
        from thirties_core.clock import parse_duration_minutes
        try:  # a length asked for with the start counts too: the item has to have it
            return span.end_dt - span.start_dt == timedelta(minutes=parse_duration_minutes(args["duration"]))
        except Exception:
            return False
    return want_end is None or want_end in (span.end_dt, getattr(span, "given_end", None))


def landed_ids(tools: Any, messages: Sequence[Dict[str, Any]], turn_start: int) -> Set[str]:
    """The ids of the items a move this turn put where it asked (or asked to put where they already were)."""
    from thirties_core.own_up import _calls
    out: Set[str] = set()
    plan = getattr(tools, "day_plan", None)
    if plan is None:
        return out
    try:
        calls = _calls(messages, turn_start)
    except Exception:
        return out
    for m, args in calls:
        if m.get("name") not in _PLACING:
            continue
        ref = args.get("event")
        if isinstance(ref, list) or not ref:
            continue
        span = _span_for(tools, str(ref).strip())
        if span is not None and _asked_for(tools, span, args):
            out.add(span.id)
    return out


__all__ = ["landed_ids"]
