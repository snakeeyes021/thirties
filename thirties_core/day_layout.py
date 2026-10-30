"""How a day reads as cards on the timeline, with no GUI in it (the GTK Day view draws what this decides).

One model for things that share time (item 17, S2, T4): items that overlap, nest or coincide are one card ("Redfox
dev + Thirties dev"), for as long as any of them runs; work or sleep under them is named on the card ("during Work")
instead of vanishing (10-20: "Why isn't work showing up in the blocks with morning routine and lunch?"); and an
expanded card lists each item with its own title, time and status rather than the joined title and "Planned" on every
row (soak friction 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

from thirties_core.clock import format_clock
from thirties_core.models import _DISPLAY_PRIORITY, DayPlan, EventSpan, SpanStatus, ThirtyBlock


@dataclass
class Segment:
    """Consecutive thirties that show the same thing: a group of items sharing time, a stretch of work or sleep, or
    one open thirty."""
    blocks: List[ThirtyBlock]
    spans: List[EventSpan] = field(default_factory=list)  # what fills it; empty when open
    is_envelope: bool = False
    backdrop: Optional[EventSpan] = None  # work or sleep under the items, shown on the card (T4)

    @property
    def key(self) -> tuple:
        return (tuple(s.id for s in self.spans), self.blocks[0].index)

    @property
    def start_dt(self) -> datetime:
        if not self.spans:
            return self.blocks[0].start_dt
        return max(self.blocks[0].start_dt, min(s.start_dt for s in self.spans))

    @property
    def end_dt(self) -> datetime:
        if not self.spans:
            return self.blocks[-1].end_dt
        return min(self.blocks[-1].end_dt, max(s.end_dt for s in self.spans))


def build_segments(plan: DayPlan) -> List[Segment]:
    """Group the day's thirties into cards.

    Thirties joined by an item running through them are one card, with every item in them: a two-thirty dinner is
    one card, "Dinner + Audiobook" too, and a one-hour item with a short one nested in it stays one card rather than
    three pieces. Work and sleep split around what's planned on them, keeping clock order, and the items' card names
    them. Open thirties stay individual: each is a slot to plan.
    """
    segments: List[Segment] = []
    for block in plan.blocks:
        occupants = plan.spans_overlapping(block.start_dt, block.end_dt)
        foreground = [s for s in occupants if not s.is_envelope]
        prev = segments[-1] if segments else None
        if foreground:
            backdrop = next((s for s in sorted(occupants, key=lambda s: _DISPLAY_PRIORITY[s.category])
                             if s.is_envelope), None)
            if prev is not None and not prev.is_envelope and {s.id for s in prev.spans} & {s.id for s in foreground}:
                prev.blocks.append(block)
                prev.spans += [s for s in foreground if s.id not in {x.id for x in prev.spans}]
                prev.backdrop = prev.backdrop or backdrop
            else:
                segments.append(Segment([block], sorted(foreground, key=lambda s: s.start_dt), False, backdrop))
            continue
        span = plan.display_span(block)
        if span is not None and prev is not None and prev.is_envelope and [s.id for s in prev.spans] == [span.id]:
            prev.blocks.append(block)
        else:
            segments.append(Segment([block], [span] if span else [], span is not None))
    return segments


def segment_title(plan: DayPlan, seg: Segment) -> Optional[str]:
    """One entry for things sharing time ("Dinner + Audiobook"); things one after another inside a single thirty get
    a line each."""
    if seg.is_envelope or not seg.spans or len(seg.blocks) == 1:
        return plan.display_title(seg.blocks[0])
    titles: List[str] = []
    for s in sorted(seg.spans, key=lambda s: (_DISPLAY_PRIORITY[s.category], s.start_dt)):
        if s.title not in titles:
            titles.append(s.title)
    return " + ".join(titles)


def segment_context(seg: Segment, plan: Optional[DayPlan] = None) -> Optional[str]:
    """The quiet line under a card's title: what a piece of a longer item belongs to, the work or sleep under
    the items ("during Work"), and (given the plan) what's shown alongside without taking the time."""
    parts = []
    if len(seg.spans) == 1:
        span = seg.spans[0]
        if span.start_dt < seg.blocks[0].start_dt or span.end_dt > seg.blocks[-1].end_dt:
            parts.append(f"Part of {span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)}")
    if seg.backdrop is not None and not seg.is_envelope:
        inside = seg.backdrop.start_dt <= seg.start_dt and seg.end_dt <= seg.backdrop.end_dt
        parts.append(("during " if inside else "partly during ") + seg.backdrop.title)
    if plan is not None:
        parts += alongside(plan, seg)
    return " · ".join(parts) or None


