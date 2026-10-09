"""Unit tests for thirties_core.conversation."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from thirties_core.calendar_engine import CalendarEvent, MockCalendarEngine
from thirties_core.config import ThirtiesConfig
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import BlockKind, TaskItem
from thirties_core.scheduler import DeterministicScheduler, StateDatabase


class TestConversation(unittest.TestCase):

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
        self.tz = ZoneInfo("America/New_York")

        # Build day plan
        self.plan, self.ambiguous = self.scheduler.build_day_plan(self.target_date)

        self.task1 = TaskItem(
            id="t1",
            source_notebook="3. Creative",
            title="Compose bridge in Dorico",
            deferred_count=1,
        )

        self.ambiguous_event = CalendarEvent(
            id="e_doc",
            summary="Doctor Appointment",
            start_dt=datetime(2026, 10, 8, 16, 0, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 8, 16, 30, tzinfo=self.tz),
            is_ambiguous=True,
            response_status="needsAction",
        )

        # Mark block 32 with the ambiguous event for testing
        b19 = self.plan.get_logical_block(19)
        b19.kind = BlockKind.AMBIGUOUS_CALENDAR
        b19.source_event_id = "e_doc"
        b19.label = "Doctor Appointment"
        self.ambiguous = [self.ambiguous_event]

        self.mock_inf = MockInferenceEngine()
        self.manager = ConversationManager(
            day_plan=self.plan,
            tasks=[self.task1],
            ambiguous_events=self.ambiguous,
            scheduler=self.scheduler,
            inference_engine=self.mock_inf,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_system_prompt_contains_context(self) -> None:
        sys_msg = self.manager.messages[0]["content"]
        self.assertIn("CURRENT ASTRONOMICAL CONTEXT:", sys_msg)
        self.assertIn("Available Daylight Thirties:", sys_msg)
        self.assertIn("Available Dark Thirties:", sys_msg)
        self.assertIn("Compose bridge in Dorico", sys_msg)
        self.assertIn("Doctor Appointment", sys_msg)

    def test_allocate_thirty_block_tool(self) -> None:
        out = self.manager.execute_tool(
            "allocate_thirty_block",
            {"block_index": 33, "task_id": "t1", "custom_label": "Dorico Compose"},
        )
        self.assertIn("Allocated block 33", out)

        b33 = self.plan.get_logical_block(33)
        self.assertEqual(b33.kind, BlockKind.ASSIGNED)
        self.assertEqual(b33.assigned_task_id, "t1")
        self.assertEqual(b33.label, "Dorico Compose")

    def test_resolve_calendar_event_declined(self) -> None:
        out = self.manager.execute_tool(
            "resolve_calendar_event",
            {"event_id": "e_doc", "attending": False},
        )
        self.assertIn("ignored and opened as discretionary", out)

        b19 = self.plan.get_logical_block(19)
        self.assertEqual(b19.kind, BlockKind.DAYLIGHT_DISCRETIONARY)
        self.assertFalse(b19.is_locked)
        self.assertEqual(len(self.manager.ambiguous_events), 0)

    def test_resolve_calendar_event_accepted(self) -> None:
        out = self.manager.execute_tool(
            "resolve_calendar_event",
            {"event_id": "e_doc", "attending": True},
        )
        self.assertIn("locked as busy calendar block", out)

        b19 = self.plan.get_logical_block(19)
        self.assertEqual(b19.kind, BlockKind.BUSY_CALENDAR)
        self.assertTrue(b19.is_locked)
        self.assertEqual(len(self.manager.ambiguous_events), 0)

    def test_finalize_day_plan_tool(self) -> None:
        out = self.manager.execute_tool(
            "finalize_day_plan",
            {"notes": "Productive daylight session"},
        )
        self.assertIn("finalized", out)
        self.assertTrue(self.plan.is_finalized)
        self.assertEqual(self.plan.notes, "Productive daylight session")

        # Snapshot saved to DB
        saved = self.state_db.get_day_snapshot(self.target_date)
        self.assertIsNotNone(saved)
        self.assertTrue(saved.is_finalized)

    def test_send_user_message_triggers_tool_dispatch(self) -> None:
        reply = self.manager.send_user_message("Let's allocate 19 to Dorico")
        self.assertIn("allocated block 19", reply)

        # Block 32 must now be ASSIGNED
        b19 = self.plan.get_logical_block(19)
        self.assertEqual(b19.kind, BlockKind.ASSIGNED)
        self.assertEqual(b19.label, "Dorico Compose")


    def test_allocate_multiple_thirty_blocks_tool(self) -> None:
        out = self.manager.execute_tool(
            "allocate_thirty_block",
            {"start_block": 28, "end_block": 31, "custom_label": "Composing in Dorico"},
        )
        self.assertIn("Allocated blocks 28 through 31", out)
        for idx in range(28, 32):
            b = self.plan.get_logical_block(idx)
            self.assertEqual(b.kind, BlockKind.ASSIGNED)
            self.assertEqual(b.label, "Composing in Dorico")

    def test_clear_work_blocks_tool(self) -> None:
        initial_daylight = self.plan.daylight_available_count
        # Clear all work blocks
        out = self.manager.execute_tool(
            "clear_blocks",
            {"clear_all_work": True},
        )
        self.assertIn("Cleared and opened", out)
        # Verify no WORK blocks remain
        work_remaining = [b for b in self.plan.blocks if b.kind == BlockKind.WORK]
        self.assertEqual(len(work_remaining), 0)
        # Daylight available count should have increased significantly
        self.assertGreater(self.plan.daylight_available_count, initial_daylight)

    def test_clear_specific_blocks_tool(self) -> None:
        # First assign blocks 28-30
        self.manager.execute_tool(
            "allocate_thirty_block",
            {"start_block": 28, "end_block": 30, "custom_label": "Composing"},
        )
        self.assertEqual(self.plan.get_logical_block(28).kind, BlockKind.ASSIGNED)

        # Now clear blocks 28-30
        out = self.manager.execute_tool(
            "clear_blocks",
            {"start_block": 28, "end_block": 30},
        )
        self.assertIn("Cleared and opened 3 blocks", out)
        for idx in range(28, 31):
            b = self.plan.get_logical_block(idx)
            self.assertIn(b.kind, (BlockKind.DAYLIGHT_DISCRETIONARY, BlockKind.DARK_DISCRETIONARY))
            self.assertEqual(b.label, "")


    def test_reinstate_work_blocks_tool(self) -> None:
        # First clear all work blocks
        self.manager.execute_tool("clear_blocks", {"clear_all_work": True})
        self.assertEqual(len([b for b in self.plan.blocks if b.kind == BlockKind.WORK]), 0)

        # Now reinstate work blocks
        out = self.manager.execute_tool("reinstate_work_blocks", {})
        self.assertIn("Reinstated", out)
        work_restored = [b for b in self.plan.blocks if b.kind == BlockKind.WORK]
        self.assertGreater(len(work_restored), 0)
        self.assertEqual(work_restored[0].label, "Work")
        self.assertTrue(work_restored[0].is_locked)


if __name__ == "__main__":
    unittest.main()
