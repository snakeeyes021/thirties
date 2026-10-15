"""Conversion of pre-span (block-kind) saved plans into the span model.

Before event spans, a day was stored as 48 midnight-to-midnight blocks that
each carried a kind (work, sleep, busy calendar, ...) and a label. Clients
displayed them sunrise-first, so the blocks shown "after midnight" actually
carried timestamps from the early morning of the *same* date. This module
rebuilds a correct sunrise-anchored canvas and turns runs of blocks into spans.

Calendar blocks are not converted: calendar events are re-fetched from the
calendar connector, and an attended invite is remembered as a decision.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.models import DayPlan, EventSpan, SpanCategory, ThirtyBlock, new_span_id


def _legacy_blocks_in_diurnal_order(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Old blocks reordered sunrise-first, with after-midnight blocks moved to the next date."""
    blocks = sorted(data["blocks"], key=lambda b: b["index"])
    sunrise_idx = next((b["index"] for b in blocks if b.get("is_sunlight")), 14)
    ordered = []
    for b in blocks[sunrise_idx:] + blocks[:sunrise_idx]:
        b = dict(b)
        start, end = datetime.fromisoformat(b["start_dt"]), datetime.fromisoformat(b["end_dt"])
        if b["index"] < sunrise_idx:
            start, end = start + timedelta(days=1), end + timedelta(days=1)
        b["start"], b["end"] = start, end
        ordered.append(b)
    return ordered


def _runs(blocks: List[Dict[str, Any]], key) -> List[Tuple[Any, datetime, datetime, Dict[str, Any]]]:
    """Group consecutive blocks with the same non-None key into (key, start, end, first_block) runs."""
    runs: List[Tuple[Any, datetime, datetime, Dict[str, Any]]] = []
    for b in blocks:
        k = key(b)
        if k is None:
            continue
        if runs and runs[-1][0] == k and runs[-1][2] == b["start"]:
            runs[-1] = (k, runs[-1][1], b["end"], runs[-1][3])
        else:
            runs.append((k, b["start"], b["end"], b))
    return runs


def legacy_spans_and_decisions(data: Dict[str, Any]) -> Tuple[List[EventSpan], Dict[str, str]]:
    """Spans and calendar decisions ("attend") recovered from an old block-kind plan dict."""
    blocks = _legacy_blocks_in_diurnal_order(data)
    spans: List[EventSpan] = []

    # Calendar blocks used to overwrite the kind of the work or sleep under them;
    # treat them as part of the envelope when the same envelope continues on both sides.
    envelope = [b["kind"] if b["kind"] in ("work", "sleep") else None for b in blocks]
    for i, b in enumerate(blocks):
        if b["kind"] in ("busy_calendar", "ambiguous_calendar") and envelope[i] is None:
            before = envelope[i - 1] if i else None
            after = next((blocks[j]["kind"] for j in range(i + 1, len(blocks))
                          if blocks[j]["kind"] not in ("busy_calendar", "ambiguous_calendar")), None)
            if before in ("work", "sleep") and before == after:
                envelope[i] = before
    for b, kind in zip(blocks, envelope):
        b["envelope"] = kind

    for kind, start, end, _ in _runs(blocks, lambda b: b["envelope"]):
        category = SpanCategory.WORK if kind == "work" else SpanCategory.SLEEP
        spans.append(EventSpan(new_span_id(), category.value.title(), start, end, category, source="user"))

    def label_key(b: Dict[str, Any]) -> Optional[Tuple[str, Optional[str]]]:
        label = b.get("label") or ""
        if b["kind"] in ("busy_calendar", "ambiguous_calendar") or label in ("", "Work", "Sleep"):
            return None
        return (label, b.get("assigned_task_id"))

    for (label, task_id), start, end, _ in _runs(blocks, label_key):
        category = SpanCategory.TASK if task_id else SpanCategory.ACTIVITY
        spans.append(EventSpan(new_span_id(), label, start, end, category, source="user", task_id=task_id))

    decisions = {
        b["source_event_id"]: "attend"
        for b in blocks
        if b["kind"] == "busy_calendar" and b.get("source_event_id")
    }
    return spans, decisions


def plan_from_legacy_dict(data: Dict[str, Any]) -> DayPlan:
    """Build a span-model DayPlan from an old block-kind plan dict."""
    ordered = _legacy_blocks_in_diurnal_order(data)
    blocks = [
        ThirtyBlock(
            index=i,
            start_dt=b["start"],
            end_dt=b["end"],
            is_sunlight=b.get("is_sunlight", False),
            is_twilight=b.get("is_twilight", False),
        )
        for i, b in enumerate(ordered)
    ]
    spans, _ = legacy_spans_and_decisions(data)
    return DayPlan(
        target_date=date.fromisoformat(data["target_date"]),
        blocks=blocks,
        sunrise=datetime.fromisoformat(data["sunrise"]),
        sunset=datetime.fromisoformat(data["sunset"]),
        spans=spans,
        is_finalized=data.get("is_finalized", False),
        notes=data.get("notes", ""),
    )
