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
        self.assertIn("Work is now scheduled", out)
        work_restored = [b for b in self.plan.blocks if b.kind == BlockKind.WORK]
        self.assertGreater(len(work_restored), 0)
        self.assertEqual(work_restored[0].label, "Work")
        self.assertTrue(work_restored[0].is_locked)


    def test_reinstate_work_blocks_preserves_assigned_tasks(self) -> None:
        # Clear all work blocks
        self.manager.execute_tool("clear_blocks", {"clear_all_work": True})
        # Schedule an appointment in Block 17 (e.g. 3:00 PM)
        self.manager.execute_tool("allocate_thirty_block", {"block_index": 17, "custom_label": "Doctor Appointment"})
        self.assertEqual(self.plan.get_logical_block(17).kind, BlockKind.ASSIGNED)

        # Reinstate work blocks
        out = self.manager.execute_tool("reinstate_work_blocks", {})
        self.assertIn("keeping your existing Block 17 ('Doctor Appointment') intact", out)

        # Block 17 remains ASSIGNED, other blocks restored to WORK
        self.assertEqual(self.plan.get_logical_block(17).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(17).label, "Doctor Appointment")
        self.assertEqual(self.plan.get_logical_block(10).kind, BlockKind.WORK)


    def test_system_prompt_contains_scheduled_commitments(self) -> None:
        # Schedule Game Night in 24-27 and Composing in 28
        self.manager.execute_tool("allocate_thirty_block", {"start_block": 24, "end_block": 27, "custom_label": "Game Night"})
        self.manager.execute_tool("allocate_thirty_block", {"block_index": 28, "custom_label": "Composing"})

        prompt = self.manager._build_system_prompt()
        self.assertIn("CURRENTLY SCHEDULED TASKS & COMMITMENTS:", prompt)
        self.assertIn("Game Night", prompt)
        self.assertIn("Composing", prompt)
        self.assertIn("Blocks 24 through 27", prompt)
        self.assertIn("Block 28", prompt)

    def test_reinstate_work_blocks_custom_time_range(self) -> None:
        # Clear all blocks first
        self.manager.execute_tool("clear_blocks", {"clear_all_work": True})
        # Set custom work hours from 7am to 3pm
        out = self.manager.execute_tool("reinstate_work_blocks", {"start_time": "7am", "end_time": "3pm"})
        self.assertIn("Blocks 1–16", out)
        for idx in range(1, 17):
            b = self.plan.get_logical_block(idx)
            self.assertEqual(b.kind, BlockKind.WORK)

    def test_slash_command_night_and_reset(self) -> None:
        from thirties_core.astronomy import get_current_time
        reply_night = self.manager.send_user_message("/night")
        self.assertIn("Simulated time set to 10:30 PM", reply_night)
        sim_now = get_current_time(self.tz)
        self.assertEqual(sim_now.hour, 22)
        self.assertEqual(sim_now.minute, 30)

        reply_reset = self.manager.send_user_message("/reset")
        self.assertIn("Simulated time cleared", reply_reset)


if __name__ == "__main__":
    unittest.main()

    def test_user_message_sets_custom_work_hours(self) -> None:
        self.manager.execute_tool("clear_blocks", {"clear_all_work": True})
        reply = self.manager.send_user_message("Oh shoot, turns out work today is from 7am to 3pm.")
        self.assertIn("Blocks 1–16", reply)
        self.assertIn("7:00 AM", reply)
        self.assertIn("3:00 PM", reply)
        self.assertEqual(self.plan.get_logical_block(1).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(16).kind, BlockKind.WORK)

    def test_discretionary_totals(self) -> None:
        # Check discretionary totals
        self.assertGreater(self.plan.daylight_discretionary_total, 0)
        self.assertGreater(self.plan.dark_discretionary_total, 0)

    def test_work_window_moves_clearing_old_work_blocks(self) -> None:
        # Initial work blocks 4-18 are set
        # Schedule composing in Block 3
        self.manager.execute_tool("allocate_thirty_block", {"block_index": 3, "custom_label": "Composing"})
        # Move work window to 7am to 3pm (Blocks 1-16)
        out = self.manager.execute_tool("reinstate_work_blocks", {"start_time": "7am", "end_time": "3pm"})
        self.assertIn("Blocks 1–16", out)
        self.assertIn("Composing", out)

        # Blocks 1-16 must all be WORK
        for idx in range(1, 17):
            b = self.plan.get_logical_block(idx)
            self.assertEqual(b.kind, BlockKind.WORK, f"Block {idx} should be WORK")

        # Block 3 should retain label Composing
        self.assertEqual(self.plan.get_logical_block(3).label, "Composing")

        # Blocks 17 and 18 (old 3:00-4:00 PM work) must be cleared back to discretionary!
        self.assertNotEqual(self.plan.get_logical_block(17).kind, BlockKind.WORK)
        self.assertNotEqual(self.plan.get_logical_block(18).kind, BlockKind.WORK)
        self.assertTrue(self.plan.get_logical_block(17).is_discretionary)
        self.assertTrue(self.plan.get_logical_block(18).is_discretionary)
