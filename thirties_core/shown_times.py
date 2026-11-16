"""Which of an item's times the user may see to the minute, and how the rest are shown: by their blocks.

A schedule is shown in blocks, the day's half-hour thirties (AGENTS.md §7, "Natural units"). Under the hood an
item keeps minute times: a routine's steps share a thirty one after another, a done item without times gets an
estimate, a placed item has a length. Those minutes only order items and keep a block from being overstuffed;
they're never shown. A time is shown to the minute only when it's a fact the user or a calendar gave:

- a calendar event's start and end (someone else's clock), and anything firm;
- what the user reported for a done item ("I did it 3:10 to 3:40"), or a check-in or tracker recorded;
- a start or end the user stated for their own item when they placed it ("put the parcel drop at 4:45"), while
  the item is still at that time (given_times.py).

Anything else starts in the block it falls in and ends at the end of the block it ends in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from thirties_core.clock import format_clock, thirty_ceil, thirty_of

# Times the user gave, not guessed (day_times.RELIABLE)
TOLD = ("reported", "checkin", "tracker")


def exact(span: Any) -> bool:
    """Whether the item's times are facts the user or a calendar gave, so they're shown as they are."""
    if getattr(span, "source", "") == "calendar" or getattr(span, "is_firm", False):
        return True
    return getattr(span, "time_source", "") in TOLD


def _still(given: Optional[datetime], now_at: datetime) -> bool:
    """A time the user stated, and the item is still there (moved since, the time is no longer theirs)."""
    return given is not None and given.timestamp() == now_at.timestamp()


def start_exact(span: Any) -> bool:
    """Whether the item's start may be shown to the minute: a fact (exact), or the start the user stated for it."""
    return exact(span) or _still(getattr(span, "given_start", None), span.start_dt)


def end_exact(span: Any) -> bool:
    """Whether the item's end may be shown to the minute: a fact (exact), or the end the user stated for it."""
    return exact(span) or _still(getattr(span, "given_end", None), span.end_dt)


def on_edge(dt: datetime) -> bool:
    return dt.minute in (0, 30) and dt.second == 0 and dt.microsecond == 0


def shown_start(span: Any) -> datetime:
    """When the item is shown to start: its real start when that's a fact, else its block."""
    return span.start_dt if start_exact(span) else thirty_of(span.start_dt)


def shown_end(span: Any) -> datetime:
    """When the item is shown to end: its real end when that's a fact, else the end of the block it ends in."""
    return span.end_dt if end_exact(span) else thirty_ceil(span.end_dt)


def span_when(span: Any) -> str:
    """"3:00 PM – 4:00 PM": the item's times as the user may see them."""
    return f"{format_clock(shown_start(span))} – {format_clock(shown_end(span))}"


def exact_ends(spans: Iterable[Any]) -> frozenset:
    """The end times among ``spans`` that may be shown as they are."""
    return frozenset(s.end_dt for s in spans if end_exact(s))


def kept_times(span: Any) -> list:
    """The item's times a reply may say to the minute (clock.real_times): only its facts and the times the user
    stated for it. One off the half hours on an item otherwise may be one Thirties packed in (straight after a
    15-minute step), so it's never taken for the user's."""
    return [dt for dt, ok in ((span.start_dt, start_exact(span)), (span.end_dt, end_exact(span))) if ok]


__all__ = ["TOLD", "end_exact", "exact", "exact_ends", "kept_times", "on_edge", "shown_end", "shown_start",
           "span_when", "start_exact"]
