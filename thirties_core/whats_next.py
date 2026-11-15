"""What's next, after a change: a compact slice of the day, rendered by Python from the plan as it now stands and
put at the end of the reply to a turn that changed something (an add, a done or skip, a move, a drop or undrop).

Asking to see the plan right after changing it was common, so the reply carries it: the
block they're in ("Now"), the next few things, anything the change moved that falls outside those, and at the
bottom what's past its time with nothing said ("Not marked yet": unknown, never assumed done or rolled). The model
never writes it and is never asked to. A reply that already shows the schedule (show_schedule), or a turn where
the user asked for it, gets none: never two schedules in one reply.

The setting (shared, every device): "nothing", "next" (this slice) or "rest" (the rest of today, the same way).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from thirties_core.clock import format_clock, slice_lines, thirty_of
from thirties_core.messages import missed_items
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

MODES = ("nothing", "next", "rest")
DEFAULT_MODE = "next"
NEXT_ROWS = 3  # the next few rows after the current block
UNMARKED_ROWS = 5  # how many unmarked items are listed; any before them only as a count
HALF = timedelta(minutes=30)

# A turn that asks to see the plan: the reply shows it already (show_schedule), so no slice under it
SCHEDULE_ASKED_RE = re.compile(
    r"\b(schedule|agenda|what'?s (?:next|left|on|planned|coming up)|what do i have|what have i got|"
    r"rest of (?:the|my|to)day|(?:my|the) day look|plan for (?:today|tonight|the day))\b", re.IGNORECASE)

Snapshot = Dict[str, Tuple[float, float, str]]


def snapshot(plan: DayPlan) -> Snapshot:
    """Each item's times and status, to see what a turn moved."""
    return {s.id: (s.start_dt.timestamp(), s.end_dt.timestamp(), s.status.value) for s in plan.spans}


def moved_since(before: Snapshot, plan: DayPlan) -> List[EventSpan]:
    """The items that were on the plan before and now have other times (still planned)."""
    out = []
    for s in plan.spans:
        old = before.get(s.id)
        if old is not None and (old[0], old[1]) != (s.start_dt.timestamp(), s.end_dt.timestamp()) \
                and s.status == SpanStatus.PLANNED and not s.is_envelope:
            out.append(s)
    return out


def _mine(plan: DayPlan) -> List[EventSpan]:
    """The user's own things still to come, as the schedule lists them (_row_items), planned only."""
    from thirties_core.messages import _row_items
    return [s for s in _row_items(plan) if s.status == SpanStatus.PLANNED and not s.tentative]


def _moved_line(moved: List[EventSpan], plan: DayPlan, said: str) -> str:
    """"Also moved: Shower, then Journal, now 7:30 PM": by the block each now starts in, joined as the rows are,
    unless the user gave that exact time themselves (then it's theirs, to the minute)."""
    from thirties_core.facts import gave_time
    groups: Dict[str, List[EventSpan]] = {}
    for s in sorted(moved, key=lambda s: s.start_dt.timestamp()):
        exact = s.start_dt != thirty_of(s.start_dt) and gave_time(said, s.start_dt)
        when = format_clock(s.start_dt if exact else thirty_of(s.start_dt))
        if s.start_dt >= plan.day_end:
            when += f" on {s.start_dt.strftime('%A')}"
        groups.setdefault(when, []).append(s)
    return "Also moved: " + "; ".join(
        ", then ".join(slice_lines([(s.start_dt, s.end_dt, s.title) for s in items])) + f", now {when}"
        for when, items in groups.items())


