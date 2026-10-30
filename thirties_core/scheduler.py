"""Deterministic schedule state: the span store and the day builder.

``StateDatabase`` persists event spans (globally, not per day, so spans that
cross midnight belong to every day they touch), per-day metadata, calendar
decisions, task history and sync bookkeeping. ``DeterministicScheduler``
assembles a ``DayPlan`` for a diurnal date from the solar canvas, stored spans,
default routines from config, and fresh calendar events.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from thirties_core.astronomy import build_base_thirties, get_solar_phases
from thirties_core.calendar_engine import (
    CalendarEngine,
    CalendarEvent,
    EDSCalendarEngine,
    events_to_spans,
)
from thirties_core.config import ThirtiesConfig
from thirties_core.models import DayPlan, EventSpan, SpanCategory, TaskItem

logger = logging.getLogger(__name__)


ROUTINE_TIMESTAMP = "1970-01-01T00:00:00+00:00"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class TaskHistoryRecord:
    task_id: str
    first_scheduled_date: str
    last_scheduled_date: str
    deferred_count: int = 0
    completed_date: Optional[str] = None
    allocated_thirties: int = 0

    @property
    def needs_decomposition(self) -> bool:
        return self.deferred_count >= 3


@dataclass
class DayMeta:
    plan_date: str
    is_finalized: bool = False
    notes: str = ""
    routines_applied: bool = False
    updated_at: str = ""


class StateDatabase:
    """Local SQLite store for spans, day metadata, calendar decisions and task history."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            config = ThirtiesConfig()
            self.db_path = config.resolve_state_db_path()
        else:
            self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.migrate_legacy_snapshots()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error, and always closes."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_history (
                task_id TEXT PRIMARY KEY,
                first_scheduled_date TEXT NOT NULL,
                last_scheduled_date TEXT NOT NULL,
                deferred_count INTEGER DEFAULT 0,
                completed_date TEXT,
                allocated_thirties INTEGER DEFAULT 0
            );
            -- Pre-span plans; read once by migrate_legacy_snapshots, never written.
            CREATE TABLE IF NOT EXISTS day_snapshots (
                plan_date TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                is_finalized INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spans (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS spans_by_time ON spans (start_utc, end_utc);
            CREATE TABLE IF NOT EXISTS day_meta (
                plan_date TEXT PRIMARY KEY,
                is_finalized INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                routines_applied INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            -- This device's own settings (never synced), e.g. which brain the clients use.
            CREATE TABLE IF NOT EXISTS local_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            -- Settings about the person (work hours, permissions...), synced between devices.
            CREATE TABLE IF NOT EXISTS shared_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Changes for Joplin waiting until it's running (see joplin_writeback.py).
            CREATE TABLE IF NOT EXISTS joplin_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS event_decisions (
                event_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

    # --- spans ---

    def upsert_span(self, span: EventSpan, updated_at: Optional[str] = None) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO spans (id, data, start_utc, end_utc, updated_at, deleted)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                data = excluded.data, start_utc = excluded.start_utc, end_utc = excluded.end_utc,
                updated_at = excluded.updated_at, deleted = 0
            """, (span.id, json.dumps(span.to_dict()), _utc_iso(span.start_dt), _utc_iso(span.end_dt),
                  updated_at or utc_now_iso()))

    def insert_span_if_absent(self, span: EventSpan, updated_at: str) -> None:
        """Insert a span unless a row with its id exists (live or deleted)."""
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR IGNORE INTO spans (id, data, start_utc, end_utc, updated_at, deleted)
            VALUES (?, ?, ?, ?, ?, 0)
            """, (span.id, json.dumps(span.to_dict()), _utc_iso(span.start_dt), _utc_iso(span.end_dt), updated_at))

    def delete_span(self, span_id: str, updated_at: Optional[str] = None) -> None:
        """Soft-delete, so the deletion can sync to other devices."""
        with self._get_connection() as conn:
            conn.execute("UPDATE spans SET deleted = 1, updated_at = ? WHERE id = ?",
                         (updated_at or utc_now_iso(), span_id))

    def get_spans_between(self, start: datetime, end: datetime) -> List[EventSpan]:
        """Live spans overlapping [start, end)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT data FROM spans WHERE deleted = 0 AND start_utc < ? AND end_utc > ? ORDER BY start_utc",
                (_utc_iso(end), _utc_iso(start)),
            ).fetchall()
        return [EventSpan.from_dict(json.loads(r["data"])) for r in rows]

    def get_span_records(self) -> List[Dict[str, Any]]:
        """Every span row including deletions, for sync."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT id, data, updated_at, deleted FROM spans").fetchall()
        return [{"id": r["id"], "data": json.loads(r["data"]), "updated_at": r["updated_at"],
                 "deleted": bool(r["deleted"])} for r in rows]

    def merge_span_record(self, record: Dict[str, Any]) -> bool:
        """Last-write-wins merge of one synced span row. Returns True if local state changed."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM spans WHERE id = ?", (record["id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            span = EventSpan.from_dict(record["data"])
            conn.execute("""
            INSERT INTO spans (id, data, start_utc, end_utc, updated_at, deleted)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                data = excluded.data, start_utc = excluded.start_utc, end_utc = excluded.end_utc,
                updated_at = excluded.updated_at, deleted = excluded.deleted
            """, (span.id, json.dumps(span.to_dict()), _utc_iso(span.start_dt), _utc_iso(span.end_dt),
                  record["updated_at"], int(record.get("deleted", False))))
            return True

    # --- day metadata ---

    def get_day_meta(self, plan_date: date) -> DayMeta:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM day_meta WHERE plan_date = ?", (plan_date.isoformat(),)).fetchone()
        if not row:
            return DayMeta(plan_date=plan_date.isoformat())
        return DayMeta(row["plan_date"], bool(row["is_finalized"]), row["notes"],
                       bool(row["routines_applied"]), row["updated_at"])

    def save_day_meta(self, meta: DayMeta, updated_at: Optional[str] = None) -> None:
        meta.updated_at = updated_at or utc_now_iso()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO day_meta (plan_date, is_finalized, notes, routines_applied, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plan_date) DO UPDATE SET
                is_finalized = excluded.is_finalized, notes = excluded.notes,
                routines_applied = excluded.routines_applied, updated_at = excluded.updated_at
            """, (meta.plan_date, int(meta.is_finalized), meta.notes, int(meta.routines_applied), meta.updated_at))

    def get_all_day_meta(self) -> List[DayMeta]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM day_meta").fetchall()
        return [DayMeta(r["plan_date"], bool(r["is_finalized"]), r["notes"], bool(r["routines_applied"]),
                        r["updated_at"]) for r in rows]

    def merge_day_meta(self, meta: DayMeta) -> bool:
        local = self.get_day_meta(date.fromisoformat(meta.plan_date))
        if local.updated_at and local.updated_at >= meta.updated_at:
            return False
        self.save_day_meta(meta, updated_at=meta.updated_at)
        return True

    # --- calendar decisions ---

    def set_event_decision(self, event_id: str, decision: str, updated_at: Optional[str] = None) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO event_decisions (event_id, decision, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET decision = excluded.decision, updated_at = excluded.updated_at
            """, (event_id, decision, updated_at or utc_now_iso()))

    def get_event_decisions(self) -> Dict[str, str]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT event_id, decision FROM event_decisions").fetchall()
        return {r["event_id"]: r["decision"] for r in rows}

    def get_event_decision_records(self) -> List[Dict[str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM event_decisions").fetchall()
        return [dict(r) for r in rows]

    def merge_event_decision(self, record: Dict[str, str]) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM event_decisions WHERE event_id = ?",
                               (record["event_id"],)).fetchone()
        if row and row["updated_at"] >= record["updated_at"]:
            return False
        self.set_event_decision(record["event_id"], record["decision"], updated_at=record["updated_at"])
        return True

    # --- legacy migration ---

    def migrate_legacy_snapshots(self) -> int:
        """Convert pre-span day snapshots into spans once. Returns the number of days migrated."""
        if self.get_sync_meta("legacy_snapshots_migrated") == "1":
            return 0
        from thirties_core.legacy import legacy_spans_and_decisions

        with self._get_connection() as conn:
            rows = conn.execute("SELECT plan_date, plan_json, is_finalized FROM day_snapshots").fetchall()
        migrated = 0
        for row in rows:
            try:
                data = json.loads(row["plan_json"])
                if "spans" in data:
                    continue
                spans, decisions = legacy_spans_and_decisions(data)
            except Exception as e:
                logger.warning("Skipping unreadable legacy snapshot %s: %s", row["plan_date"], e)
                continue
            for span in spans:
                self.upsert_span(span)
            for event_id, decision in decisions.items():
                self.set_event_decision(event_id, decision)
            self.save_day_meta(DayMeta(row["plan_date"], bool(row["is_finalized"]), data.get("notes", ""),
                                       routines_applied=True))
            migrated += 1
        self.set_sync_meta("legacy_snapshots_migrated", "1")
        if migrated:
            logger.info("Migrated %d legacy day snapshots to event spans.", migrated)
        return migrated

    # --- this device's settings ---

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM local_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._get_connection() as conn:
            conn.execute("INSERT INTO local_settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- Joplin outbox ---

    def outbox_add(self, op: str, created_at: str) -> None:
        with self._get_connection() as conn:
            conn.execute("INSERT INTO joplin_outbox (op, created_at) VALUES (?, ?)", (op, created_at))

    def outbox_pending(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT id, op, attempts FROM joplin_outbox WHERE state = 'pending' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def outbox_done(self, op_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM joplin_outbox WHERE id = ?", (op_id,))

    def outbox_attempt(self, op_id: int, error: str) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE joplin_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?", (error, op_id))

    def outbox_fail(self, op_id: int, error: str) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE joplin_outbox SET state = 'failed', last_error = ? WHERE id = ?", (error, op_id))

    def delete_setting(self, key: str) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM local_settings WHERE key = ?", (key,))

    def get_shared_setting(self, key: str) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM shared_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_shared_setting(self, key: str, value: str, updated_at: str) -> None:
        with self._get_connection() as conn:
            conn.execute("INSERT INTO shared_settings (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, value, updated_at))

    def get_shared_setting_records(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM shared_settings").fetchall()
        return [dict(r) for r in rows]

    def merge_shared_setting(self, record: Dict[str, Any]) -> int:
        """Last write wins. Returns 1 if the record changed this device's value."""
        current = self.get_shared_setting_records()
        mine = next((r for r in current if r["key"] == record["key"]), None)
        if mine and mine["updated_at"] >= record["updated_at"]:
            return 0
        self.set_shared_setting(record["key"], record["value"], record["updated_at"])
        return 1

    # --- task history ---

    def get_task_history(self, task_id: str) -> Optional[TaskHistoryRecord]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM task_history WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            return TaskHistoryRecord(
                task_id=row["task_id"],
                first_scheduled_date=row["first_scheduled_date"],
                last_scheduled_date=row["last_scheduled_date"],
                deferred_count=row["deferred_count"],
                completed_date=row["completed_date"],
                allocated_thirties=row["allocated_thirties"],
            )

    def record_task_allocation(self, task_id: str, plan_date: date, count: int = 1) -> None:
        date_str = plan_date.isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM task_history WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            if row:
                conn.execute("""
                UPDATE task_history
                SET last_scheduled_date = ?,
                    allocated_thirties = allocated_thirties + ?
                WHERE task_id = ?
                """, (date_str, count, task_id))
            else:
                conn.execute("""
                INSERT INTO task_history (task_id, first_scheduled_date, last_scheduled_date, allocated_thirties)
                VALUES (?, ?, ?, ?)
                """, (task_id, date_str, date_str, count))
            conn.commit()

    def record_task_deferred(self, task_id: str) -> int:
        """Increment deferred count and return updated count."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT deferred_count FROM task_history WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            if row:
                new_count = row["deferred_count"] + 1
                conn.execute("UPDATE task_history SET deferred_count = ? WHERE task_id = ?", (new_count, task_id))
            else:
                new_count = 1
                today_str = date.today().isoformat()
                conn.execute("""
                INSERT INTO task_history (task_id, first_scheduled_date, last_scheduled_date, deferred_count)
                VALUES (?, ?, ?, 1)
                """, (task_id, today_str, today_str))
            conn.commit()
            return new_count

    def record_task_completed(self, task_id: str, completed_date: Optional[date] = None) -> None:
        comp_str = (completed_date or date.today()).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO task_history (task_id, first_scheduled_date, last_scheduled_date, completed_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET completed_date = excluded.completed_date
            """, (task_id, comp_str, comp_str, comp_str))

    def completed_since(self, since: date) -> set:
        """Ids of items Thirties recorded as done on or after `since`."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT task_id FROM task_history WHERE completed_date >= ?", (since.isoformat(),))
            return {r[0] for r in rows.fetchall()}

    def get_all_task_histories(self) -> List[TaskHistoryRecord]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM task_history")
            return [
                TaskHistoryRecord(
                    task_id=row["task_id"],
                    first_scheduled_date=row["first_scheduled_date"],
                    last_scheduled_date=row["last_scheduled_date"],
                    deferred_count=row["deferred_count"],
                    completed_date=row["completed_date"],
                    allocated_thirties=row["allocated_thirties"],
                )
                for row in cur.fetchall()
            ]

    def upsert_task_history(self, rec: TaskHistoryRecord) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO task_history (
                task_id, first_scheduled_date, last_scheduled_date,
                deferred_count, completed_date, allocated_thirties
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                first_scheduled_date = excluded.first_scheduled_date,
                last_scheduled_date = excluded.last_scheduled_date,
                deferred_count = excluded.deferred_count,
                completed_date = excluded.completed_date,
                allocated_thirties = excluded.allocated_thirties
            """, (
                rec.task_id,
                rec.first_scheduled_date,
                rec.last_scheduled_date,
                rec.deferred_count,
                rec.completed_date,
                rec.allocated_thirties,
            ))
            conn.commit()

    def get_sync_meta(self, key: str) -> Optional[str]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM sync_meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_sync_meta(self, key: str, value: str) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO sync_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
            conn.commit()


