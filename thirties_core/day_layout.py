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
from typing import List, Optional

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


def segment_context(seg: Segment) -> Optional[str]:
    """The quiet line under a card's title: what a piece of a longer item belongs to, and the work or sleep under
    the items ("during Work")."""
    parts = []
    if len(seg.spans) == 1:
        span = seg.spans[0]
        if span.start_dt < seg.blocks[0].start_dt or span.end_dt > seg.blocks[-1].end_dt:
            parts.append(f"Part of {span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)}")
    if seg.backdrop is not None and not seg.is_envelope:
        inside = seg.backdrop.start_dt <= seg.start_dt and seg.end_dt <= seg.backdrop.end_dt
        parts.append(("during " if inside else "partly during ") + seg.backdrop.title)
    return " · ".join(parts) or None


_STATUS = {SpanStatus.PLANNED: "Planned", SpanStatus.DONE: "Done", SpanStatus.PARTIAL: "Partly done",
           SpanStatus.SKIPPED: "Skipped"}


def status_label(spans: List[EventSpan]) -> Optional[str]:
    """The badge for a card or row: its items' status ("Done"), or how many of them are done ("1 of 2 done")."""
    spans = [s for s in spans if not s.is_envelope]
    if not spans:
        return None
    statuses = {s.status for s in spans}
    if len(statuses) == 1:
        return _STATUS[statuses.pop()]
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


def card_rows(plan: DayPlan, seg: Segment) -> List[CardRow]:
    """What an expanded card lists. Several items: each item once, with its own title, real times and status. One
    item, or work or sleep: its thirties."""
    items = [s for s in seg.spans if not s.is_envelope]
    if len(items) > 1:
        rows = []
        for s in sorted(items, key=lambda s: (s.start_dt, _DISPLAY_PRIORITY[s.category])):
            block = plan.block_at(max(s.start_dt, seg.blocks[0].start_dt)) or seg.blocks[0]
            rows.append(CardRow(block, s.title, s, f"{format_clock(s.start_dt)} – {format_clock(s.end_dt)}",
                                status_label([s])))
        return rows
    title = segment_title(plan, seg) or ""
    span = seg.spans[0] if seg.spans else None
    return [CardRow(b, title, span, None, status_label(items)) for b in seg.blocks]


__all__ = ["CardRow", "Segment", "build_segments", "card_rows", "segment_context", "segment_title", "status_label"]