def alongside(plan: DayPlan, seg: Segment) -> List[str]:
    """Calendar events that take no time, as quiet notes on the cards they fall on: someone else's ("show only"
    calendars, O1: the user's wife's rehearsals) and invites not answered yet. They never become cards of their own
    or split an open thirty: they aren't the user's time. Each is noted once, on the card where it starts (or the
    day's first card, if it started before the day)."""
    start, end = seg.blocks[0].start_dt, seg.blocks[-1].end_dt
    first = seg.blocks[0].index == plan.blocks[0].index
    out = []
    for s in plan.spans_in_view():
        begins = start.timestamp() <= s.start_dt.timestamp() < end.timestamp() or (first and s.overlaps(start, end))
        if (s.shown_only or s.tentative) and begins:
            when = f"{format_clock(s.start_dt)} – {format_clock(s.end_dt)}"
            out.append(f"{s.title} {when} ({'not yours' if s.shown_only else 'invite, not answered'})")
    return out


_STATUS = {SpanStatus.PLANNED: "Planned", SpanStatus.DONE: "Done", SpanStatus.PARTIAL: "Partly done",
           SpanStatus.SKIPPED: "Skipped",
           SpanStatus.UNKNOWN: "Not reported"}  # gone moot with nothing said (B4): not known to have happened
# A done session on something ongoing (B2: project time): it was worked on, and the thing carries on.
WORKED_ON = "Worked on"


def status_label(spans: List[EventSpan], ongoing: Optional[Callable[[EventSpan], bool]] = None) -> Optional[str]:
    """The badge for a card or row: its items' status ("Done"), or how many of them are done ("1 of 2 done").
    ``ongoing`` says which items are ongoing (kinds.py): a done session on one reads "Worked on"."""
    spans = [s for s in spans if not s.is_envelope]
    if not spans:
        return None
    statuses = {s.status for s in spans}
    if len(statuses) == 1:
        status = statuses.pop()
        if status == SpanStatus.DONE and ongoing is not None and all(ongoing(s) for s in spans):
            return WORKED_ON
        return _STATUS.get(status, str(status.value).capitalize())
    done = sum(1 for s in spans if s.status in (SpanStatus.DONE, SpanStatus.PARTIAL))
    return f"{done} of {len(spans)} done"


@dataclass
class CardRow:
    """One row of an expanded card."""
    block: ThirtyBlock
    title: str
    span: Optional[EventSpan]
    subtitle: Optional[str] = None  # None: the block's own times
    status: Optional[str] = None


def card_rows(plan: DayPlan, seg: Segment, ongoing: Optional[Callable[[EventSpan], bool]] = None) -> List[CardRow]:
    """What an expanded card lists. Several items: each item once, with its own title, real times and status. One
    item, or work or sleep: its thirties."""
    items = [s for s in seg.spans if not s.is_envelope]
    if len(items) > 1:
        rows = []
        for s in sorted(items, key=lambda s: (s.start_dt, _DISPLAY_PRIORITY[s.category])):
            block = plan.block_at(max(s.start_dt, seg.blocks[0].start_dt)) or seg.blocks[0]
            rows.append(CardRow(block, s.title, s, f"{format_clock(s.start_dt)} – {format_clock(s.end_dt)}",
                                status_label([s], ongoing)))
        return rows
    title = segment_title(plan, seg) or ""
    span = seg.spans[0] if seg.spans else None
    return [CardRow(b, title, span, None, status_label(items, ongoing)) for b in seg.blocks]


__all__ = ["CardRow", "Segment", "WORKED_ON", "alongside", "build_segments", "card_rows", "segment_context",
           "segment_title", "status_label"]