def render(plan: DayPlan, now: datetime, moved: Iterable[EventSpan] = (), mode: str = DEFAULT_MODE,
           said: str = "", unmarked: Optional[List[EventSpan]] = None) -> str:
    """The slice, its sections a blank line apart (messages.join_sections): "Now:" and what's in the current block
    (none when nothing is), the next few rows (or the rest of today) laid out by the schedule's own row formatter,
    any moved item not among them, "Not marked yet:" (none when nothing is), and someone else's events in that
    stretch on a "Not yours" line. "" outside the day, or with the mode "nothing". ``said``: the user's message, so a time they gave
    keeps its minutes. ``unmarked``: what's past its time with nothing said (messages.missed_for, which leaves out
    what the user asked not to be asked about); worked out from the plan when not given."""
    from thirties_core.messages import plain_rows, schedule_rows
    if mode not in MODES or mode == "nothing" or not (plan.day_start <= now < plan.day_end):
        return ""
    block = thirty_of(now)
    mine = _mine(plan)
    here = [s for s in mine if s.start_dt < block + HALF and s.end_dt > block]
    on_top = [s for s in here if not s.is_envelope]
    here = on_top or here  # what's placed on Work is what's happening; Work alone when nothing is
    now_line = "Now: " + ", then ".join(slice_lines([(max(s.start_dt, block), s.end_dt, s.title) for s in here])) \
        if here else ""
    upcoming = sorted((s for s in mine if s.start_dt >= block + HALF), key=lambda s: s.start_dt.timestamp())
    rows = plain_rows(schedule_rows(plan, block + HALF, planned_only=True))  # the whole rest: "(until" as shown
    shown_rows = rows if mode == "rest" else rows[:NEXT_ROWS]
    next_block = "\n".join(["Next:" if mode == "next" else "The rest of today:"] + shown_rows) if shown_rows \
        else "Nothing else is planned today."
    shown_until = block + HALF
    if shown_rows:
        cut = upcoming if mode == "rest" else _items_in_rows(upcoming, len(shown_rows))
        shown_until = max((s.end_dt for s in cut), default=block + HALF)
    shown_ids = {s.id for s in here} | {s.id for s in upcoming if s.start_dt < shown_until}
    elsewhere = [s for s in moved if s.id not in shown_ids]
    theirs = [s for s in plan.spans_in_view() if s.shown_only and s.end_dt > block and s.start_dt < shown_until]
    behind = unmarked_lines(missed_items(plan, now) if unmarked is None else unmarked)  # today's only
    from thirties_core.messages import join_sections
    return join_sections(  # Now, Next, Also moved, Not marked yet, Not yours: a blank line between each
        now_line, next_block, _moved_line(elsewhere, plan, said) if elsewhere else "", "\n".join(behind),
        "Not yours: " + "; ".join(f"{s.title}, {format_clock(s.start_dt)}"
                                  for s in sorted(theirs, key=lambda s: s.start_dt.timestamp())) if theirs else "")


def unmarked_lines(spans: List[EventSpan]) -> List[str]:
    """"Not marked yet:" and one plain line per item, by the block it started in, oldest first: the most recent
    few, and how many more came before them. Nothing at all when there's nothing."""
    from thirties_core.messages import MISSED_HEADING
    items = sorted(spans, key=lambda s: (s.start_dt.timestamp(), s.end_dt.timestamp()))
    if not items:
        return []
    shown = items[-UNMARKED_ROWS:]
    more = len(items) - len(shown)
    return [MISSED_HEADING] + ([f"+{more} more earlier"] if more else []) \
        + [f"{format_clock(thirty_of(s.start_dt))} – {s.title}" for s in shown]


def _items_in_rows(upcoming: List[EventSpan], n_rows: int) -> List[EventSpan]:
    """The items the first ``n_rows`` rows of agenda_lines cover (rows are thirties: count distinct start thirties,
    an item that runs on keeps its row)."""
    starts = sorted({thirty_of(s.start_dt) for s in upcoming})
    last = starts[min(n_rows, len(starts)) - 1] if starts else None
    return [s for s in upcoming if last is not None and thirty_of(s.start_dt) <= last]


__all__ = ["MODES", "DEFAULT_MODE", "SCHEDULE_ASKED_RE", "snapshot", "moved_since", "render", "unmarked_lines"]
