"""Deterministic time budgeting and rolling schedule allocation engine.

Overlays astronomical solar events, sleep windows, work intervals, and
calendar commitments to produce strict integer counts of available
Daylight and Dark Thirties. Tracks task rollover and staleness in state.sqlite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from thirties_core.astronomy import build_base_thirties, get_solar_phases
from thirties_core.calendar_engine import (
    CalendarEngine,
    CalendarEvent,
    EDSCalendarEngine,
    map_events_to_blocks,
)
from thirties_core.config import ThirtiesConfig
from thirties_core.models import BlockKind, DayPlan, TaskItem, ThirtyBlock

logger = logging.getLogger(__name__)


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


class StateDatabase:
    """Local SQLite state database storing task rollover history and day snapshots."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            config = ThirtiesConfig()
            self.db_path = config.resolve_state_db_path()
        else:
            self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS task_history (
                task_id TEXT PRIMARY KEY,
                first_scheduled_date TEXT NOT NULL,
                last_scheduled_date TEXT NOT NULL,
                deferred_count INTEGER DEFAULT 0,
                completed_date TEXT,
                allocated_thirties INTEGER DEFAULT 0
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS day_snapshots (
                plan_date TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                is_finalized INTEGER DEFAULT 0
            );
            """)
            conn.commit()

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
            conn.execute("UPDATE task_history SET completed_date = ? WHERE task_id = ?", (comp_str, task_id))
            conn.commit()

    def save_day_snapshot(self, plan: DayPlan) -> None:
        date_str = plan.target_date.isoformat()
        payload = json.dumps(plan.to_dict())
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO day_snapshots (plan_date, plan_json, is_finalized)
            VALUES (?, ?, ?)
            ON CONFLICT(plan_date) DO UPDATE SET
                plan_json = excluded.plan_json,
                is_finalized = excluded.is_finalized
            """, (date_str, payload, int(plan.is_finalized)))
            conn.commit()

    def get_day_snapshot(self, plan_date: date) -> Optional[DayPlan]:
        date_str = plan_date.isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT plan_json FROM day_snapshots WHERE plan_date = ?", (date_str,))
            row = cur.fetchone()
            if not row:
                return None
            data = json.loads(row["plan_json"])
            return DayPlan.from_dict(data)


class DeterministicScheduler:
    """Applies hard constraints onto base astronomical blocks and budgets time."""

    def __init__(
        self,
        config: Optional[ThirtiesConfig] = None,
        state_db: Optional[StateDatabase] = None,
        calendar_engine: Optional[CalendarEngine] = None,
    ) -> None:
        self.config = config or ThirtiesConfig()
        self.state_db = state_db or StateDatabase()
        self.calendar_engine = calendar_engine or EDSCalendarEngine(self.config)

    def _apply_sleep_and_work_blocks(self, blocks: List[ThirtyBlock]) -> None:
        gen = self.config.general
        w_start = gen.work_start_thirty
        w_end = gen.work_end_thirty
        s_start = gen.sleep_start_thirty
        s_end = gen.sleep_end_thirty

        for block in blocks:
            idx = block.index

            # Sleep window (spans across midnight, e.g. 46..47 and 0..13)
            is_sleep = False
            if s_start > s_end:
                if idx >= s_start or idx < s_end:
                    is_sleep = True
            else:
                if s_start <= idx < s_end:
                    is_sleep = True

            if is_sleep:
                block.kind = BlockKind.SLEEP
                block.label = "Sleep"
                block.is_locked = True
                continue

            # Work window (e.g. 17..31)
            if w_start <= idx <= w_end:
                block.kind = BlockKind.WORK
                block.label = "Work"
                block.is_locked = True
                continue

    def build_day_plan(
        self,
        target_date: date,
        events: Optional[List[CalendarEvent]] = None,
    ) -> Tuple[DayPlan, List[CalendarEvent]]:
        """Construct a deterministic DayPlan overlaid with solar, sleep, work, and calendar bounds."""
        gen = self.config.general
        tz = ZoneInfo(gen.timezone)

        # 1. Base astronomical 48 blocks
        phases = get_solar_phases(gen.latitude, gen.longitude, target_date, tz)
        blocks = build_base_thirties(gen.latitude, gen.longitude, target_date, tz)

        # 2. Overlay Sleep & Work
        self._apply_sleep_and_work_blocks(blocks)

        # 3. Ingest Calendar Events
        if events is None:
            events = self.calendar_engine.fetch_events_for_day(target_date, tz)

        ambiguous_events = map_events_to_blocks(blocks, events)

        # 4. Assemble Plan and recalculate exact integer tallies
        plan = DayPlan(
            target_date=target_date,
            blocks=blocks,
            sunrise=phases["sunrise"],
            sunset=phases["sunset"],
        )
        plan.recalculate_counts()

        return plan, ambiguous_events

    def build_rolling_schedule(self, start_date: date, days: int = 7) -> List[DayPlan]:
        """Generate rolling multi-day budget plans."""
        plans: List[DayPlan] = []
        for offset in range(days):
            current_date = start_date + timedelta(days=offset)
            plan, _ = self.build_day_plan(current_date)
            plans.append(plan)
        return plans

    def attach_history_to_tasks(self, tasks: List[TaskItem]) -> None:
        """Enrich task items with deferred counts from state database."""
        for task in tasks:
            rec = self.state_db.get_task_history(task.id)
            if rec:
                task.deferred_count = rec.deferred_count

    def finalize_day_plan(self, plan: DayPlan) -> None:
        """Lock in day schedule and record task allocations."""
        plan.is_finalized = True
        for block in plan.blocks:
            if block.assigned_task_id:
                self.state_db.record_task_allocation(
                    task_id=block.assigned_task_id,
                    plan_date=plan.target_date,
                    count=1,
                )
        self.state_db.save_day_snapshot(plan)