def _thirty_to_time(thirty: int) -> time:
    """Config thirty index (0 = midnight, 17 = 8:30 AM) to a clock time."""
    thirty %= 48
    return time(thirty // 2, 30 * (thirty % 2))


class DeterministicScheduler:
    """Assembles DayPlans from the solar canvas, stored spans, routines and the calendar."""

    def __init__(
        self,
        config: Optional[ThirtiesConfig] = None,
        state_db: Optional[StateDatabase] = None,
        calendar_engine: Optional[CalendarEngine] = None,
    ) -> None:
        self.config = config or ThirtiesConfig()
        self.state_db = state_db or StateDatabase()
        self.calendar_engine = calendar_engine or EDSCalendarEngine(self.config)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.general.timezone)

    def routine_spans(self, target_date: date) -> List[EventSpan]:
        """Default work and sleep for a diurnal date, from config.

        Work runs from work_start_thirty through the end of work_end_thirty, on the
        configured work_days only. Sleep is the night of target_date: from
        sleep_start_thirty until sleep_end_thirty the following morning. Each weekday can differ (week.py).
        """
        from thirties_core.week import day_hours
        gen = self.config.general
        tz = self.tz
        hours, morning_after = day_hours(gen, target_date), day_hours(gen, target_date + timedelta(days=1))
        work_start = datetime.combine(target_date, _thirty_to_time(hours.work_start), tzinfo=tz)
        work_end = datetime.combine(target_date, _thirty_to_time(hours.work_end + 1), tzinfo=tz)
        if work_end <= work_start:
            work_end += timedelta(days=1)

        sleep_start_t = _thirty_to_time(hours.sleep_start)
        sleep_day = target_date if sleep_start_t >= time(12, 0) else target_date + timedelta(days=1)
        sleep_start = datetime.combine(sleep_day, sleep_start_t, tzinfo=tz)
        sleep_end = datetime.combine(sleep_day, _thirty_to_time(morning_after.sleep_end), tzinfo=tz)
        if sleep_end <= sleep_start:
            sleep_end += timedelta(days=1)

        # Deterministic ids, so devices that both materialize a day produce the same spans.
        day = target_date.isoformat()
        spans = [EventSpan(f"routine-{day}-sleep", "Sleep", sleep_start, sleep_end, SpanCategory.SLEEP, source="routine")]
        if hours.works:
            spans.insert(0, EventSpan(f"routine-{day}-work", "Work", work_start, work_end, SpanCategory.WORK, source="routine"))
        return spans

    def ensure_routines(self, target_date: date) -> None:
        """Materialize default work and sleep for a date once; later edits are never overwritten."""
        meta = self.state_db.get_day_meta(target_date)
        if meta.routines_applied:
            return
        for span in self.routine_spans(target_date):
            # The oldest possible timestamp: any real edit or deletion, on any device, wins over a default.
            self.state_db.insert_span_if_absent(span, updated_at=ROUTINE_TIMESTAMP)
        meta.routines_applied = True
        self.state_db.save_day_meta(meta)

    def fetch_calendar_events(self, plan: DayPlan) -> List[CalendarEvent]:
        """Calendar events overlapping the diurnal day (which spans two calendar dates)."""
        events: Dict[str, CalendarEvent] = {}
        for d in (plan.day_start.date(), plan.day_end.date()):
            try:
                fetched = self.calendar_engine.fetch_events_for_day(d, self.tz)
            except Exception as e:
                logger.warning("Calendar fetch failed for %s: %s", d, e)
                fetched = []
            for e in fetched:
                if e.overlaps_interval(plan.day_start, plan.day_end):
                    events[e.id] = e
        return list(events.values())

    def build_day_plan(
        self,
        target_date: date,
        events: Optional[List[CalendarEvent]] = None,
    ) -> Tuple[DayPlan, List[CalendarEvent]]:
        """Build the DayPlan for a diurnal date and return it with calendar invites awaiting a decision."""
        gen = self.config.general
        tz = self.tz
        phases = get_solar_phases(gen.latitude, gen.longitude, target_date, tz)
        plan = DayPlan(
            target_date=target_date,
            blocks=build_base_thirties(gen.latitude, gen.longitude, target_date, tz),
            sunrise=phases["sunrise"],
            sunset=phases["sunset"],
        )

        # The previous night's sleep can reach into this morning's canvas.
        self.ensure_routines(target_date - timedelta(days=1))
        self.ensure_routines(target_date)
        plan.spans = self.state_db.get_spans_between(plan.day_start, plan.day_end)

        if events is None:
            events = self.fetch_calendar_events(plan)
        calendar_spans, pending = events_to_spans(events, self.state_db.get_event_decisions(),
                                                  getattr(self.config.calendar, "modes", None))
        calendar_spans = [s for s in calendar_spans if s.overlaps(plan.day_start, plan.day_end)]
        from thirties_core.calendar_outcomes import CalendarOutcomes
        CalendarOutcomes(self.state_db).apply(calendar_spans)  # what happened, kept apart from the calendar
        plan.spans.extend(calendar_spans)
        plan.spans.sort(key=lambda s: s.start_dt)

        meta = self.state_db.get_day_meta(target_date)
        plan.is_finalized = meta.is_finalized
        plan.notes = meta.notes
        return plan, pending

    def build_rolling_schedule(self, start_date: date, days: int = 7) -> List[DayPlan]:
        """Generate plans for several consecutive days."""
        return [self.build_day_plan(start_date + timedelta(days=offset))[0] for offset in range(days)]

    def attach_history_to_tasks(self, tasks: List[TaskItem]) -> None:
        """Enrich task items with deferred counts from state database."""
        for task in tasks:
            rec = self.state_db.get_task_history(task.id)
            if rec:
                task.deferred_count = rec.deferred_count

    def finalize_day_plan(self, plan: DayPlan, notes: Optional[str] = None) -> None:
        """Lock in the day and record how many thirties each task was given."""
        plan.is_finalized = True
        if notes:
            plan.notes = notes
        for span in plan.spans:
            if span.task_id and span.overlaps(plan.day_start, plan.day_end):
                self.state_db.record_task_allocation(
                    task_id=span.task_id,
                    plan_date=plan.target_date,
                    count=max(1, span.duration_minutes // 30),
                )
        meta = self.state_db.get_day_meta(plan.target_date)
        meta.is_finalized, meta.notes = True, plan.notes
        self.state_db.save_day_meta(meta)
