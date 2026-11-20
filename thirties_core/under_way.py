"""When an item under way really began: kept on the record, given to the model as data, never shown as a clock.

The user says they're doing something now ("on the wheel with the teapot"): the item goes in the block it began in
(PlanTools.doing_now) and keeps the minute it really began as ``actual_start``. That minute is tracking data, like a
done item's real times: it makes the day's record and the estimates right. The user is shown blocks (AGENTS §7.1):
the item under way is in its block, as everything else is, even when it began partway through.

- **For the record only.** The model reads the minute where it reads the item (the doing_now result, describe, the
  schedule it reads), marked plainly: "began 10:50 AM (recorded; show the block, not the minute)" (``recorded``).
  Nothing the user sees carries it: the status line, Now, the whole day and the phone's message show the block.
- **A reply never says it.** The minute isn't a time the user stated for the item's plan, so the reply's rounding
  says it by its block (clock.to_thirties), even when the user said it in a correction or it's the current minute
  (``unsaid``); the note itself never reaches the user (internal_text).
- **A start the user corrects is recorded.** "It began at 10:44", "it kicked off a minute ago", "the start is
  wrong": the model's update_event in that turn changes the recorded start (``given_start``, ``corrected``). If the
  new start falls in another block, the item moves to that block. Without such words its start stays
  (user_placed.start_refused): a question about its end, or room made for something else, never changes when it
  began.

Under way: planned (not marked yet), begun (``actual_start``) and not ended, not work or sleep, not a calendar
event. An item still going with no start said (in_progress.still_doing keeps its planned start as the start) has no
recorded start here: its start was never a fact.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Optional, Tuple

from thirties_core.clock import format_clock, thirty_of

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

# how far past the current minute a start given for something already going may still be (a reply takes a while)
_AHEAD = timedelta(minutes=5)

# the mark on a recorded start wherever the model reads it (internal_text keeps it from the user)
RECORDED = "recorded; show the block, not the minute"

# the user asking for when something began to change: "it began at 10:44", "started just now", "kicked off a
# minute ago", "the start is wrong", "not 10:30", "it's now, not then"
START_ASK_RE = re.compile(
    r"\b(?:start(?:ed|s)?|began|begun|begin|kicked off|got going|got started|got into it)\b(?:\s+\w+){0,4}?\s+"
    r"(?:at|around|about|just|only|right|now|a (?:minute|moment|few|couple)|\d)|"
    r"\b(?:only|just) (?:now )?(?:started|began|begun|got going|got started|kicked off)\b|"
    r"\b(?:the |its |it'?s |when it )?(?:start(?:ed)?|start time|starting time)\b(?:\s+\w+){0,3}?\s+"
    r"(?:is |was )?(?:wrong|off|not right|incorrect)\b|"
    r"\b(?:fix|correct|change|update|set)\w*\b(?:\s+\w+){0,3}?\s+(?:the |its )?(?:start(?:ing)? (?:time)?|start)\b|"
    r"\b(?:isn'?t|is not|wasn'?t|was not|not)\s+(?:at\s+)?\d{1,2}(?::\d{2}|\s*(?:am|pm|a\.m\.|p\.m\.))|"
    r"\b(?:it'?s|it is|it was)\s+(?:right\s+)?now\b(?!\s*\d)",
    re.IGNORECASE)

# the user asking what time it is: the current minute is said as itself then, whatever began at it
_TIME_ASKED_RE = re.compile(r"\bwhat(?:'s| is)? the time\b|\bwhat time is it\b|\btime is it\b|\bthe time now\b",
                            re.IGNORECASE)


def real_start(span: "EventSpan") -> Optional[datetime]:
    """When an item under way really began, as recorded; None when it isn't under way or that's unknown."""
    from thirties_core.models import SpanStatus
    began = getattr(span, "actual_start", None)
    if (began is None or getattr(span, "actual_end", None) is not None or span.status != SpanStatus.PLANNED
            or span.is_envelope or getattr(span, "source", "") == "calendar"):
        return None
    planned = getattr(span, "planned_start", None)
    if planned is not None and planned.timestamp() == began.timestamp():
        return None  # still going with no start said: its plan's start, not a fact
    return began


def recorded(span: "EventSpan") -> str:
    """"began 10:50 AM (recorded; show the block, not the minute)" for an item under way, for the model; "" else."""
    began = real_start(span)
    return f"began {format_clock(began)} ({RECORDED})" if began is not None else ""


def unsaid(keep: frozenset, now: frozenset, spans: Iterable["EventSpan"], message: Optional[str] = None
           ) -> Tuple[frozenset, frozenset]:
    """The minute times a reply may say as they are (clock.real_times), without when an item under way began: that
    minute is said by its block, even when the user said it correcting the start, or it's the current minute (unless
    they asked the time). One that's also a time stated for an item's plan stays."""
    from thirties_core.shown_times import kept_times
    spans = list(spans)
    began = {((dt.hour % 12) or 12, dt.minute) for dt in (real_start(s) for s in spans) if dt is not None}
    planned = {((dt.hour % 12) or 12, dt.minute) for s in spans for dt in kept_times(s)}
    hidden = began - planned
    if not hidden:
        return keep, now
    return keep - hidden, (now if _TIME_ASKED_RE.search(message or "") else now - hidden)


def start_asked(pt: Any) -> bool:
    """The user asks for when an item began to change, this turn or just before (change_asks.asked)."""
    from thirties_core.change_asks import asked
    return asked(pt, START_ASK_RE)


def restart_allowed(pt: "PlanTools", span: "EventSpan", new_start: datetime) -> bool:
    """update_event may give an item under way this new start: the user asked for its start to change, and the new
    start has already come (a later start isn't a correction of when it began, it's a move)."""
    return new_start <= pt._now() + _AHEAD and start_asked(pt)


def given_start(pt: "PlanTools", span: "EventSpan", new_start: datetime) -> Optional[datetime]:
    """The start update_event gives an item under way that changes when it began, or None (its start stays as
    it began): a later time it moves to (it isn't under way any more), or a start the user's words gave it this
    turn (said as a time, or asked for as a correction, ``restart_allowed``)."""
    from thirties_core.models import SpanStatus
    if span.actual_start is None or span.status != SpanStatus.PLANNED or span.is_envelope:
        return None
    if abs((new_start - span.actual_start).total_seconds()) < 60:
        return None
    if new_start > pt._now() + _AHEAD:
        return new_start
    message = getattr(pt, "user_message", None)
    if message is None or getattr(pt, "by_hand", False):
        return None
    from thirties_core.given_times import stated
    return new_start if stated(pt, span.title, new_start, any_tool=True) or restart_allowed(pt, span, new_start) \
        else None


def is_correction(pt: "PlanTools", began: datetime) -> bool:
    """A start from ``given_start`` that corrects when it began (it has already come), not a move to later."""
    return began <= pt._now() + _AHEAD


def corrected(pt: "PlanTools", span: "EventSpan", began: datetime) -> str:
    """The recorded start of an item under way, corrected to ``began``: it stays in the block it began in, keeping
    its length, so one corrected into another block moves there. The minute isn't a time for its plan: the item's
    plan keeps to blocks (nothing given_times keeps)."""
    block, was = thirty_of(began), span.start_dt
    moved = block != was
    if moved:
        span.end_dt = block + (span.end_dt - span.start_dt)
        span.start_dt = block
        span.given_start = None
        pt.day_plan.spans.sort(key=lambda s: s.start_dt)
    span.actual_start = began
    pt._persist(span)
    if moved:
        from thirties_core.move_scope import moved as note_moved  # what this turn moved (move_scope.py)
        note_moved(pt, span)
    notes = pt._make_room(span) if block > was else []  # later: what was planned in its new time moves after it
    pt._save_plan_after_change()
    where = (f"now in the {format_clock(block)} block" if moved else f"still in the {format_clock(block)} block") + \
        f", until {format_clock(span.end_dt)}"
    return f"{pt._title(span)} {recorded(span)}; {where}." + (f" ({'; '.join(notes)})" if notes else "")


def restarted(pt: "PlanTools", span: "EventSpan", new_start: datetime) -> None:
    """After update_event moved an item under way to a later time (``given_start``): it isn't under way any more."""
    span.actual_start = None if new_start > pt._now() + _AHEAD else new_start


__all__ = ["RECORDED", "START_ASK_RE", "corrected", "given_start", "is_correction", "real_start", "recorded",
           "restart_allowed", "restarted", "start_asked", "unsaid"]
