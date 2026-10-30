"""Context with each outcome (ROADMAP B7, AGENTS.md §7 light: "record context for learning later").

When an outcome is logged (done, partly done, skipped), what surrounded it is
stored with it: how full the day was, the gaps before and after, the nearby
items and their kinds, and the time since waking. It's captured at that moment,
because the plan changes later (things move, get redrawn, get removed) and the
surroundings can't be rebuilt afterwards.

Nothing reads it yet; the analysis comes later ("does the gym get skipped when
it's wedged between two meetings?"). It's plain JSON per span, synced like the
rest of the record. A later outcome for the same item replaces the earlier one,
captured afresh (the newest report is the one the record holds).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus
from thirties_core.scheduler import StateDatabase, utc_now_iso

NEARBY = timedelta(minutes=90)  # what counts as "nearby": starting or ending within this of the item
MAX_NEARBY = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_context (
    span_id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _minutes(a: datetime, b: datetime) -> int:
    return int(round((b.timestamp() - a.timestamp()) / 60))


def _occupied(span: EventSpan) -> bool:
    return not span.tentative and not span.shown_only and span.status != SpanStatus.SKIPPED


def capture(plan: DayPlan, span: EventSpan, now: datetime, wake: Optional[datetime] = None,
            bed: Optional[datetime] = None) -> Dict[str, Any]:
    """What surrounded ``span`` on ``plan`` at ``now``. ``wake`` / ``bed``: the day's actual (or planned) waking
    hours, from day_times; without them, the canvas edges stand in."""
    start, end = span.actual_start or span.start_dt, span.actual_end or span.end_dt
    others = [s for s in plan.spans if s.id != span.id and _occupied(s)]
    foreground = [s for s in others if not s.is_envelope]
    wake = wake or plan.day_start
    bed = bed or plan.day_end
    awake = max(1, _minutes(wake, bed))

    def inside(s: EventSpan) -> int:  # minutes of s within the waking day
        a, b = max(s.start_dt, wake, key=lambda t: t.timestamp()), min(s.end_dt, bed, key=lambda t: t.timestamp())
        return max(0, _minutes(a, b))
    planned = sum(inside(s) for s in foreground) + inside(span)  # the item itself was part of the day's load

    # gaps: to the nearest thing before and after (an item, a calendar event, or sleep; work counts only if the
    # item isn't during work: an item placed on top of work isn't "next to" it)
    during_work = any(s.category == SpanCategory.WORK and s.start_dt.timestamp() <= start.timestamp()
                      < s.end_dt.timestamp() for s in others)
    bounds = [s for s in others if not (s.category == SpanCategory.WORK and during_work)]
    before = [s.end_dt for s in bounds if s.end_dt.timestamp() <= start.timestamp() + 60]
    after = [s.start_dt for s in bounds if s.start_dt.timestamp() >= end.timestamp() - 60]
    overlapping = [s for s in foreground if s.overlaps(start, end)]
    gap_before = max(0, _minutes(max(before, key=lambda t: t.timestamp()), start)) if before else None
    gap_after = max(0, _minutes(end, min(after, key=lambda t: t.timestamp()))) if after else None

    near = sorted((s for s in foreground if s.start_dt.timestamp() < (end + NEARBY).timestamp()
                   and s.end_dt.timestamp() > (start - NEARBY).timestamp()),
                  key=lambda s: abs(s.start_dt.timestamp() - start.timestamp()))[:MAX_NEARBY]
    nearby = [{"title": s.title, "kind": s.category.value, "fixed": bool(s.is_firm), "status": s.status.value,
               "starts_min_from_item": _minutes(start, s.start_dt), "minutes": s.duration_minutes,
               "alongside": s in overlapping} for s in sorted(near, key=lambda s: s.start_dt.timestamp())]

    return {
        "logged_at": now.isoformat(),
        "status": span.status.value,
        "kind": span.category.value,
        "time_source": span.time_source,
        "planned": [(span.planned_start or span.start_dt).isoformat(), (span.planned_end or span.end_dt).isoformat()],
        "actual": [start.isoformat(), end.isoformat()] if span.status != SpanStatus.SKIPPED else None,
        "day_fullness": {"planned_minutes": planned, "awake_minutes": awake, "share": round(planned / awake, 2),
                         "items": len(foreground) + 1},
        "gap_before_min": gap_before,
        "gap_after_min": gap_after,
        "during_work": during_work,
        "daylight": bool(plan.block_at(start).is_sunlight) if plan.block_at(start) else None,
        "minutes_since_wake": _minutes(wake, start),
        "minutes_after_planned": _minutes(span.planned_start or span.start_dt, start),
        "nearby": nearby,
    }


class OutcomeContextStore:
    """The captured context per span (latest report wins), synced last-write-wins."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def record(self, plan: DayPlan, span: EventSpan, now: datetime, wake: Optional[datetime] = None,
               bed: Optional[datetime] = None) -> Dict[str, Any]:
        data = capture(plan, span, now, wake, bed)
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO outcome_context (span_id, plan_date, title, status, logged_at, data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(span_id) DO UPDATE SET plan_date = excluded.plan_date, title = excluded.title,
                status = excluded.status, logged_at = excluded.logged_at, data = excluded.data,
                updated_at = excluded.updated_at
            """, (span.id, plan.target_date.isoformat(), span.title, span.status.value, data["logged_at"],
                  json.dumps(data), utc_now_iso()))
        return data

    def get(self, span_id: str) -> Optional[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT data FROM outcome_context WHERE span_id = ?", (span_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def between(self, first_day: str, last_day: str) -> List[Dict[str, Any]]:
        """Every captured context for days in [first_day, last_day] (ISO dates), oldest first, with its span."""
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM outcome_context WHERE plan_date >= ? AND plan_date <= ? "
                                "ORDER BY logged_at", (first_day, last_day)).fetchall()
        return [{"span_id": r["span_id"], "title": r["title"], **json.loads(r["data"])} for r in rows]

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM outcome_context").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM outcome_context WHERE span_id = ?",
                               (record["span_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("""
            INSERT OR REPLACE INTO outcome_context (span_id, plan_date, title, status, logged_at, data, updated_at)
            VALUES (:span_id, :plan_date, :title, :status, :logged_at, :data, :updated_at)
            """, record)
            return True


__all__ = ["capture", "OutcomeContextStore"]
