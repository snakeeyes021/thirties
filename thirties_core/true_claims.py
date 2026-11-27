"""What a reply check may read as a claim, so a check fires only on a sentence that is false at the end of the turn.

Three readings that called true sentences false, each taken out:

- **A block phrase names a block, never an item's time.** "You're in the 2:00 PM block, with Patch the spare tubes
  next" says where the user is; the time in it was read as a claim about the item beside it (``without_block_times``).
- **An item that has been handled isn't one that failed to move.** A skipped, done or left-unknown item never gets
  an "is still at ...; I couldn't move it" line, however long ago it was handled (``movable``).
- **Times an outcome recorded are the item's real times.** One marked done with no times known, one skipped, one
  left unknown has no place on the plan a time could be wrong about (``time_is_open``); one with recorded times
  is judged by those and by the forms the schedule shows them in (``shown_forms``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Set, Tuple

_CLOCK = r"\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\b\.?)?"
# "the 2:00 PM block", "the 2:00 – 2:30 block", "the block at 2:30", "the block starting at 2:30"
_BLOCK_PHRASE_RE = re.compile(
    rf"(?:{_CLOCK}\s*(?:–|-|to)\s*)?{_CLOCK}\s+blocks?\b|\bblocks?\s+(?:(?:at|from|of|starting(?:\s+at)?|"
    rf"beginning(?:\s+at)?)\s+){_CLOCK}", re.IGNORECASE)


def without_block_times(text: str) -> str:
    """The text without the times of block phrases: a block is named by its time, and that is no claim about
    an item."""
    return _BLOCK_PHRASE_RE.sub(" ", text or "")


def movable(span: Any) -> bool:
    """Whether a move can fail for this item: it is still planned. A skipped, done, partial or unknown item has been
    handled, and is never one a move was asked for."""
    from thirties_core.models import SpanStatus
    return getattr(span, "status", None) == SpanStatus.PLANNED


def time_is_open(span: Any) -> bool:
    """Whether no time the reply says about this item can be wrong: it was handled with no time known (skipped, left
    unknown, or marked done with nothing but its plan to go by), so the plan's times were never where it happened."""
    from thirties_core.models import SpanStatus
    status = getattr(span, "status", None)
    if status in (SpanStatus.SKIPPED, SpanStatus.UNKNOWN):
        return True
    return status in (SpanStatus.DONE, SpanStatus.PARTIAL) and getattr(span, "time_source", "") in ("planned", "")


def _key(dt: Optional[datetime]) -> Set[Tuple[int, int]]:
    return {(dt.hour % 12, dt.minute)} if dt is not None else set()


def shown_forms(span: Any) -> Tuple[Set[Tuple[int, int]], Set[Tuple[int, int]]]:
    """(the starts, the ends) a reply may say for an item, in every form the schedule shows them: the times it has
    on the plan, the times it really happened at and its original plan, each as it is or by its block (a start by
    its block's start; an end that isn't a fact by the end of its block), and where a list of what got done puts
    it (shown_times.done_when). An end that is a fact is never read as the end of its block."""
    from thirties_core.clock import thirty_ceil, thirty_of
    from thirties_core.shown_times import done_when, end_exact, shown_end
    starts: Set[Tuple[int, int]] = set()
    ends: Set[Tuple[int, int]] = set()
    for dt in (span.start_dt, getattr(span, "actual_start", None), getattr(span, "planned_start", None)):
        if dt is not None:
            starts |= _key(dt) | _key(thirty_of(dt))
    for dt in (span.end_dt, getattr(span, "actual_end", None), getattr(span, "planned_end", None)):
        if dt is not None:
            ends |= _key(dt)
            if not end_exact(span):
                ends |= _key(thirty_ceil(dt))
    ends |= _key(shown_end(span))
    if getattr(getattr(span, "status", None), "value", "") in ("done", "partial"):
        listed = done_when(span)
        starts |= _key(listed)
        ends |= _key(listed)
    return starts, ends


def where_it_happened(span: Any) -> datetime:
    """When the item starts as the user should hear it: when it really started, if it was marked done with times,
    else where it sits."""
    if span.status.value in ("done", "partial") and getattr(span, "actual_start", None) is not None:
        return span.actual_start
    return span.start_dt

