"""Outcomes for calendar events, kept in Thirties and never written to the calendar.

A calendar event is something you'd mark done (soak friction 1: "an event is something you'd mark done, but
Thirties treats it like a reminder"). Calendar spans are rebuilt from the calendar every time a day is built, so their
outcome can't live on the span: it lives here, keyed by the occurrence's span id ("cal:<uid>@<start>"), and the
scheduler lays it back over the span. The calendar itself is never touched, and the event keeps its calendar times;
what really happened goes in ``actual_start``/``actual_end``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from thirties_core.models import EventSpan, SpanStatus
from thirties_core.scheduler import StateDatabase, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_outcomes (
    span_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    actual_start TEXT NOT NULL DEFAULT '',
    actual_end TEXT NOT NULL DEFAULT '',
    time_source TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

_COLS = ["span_id", "status", "actual_start", "actual_end", "time_source", "notes", "updated_at"]


class CalendarOutcomes:
    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def record(self, span: EventSpan) -> None:
        """Store the span's outcome as it now stands (status, actual times, notes)."""
        self._write({"span_id": span.id, "status": span.status.value,
                     "actual_start": span.actual_start.isoformat() if span.actual_start else "",
                     "actual_end": span.actual_end.isoformat() if span.actual_end else "",
                     "time_source": span.time_source or "", "notes": span.notes or "", "updated_at": utc_now_iso()})

    def apply(self, spans: Iterable[EventSpan]) -> None:
        """Lay stored outcomes back over freshly built calendar spans."""
        spans = [s for s in spans if s.source == "calendar"]
        if not spans:
            return
        with self.db._get_connection() as conn:
            rows = {r["span_id"]: r for r in conn.execute(
                f"SELECT * FROM calendar_outcomes WHERE span_id IN ({', '.join('?' * len(spans))})",
                [s.id for s in spans]).fetchall()}
        for span in spans:
            row = rows.get(span.id)
            if row is None:
                continue
            span.status = SpanStatus(row["status"])
            span.actual_start = datetime.fromisoformat(row["actual_start"]) if row["actual_start"] else None
            span.actual_end = datetime.fromisoformat(row["actual_end"]) if row["actual_end"] else None
            span.time_source = row["time_source"]
            span.notes = row["notes"] or span.notes

    def _write(self, record: Dict[str, Any]) -> None:
        with self.db._get_connection() as conn:
            conn.execute(f"INSERT OR REPLACE INTO calendar_outcomes ({', '.join(_COLS)}) VALUES "
                         f"({', '.join('?' * len(_COLS))})", [record.get(c, "") for c in _COLS])

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM calendar_outcomes").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM calendar_outcomes WHERE span_id = ?",
                               (record["span_id"],)).fetchone()
        if row and row["updated_at"] >= record["updated_at"]:
            return False
        self._write(record)
        return True


def log_calendar_outcome(tools, span: EventSpan, status: SpanStatus, start: Optional[datetime],
                         end: Optional[datetime], note: str = "", estimated: bool = False) -> str:
    """Record what happened to a calendar event without changing the calendar. With no times given it happened at
    its calendar time (that's what a calendar time is); times the user gave are kept as said."""
    from thirties_core.clock import format_clock
    span.status = status
    if note:
        span.notes = note
    if status == SpanStatus.SKIPPED:
        span.actual_start = span.actual_end = None
        span.time_source = ""
    else:
        length = span.end_dt - span.start_dt
        if start and not end:
            end = start + length
        if end and not start:
            start = end - length
        span.actual_start, span.actual_end = start or span.start_dt, end or span.end_dt
        span.time_source = ("estimated" if estimated else "reported") if (start or end) else "planned"
    db = tools._state_db
    if db is not None:
        CalendarOutcomes(db).record(span)
    result = f"Marked {span.title} {status.value}"
    if span.actual_start and status != SpanStatus.SKIPPED:
        result += f" ({format_clock(span.actual_start)} – {format_clock(span.actual_end)}, {span.time_source})"
    return result + "; the calendar itself is unchanged."
