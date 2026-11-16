"""The user's own start or end for a calendar event, kept on their plan; the calendar is never written.

A calendar event's times are someone else's clock, so Thirties never changes them on its own, and never writes to
the calendar. But the user knows better than the invite sometimes ("the check-up runs till 3:15, not 2:45"): told
so, the plan keeps the time they gave for that event, shown as they said it, and the calendar stays as it was.
Like an event's outcome (calendar_outcomes.py), it can't live on the span, which is built from the calendar each
time: it lives here, keyed by the occurrence's span id, and is laid back over the span when the day is built.

Only a time the user's words give is kept; any other change to a calendar event is refused as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from thirties_core.scheduler import StateDatabase, utc_now_iso

if TYPE_CHECKING:
    from thirties_core.models import EventSpan
    from thirties_core.plan_tools import PlanTools

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_own_times (
    span_id TEXT PRIMARY KEY,
    start TEXT NOT NULL DEFAULT '',
    end TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""
_COLS = ["span_id", "start", "end", "updated_at"]


class CalendarOwnTimes:
    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def set(self, span: "EventSpan", start: Optional[datetime], end: Optional[datetime]) -> None:
        """Keep a start or end the user gave (None leaves what was kept before)."""
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT start, end FROM calendar_own_times WHERE span_id = ?", (span.id,)).fetchone()
        self._write({"span_id": span.id, "start": start.isoformat() if start else (row["start"] if row else ""),
                     "end": end.isoformat() if end else (row["end"] if row else ""), "updated_at": utc_now_iso()})

    def apply(self, spans: Iterable["EventSpan"]) -> None:
        """Lay the user's own times back over freshly built calendar spans."""
        spans = [s for s in spans if s.source == "calendar"]
        if not spans:
            return
        with self.db._get_connection() as conn:
            rows = {r["span_id"]: r for r in conn.execute(
                f"SELECT * FROM calendar_own_times WHERE span_id IN ({', '.join('?' * len(spans))})",
                [s.id for s in spans]).fetchall()}
        for span in spans:
            row = rows.get(span.id)
            if row is None:
                continue
            start = datetime.fromisoformat(row["start"]) if row["start"] else None
            end = datetime.fromisoformat(row["end"]) if row["end"] else None
            if (end or span.end_dt) <= (start or span.start_dt):
                continue  # the calendar moved under it: the two no longer fit together
            if start:
                span.start_dt = span.given_start = start
            if end:
                span.end_dt = span.given_end = end

    def _write(self, record: Dict[str, Any]) -> None:
        with self.db._get_connection() as conn:
            conn.execute(f"INSERT OR REPLACE INTO calendar_own_times ({', '.join(_COLS)}) VALUES "
                         f"({', '.join('?' * len(_COLS))})", [record.get(c, "") for c in _COLS])

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM calendar_own_times").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM calendar_own_times WHERE span_id = ?",
                               (record["span_id"],)).fetchone()
        if row and row["updated_at"] >= record["updated_at"]:
            return False
        self._write(record)
        return True


def own_times(pt: "PlanTools", span: "EventSpan", start: Any = None, end: Any = None, duration: Any = None) -> \
        Optional[str]:
    """update_event on a calendar event: the start or end the user's words give, kept on the plan. None when the
    call isn't that (the caller refuses it as before); a ToolError when the times don't fit."""
    from thirties_core.clock import format_clock, parse_duration_minutes
    from thirties_core.facts import gave_time
    from thirties_core.plan_tools import ToolError
    message = getattr(pt, "user_message", None)
    if start in (None, "") and end in (None, "") and duration in (None, ""):
        return None
    try:
        new_start = pt._start(start) if start not in (None, "") else span.start_dt
        if end not in (None, ""):
            new_end = pt._start(end)  # on the event's own day: an end before its start is refused below
        elif duration not in (None, ""):
            new_end = new_start + timedelta(minutes=parse_duration_minutes(duration))
        else:
            new_end = span.end_dt
    except Exception:
        return None
    changed = [dt for dt, was in ((new_start, span.start_dt), (new_end, span.end_dt)) if dt != was]
    if not changed:
        return None
    if not getattr(pt, "by_hand", False):
        if message is None or not all(gave_time(message, dt) for dt in changed):
            return None  # a time the user didn't give: the calendar's stands
    if new_end <= new_start:
        raise ToolError(f"{span.title} would end before it starts: give an end after "
                        f"{format_clock(new_start)}.")
    was = f"{format_clock(span.start_dt)} – {format_clock(span.end_dt)}"
    span.start_dt, span.end_dt = new_start, new_end
    if new_start in changed:
        span.given_start = new_start
    if new_end in changed:
        span.given_end = new_end
    db = getattr(pt, "_state_db", None)
    if db is not None:
        CalendarOwnTimes(db).set(span, new_start if new_start in changed else None,
                                 new_end if new_end in changed else None)
    return (f"{span.title} is now {format_clock(new_start)} – {format_clock(new_end)} on the user's plan, as they "
            f"said (it was {was}); their calendar itself isn't changed.")


__all__ = ["CalendarOwnTimes", "own_times"]
