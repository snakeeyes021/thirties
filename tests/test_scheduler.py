"""Unit tests for thirties_core.scheduler."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.models import BlockKind, TaskItem
from thirties_core.scheduler import DeterministicScheduler, StateDatabase


class TestScheduler(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.sqlite"
        self.state_db = StateDatabase(self.db_path)

        self.cfg = ThirtiesConfig()
        self.mock_cal = MockCalendarEngine()
        self.scheduler = DeterministicScheduler(
            config=self.cfg,
            state_db=self.state_db,
            calendar_engine=self.mock_cal,
        )
        self.target_date = date(2026, 10, 8)
        self.tz = ZoneInfo(self.cfg.general.timezone)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_state_db_task_history_and_decomposition(self) -> None:
        task_id = "task_music_1"
        self.state_db.record_task_allocation(task_id, self.target_date, count=2)
        rec = self.state_db.get_task_history(task_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.allocated_thirties, 2)
        self.assertEqual(rec.deferred_count, 0)
        self.assertFalse(rec.needs_decomposition)

        # Defer 1st time
        c1 = self.state_db.record_task_deferred(task_id)
        self.assertEqual(c1, 1)

        # Defer 2nd time
        c2 = self.state_db.record_task_deferred(task_id)
        self.assertEqual(c2, 2)
        self.assertFalse(self.state_db.get_task_history(task_id).needs_decomposition)

        # Defer 3rd time -> triggers needs_decomposition!
        c3 = self.state_db.record_task_deferred(task_id)
        self.assertEqual(c3, 3)
        self.assertTrue(self.state_db.get_task_history(task_id).needs_decomposition)

    def test_state_db_snapshot_save_and_retrieve(self) -> None:
        plan, _ = self.scheduler.build_day_plan(self.target_date)
        plan.notes = "Testing day snapshot"
        self.state_db.save_day_snapshot(plan)

        restored = self.state_db.get_day_snapshot(self.target_date)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.target_date, self.target_date)
        self.assertEqual(restored.notes, "Testing day snapshot")
        self.assertEqual(len(restored.blocks), 48)

    def test_deterministic_grid_math_and_tallies(self) -> None:
        plan, ambiguous = self.scheduler.build_day_plan(self.target_date)

        self.assertEqual(len(plan.blocks), 48)
        self.assertEqual(len(ambiguous), 0)

        # Sleep blocks:
        # sleep_start=46 (11:00 PM), sleep_end=14 (07:00 AM)
        # Blocks 46, 47, and 0..13 are sleep (total 16 blocks = 8 hours)
        sleep_blocks = [b for b in plan.blocks if b.kind == BlockKind.SLEEP]
        self.assertEqual(len(sleep_blocks), 16)
        for b in sleep_blocks:
            self.assertTrue(b.is_locked)

        # Work blocks:
        # work_start=17 (08:30 AM), work_end=31 (03:30 PM)
        # Blocks 17..31 inclusive (total 15 blocks = 7.5 hours)
        work_blocks = [b for b in plan.blocks if b.kind == BlockKind.WORK]
        self.assertEqual(len(work_blocks), 15)
        for b in work_blocks:
            self.assertTrue(b.is_locked)

        # Exact integer counts check:
        # Total blocks must sum to 48
        total = (
            len(sleep_blocks)
            + len(work_blocks)
            + plan.daylight_available_count
            + plan.dark_available_count
        )
        self.assertEqual(total, 48)

        # In October in Arlington (sunrise ~7:10 AM, sunset ~6:41 PM):
        # Work finishes at 3:30 PM (block 31). Blocks 32..36 are Daylight discretionary!
        self.assertGreater(plan.daylight_available_count, 0)
        self.assertGreater(plan.dark_available_count, 0)

    def test_calendar_overlay_adjusts_discretionary_tallies(self) -> None:
        # Baseline plan without calendar
        base_plan, _ = self.scheduler.build_day_plan(self.target_date)
        base_daylight = base_plan.daylight_available_count

        # Add 1-hour event at 4:00 PM (blocks 32 and 33, which are Daylight discretionary)
        ev = CalendarEvent(
            id="ev_choir",
            summary="Choir Rehearsal",
            start_dt=datetime(2026, 10, 8, 16, 0, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 8, 17, 0, tzinfo=self.tz),
            response_status="accepted",
        )
        self.mock_cal.add_event(ev)

        plan_with_ev, _ = self.scheduler.build_day_plan(self.target_date)

        # Blocks 32 and 33 must now be BUSY_CALENDAR
        self.assertEqual(plan_with_ev.get_block(32).kind, BlockKind.BUSY_CALENDAR)
        self.assertEqual(plan_with_ev.get_block(33).kind, BlockKind.BUSY_CALENDAR)

        # Daylight discretionary count must decrease by exactly 2 blocks
        self.assertEqual(plan_with_ev.daylight_available_count, base_daylight - 2)

    def test_rolling_7_day_schedule(self) -> None:
        plans = self.scheduler.build_rolling_schedule(self.target_date, days=7)
        self.assertEqual(len(plans), 7)
        for p in plans:
            self.assertEqual(len(p.blocks), 48)
            self.assertGreater(p.daylight_available_count, 0)
            self.assertGreater(p.dark_available_count, 0)

    def test_attach_history_to_tasks(self) -> None:
        t1 = TaskItem(id="t1", source_notebook="1. Tasks", title="Task 1")
        t2 = TaskItem(id="t2", source_notebook="1. Tasks", title="Task 2")

        # Defer t2 three times
        self.state_db.record_task_deferred("t2")
        self.state_db.record_task_deferred("t2")
        self.state_db.record_task_deferred("t2")

        self.scheduler.attach_history_to_tasks([t1, t2])
        self.assertEqual(t1.deferred_count, 0)
        self.assertEqual(t2.deferred_count, 3)


if __name__ == "__main__":
    unittest.main()
