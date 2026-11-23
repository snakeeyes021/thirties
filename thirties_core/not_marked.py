"""What's not marked yet: the one rule behind the Agenda's "Not marked yet" section and the chat's list.

An item belongs on that list once its time is over with no word from the user about it:

- **still planned** once the thirty it ends in is over (an item in the block the user is still in isn't behind), or
- **stored as unknown** (``SpanStatus.UNKNOWN``, "Not reported"): its time passed with no outcome and the
  background settled it so (kinds.py). Nobody knows whether it happened, so it is listed the same as a planned
  one, and marking it done or skipped (with or without a time) settles it the same way.

An unknown copy of an item that is done on the same plan (an earlier session that didn't happen, then planned
again and done) is settled by that done, so it isn't listed.

The list is in time order, oldest first. The Agenda and the chat each choose which items are the user's to mark
(the Agenda also lists the user's own calendar events); whether one of those is behind is always this rule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from thirties_core.clock import thirty_ceil
from thirties_core.models import EventSpan, SpanStatus


def gone_by(span: EventSpan, block: Optional[datetime]) -> bool:
    """Past its time with nothing said. ``block`` is the start of the current thirty; None for a day looked at
    as a whole (not today), where only what's stored as unknown is behind."""
    if span.status == SpanStatus.UNKNOWN:
        return True
    return block is not None and span.status == SpanStatus.PLANNED and thirty_ceil(span.end_dt) <= block


def _done_keys(spans: List[EventSpan]) -> set:
    keys = set()
    for s in spans:
        if s.status == SpanStatus.DONE:
            keys.add(("title", s.title.strip().lower()))
            if s.task_id:
                keys.add(("task", s.task_id))
    return keys


def _settled(span: EventSpan, done: set) -> bool:
    """An unknown copy of an item done on the same plan."""
    return span.status == SpanStatus.UNKNOWN and (("title", span.title.strip().lower()) in done
                                                  or (bool(span.task_id) and ("task", span.task_id) in done))


def unmarked_spans(spans: Iterable[EventSpan], block: Optional[datetime]) -> List[EventSpan]:
    """The items among ``spans`` (one day's plan) that are not marked yet, oldest first."""
    spans = list(spans)
    done = _done_keys(spans)
    return sorted((s for s in spans if gone_by(s, block) and not _settled(s, done)),
                  key=lambda s: (s.start_dt.timestamp(), s.end_dt.timestamp()))


__all__ = ["gone_by", "unmarked_spans"]
