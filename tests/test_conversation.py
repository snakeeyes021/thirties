"""Unit tests for thirties_core.conversation planning state machine and orthogonal primitives."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from thirties_core.astronomy import get_current_time
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
        )

        # Mark block 19 with the ambiguous event for testing
        b19 = self.plan.get_logical_block(19)
        b19.kind = BlockKind.AMBIGUOUS_CALENDAR
        b19.source_event_id = "e_doc"
        b19.label = "Doctor Appointment"

        self.mock_inf = MockInferenceEngine()
        self.manager = ConversationManager(
            day_plan=self.plan,
            tasks=[self.task1],
            ambiguous_events=[self.ambiguous_event],
            scheduler=self.scheduler,
            inference_engine=self.mock_inf,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_system_prompt_contains_context(self) -> None:
        sys_msg = self.manager.messages[0]["content"]
        self.assertIn("Thursday, October 08, 2026", sys_msg)
        self.assertIn("Available Daylight Thirties:", sys_msg)
        self.assertIn("Available Dark Thirties:", sys_msg)
        self.assertIn("Compose bridge in Dorico", sys_msg)
        self.assertIn("Doctor Appointment", sys_msg)

    def test_modify_blocks_single_block(self) -> None:
        out = self.manager.execute_tool(
            "modify_blocks",
            {"start_block": 33, "task_id": "t1", "label": "Dorico Compose"},
        )
        self.assertIn("Allocated block 33", out)

        b33 = self.plan.get_logical_block(33)
        self.assertTrue(b33.is_assigned)
        self.assertEqual(b33.assigned_task_id, "t1")
        self.assertEqual(b33.label, "Dorico Compose")

    def test_modify_blocks_multiple_blocks(self) -> None:
        out = self.manager.execute_tool(
            "modify_blocks",
            {"start_block": 28, "end_block": 31, "label": "Composing in Dorico"},
        )
        self.assertIn("Allocated blocks 28 through 31", out)
        for idx in range(28, 32):
            b = self.plan.get_logical_block(idx)
            self.assertTrue(b.is_assigned)
            self.assertEqual(b.label, "Composing in Dorico")

    def test_modify_blocks_work_envelope(self) -> None:
        # First clear all work blocks
        self.manager.execute_tool("clear_blocks", {"clear_kind": "WORK"})
        self.assertEqual(len([b for b in self.plan.blocks if b.kind == BlockKind.WORK]), 0)

        # Assign work blocks
        out = self.manager.execute_tool(
            "modify_blocks",
            {"start_block": 4, "end_block": 18, "kind": "WORK", "is_locked": True},
        )
        self.assertIn("Work is now scheduled", out)
        work_restored = [b for b in self.plan.blocks if b.kind == BlockKind.WORK]
        self.assertEqual(len(work_restored), 15)
        self.assertEqual(work_restored[0].label, "Work")
        self.assertTrue(work_restored[0].is_locked)

    def test_modify_blocks_preserves_assigned_tasks(self) -> None:
        # Clear all work blocks
        self.manager.execute_tool("clear_blocks", {"clear_kind": "WORK"})
        # Schedule an appointment in Block 17
        self.manager.execute_tool("modify_blocks", {"start_block": 17, "end_block": 17, "label": "Doctor Appointment"})
        self.assertTrue(self.plan.get_logical_block(17).is_assigned)

        # Set work blocks
        out = self.manager.execute_tool("modify_blocks", {"start_block": 4, "end_block": 18, "kind": "WORK"})
        self.assertIn("keeping your existing Block 17 ('Doctor Appointment') intact", out)

        self.assertEqual(self.plan.get_logical_block(17).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(17).label, "Doctor Appointment")
        self.assertEqual(self.plan.get_logical_block(10).kind, BlockKind.WORK)

    def test_modify_blocks_custom_time_range(self) -> None:
        self.manager.execute_tool("clear_blocks", {"clear_kind": "WORK"})
        out = self.manager.execute_tool("modify_blocks", {"start_time": "7am", "end_time": "3pm", "kind": "WORK"})
        self.assertIn("Blocks 1–16", out)
        for idx in range(1, 17):
            b = self.plan.get_logical_block(idx)
            self.assertEqual(b.kind, BlockKind.WORK)

    def test_work_window_moves_clearing_old_work_blocks(self) -> None:
        # Initial work blocks 4-18 are set
        # Schedule composing in Block 3
        self.manager.execute_tool("modify_blocks", {"start_block": 3, "end_block": 3, "label": "Composing"})
        # Move work window to 7am to 3pm (Blocks 1-16) with explicit clear_existing_envelope=True
        out = self.manager.execute_tool(
            "modify_blocks",
            {"start_time": "7am", "end_time": "3pm", "kind": "WORK", "clear_existing_envelope": True},
        )
        self.assertIn("Blocks 1–16", out)
        self.assertIn("Composing", out)

        for idx in range(1, 17):
            b = self.plan.get_logical_block(idx)
            self.assertEqual(b.kind, BlockKind.WORK, f"Block {idx} should be WORK")

        self.assertEqual(self.plan.get_logical_block(3).label, "Composing")
        self.assertNotEqual(self.plan.get_logical_block(17).kind, BlockKind.WORK)
        self.assertNotEqual(self.plan.get_logical_block(18).kind, BlockKind.WORK)
        self.assertTrue(self.plan.get_logical_block(17).is_discretionary)
        self.assertTrue(self.plan.get_logical_block(18).is_discretionary)

    def test_calendar_lock_protection_in_modify_and_clear(self) -> None:
        b12 = self.plan.get_logical_block(12)
        b12.kind = BlockKind.BUSY_CALENDAR
        b12.is_locked = True
        b12.label = "Dentist Appointment"

        # Attempt to modify block 12 without force_calendar
        out = self.manager.modify_blocks(start_block=12, end_block=12, label="Gaming")
        self.assertIn("Cannot modify block(s) [12]: locked by calendar event", out)
        self.assertEqual(b12.label, "Dentist Appointment")
        self.assertEqual(b12.kind, BlockKind.BUSY_CALENDAR)

        # Attempt to clear block 12 without force_calendar
        out_clear = self.manager.clear_blocks(start_block=12, end_block=12)
        self.assertIn("0 blocks", out_clear)
        self.assertEqual(b12.kind, BlockKind.BUSY_CALENDAR)

    def test_clear_work_blocks_tool(self) -> None:
        initial_daylight = self.plan.daylight_available_count
        out = self.manager.execute_tool(
            "clear_blocks",
            {"clear_kind": "WORK"},
        )
        self.assertIn("Cleared and opened", out)
        work_remaining = [b for b in self.plan.blocks if b.kind == BlockKind.WORK]
        self.assertEqual(len(work_remaining), 0)
        self.assertGreater(self.plan.daylight_available_count, initial_daylight)

    def test_clear_specific_blocks_tool(self) -> None:
        self.manager.execute_tool(
            "modify_blocks",
            {"start_block": 28, "end_block": 30, "label": "Composing"},
        )
        self.assertTrue(self.plan.get_logical_block(28).is_assigned)

        out = self.manager.execute_tool(
            "clear_blocks",
            {"start_block": 28, "end_block": 30},
        )
        self.assertIn("Cleared and opened 3 blocks", out)
        for idx in range(28, 31):
            b = self.plan.get_logical_block(idx)
            self.assertIn(b.kind, (BlockKind.DAYLIGHT_DISCRETIONARY, BlockKind.DARK_DISCRETIONARY))
            self.assertEqual(b.label, "")

    def test_resolve_event_declined(self) -> None:
        out = self.manager.execute_tool(
            "resolve_event",
            {"event_id": "e_doc", "action": "decline"},
        )
        self.assertTrue("opened as discretionary" in out)

        b19 = self.plan.get_logical_block(19)
        self.assertEqual(b19.kind, BlockKind.DAYLIGHT_DISCRETIONARY)
        self.assertFalse(b19.is_locked)
        self.assertEqual(len(self.manager.ambiguous_events), 0)

    def test_resolve_event_accepted(self) -> None:
        out = self.manager.execute_tool(
            "resolve_event",
            {"event_id": "e_doc", "action": "attend"},
        )
        self.assertIn("locked as busy calendar block", out)

        b19 = self.plan.get_logical_block(19)
        self.assertEqual(b19.kind, BlockKind.BUSY_CALENDAR)
        self.assertTrue(b19.is_locked)
        self.assertEqual(len(self.manager.ambiguous_events), 0)

    def test_resolve_event_preserves_other_ambiguous_events(self) -> None:
        e2 = CalendarEvent(
            id="e_dentist",
            summary="Dentist",
            start_dt=datetime(2026, 10, 8, 17, 0, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 8, 17, 30, tzinfo=self.tz),
            is_ambiguous=True,
        )
        self.manager.ambiguous_events.append(e2)
        self.assertEqual(len(self.manager.ambiguous_events), 2)

        out = self.manager.resolve_event(event_id="e_doc", action="attend")
        self.assertIn("Event e_doc resolved: locked as busy calendar block", out)
        self.assertEqual(len(self.manager.ambiguous_events), 1)
        self.assertEqual(self.manager.ambiguous_events[0].id, "e_dentist")

    def test_inspect_blocks_tool(self) -> None:
        out = self.manager.execute_tool(
            "inspect_blocks",
            {"start_block": 1, "end_block": 3},
        )
        self.assertIn("Block 1", out)
        self.assertIn("Block 2", out)
        self.assertIn("Block 3", out)

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
        self.assertIn("block 19", reply.lower())

        b19 = self.plan.get_logical_block(19)
        self.assertTrue(b19.is_assigned)
        self.assertEqual(b19.label, "Dorico Compose")

    def test_system_prompt_contains_scheduled_commitments(self) -> None:
        self.manager.execute_tool("modify_blocks", {"start_block": 24, "end_block": 27, "label": "Game Night"})
        self.manager.execute_tool("modify_blocks", {"start_block": 28, "end_block": 28, "label": "Composing"})

        prompt = self.manager._build_system_prompt()
        self.assertIn("CURRENTLY SCHEDULED TASKS & COMMITMENTS:", prompt)
        self.assertIn("Game Night", prompt)
        self.assertIn("Composing", prompt)
        self.assertIn("Blocks 24 through 27", prompt)
        self.assertIn("Block 28", prompt)

    def test_slash_command_night_and_reset(self) -> None:
        reply_night = self.manager.send_user_message("/night")
        self.assertIn("Simulated time set to 10:30 PM", reply_night)
        sim_now = get_current_time(self.tz)
        self.assertEqual(sim_now.hour, 22)
        self.assertEqual(sim_now.minute, 30)

        reply_reset = self.manager.send_user_message("/reset")
        self.assertIn("Simulated time cleared", reply_reset)

    def test_user_message_sets_custom_work_hours(self) -> None:
        self.manager.execute_tool("clear_blocks", {"clear_kind": "WORK"})
        reply = self.manager.send_user_message("Oh shoot, turns out work today is from 7am to 3pm.")
        self.assertIn("Blocks 1–16", reply)
        self.assertIn("7:00 AM", reply)
        self.assertIn("3:00 PM", reply)
        self.assertEqual(self.plan.get_logical_block(1).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(16).kind, BlockKind.WORK)

    def test_discretionary_totals(self) -> None:
        self.assertGreater(self.plan.daylight_discretionary_total, 0)
        self.assertGreater(self.plan.dark_discretionary_total, 0)

    def test_react_loop_on_inspect_blocks(self) -> None:
        mock_engine = MockInferenceEngine([
            {
                "role": "assistant",
                "content": "Let me inspect.",
                "tool_calls": [
                    {"id": "insp_1", "name": "inspect_blocks", "arguments": {"start_block": 1, "end_block": 5}}
                ],
            },
            {
                "role": "assistant",
                "content": "I have verified blocks 1 to 5 are open.",
                "tool_calls": [],
            }
        ])
        self.manager.inference_engine = mock_engine
        reply = self.manager.send_user_message("Can you check blocks 1 to 5?")
        self.assertEqual(reply, "I have verified blocks 1 to 5 are open.")


    def test_modify_blocks_with_clock_times(self) -> None:
        out = self.manager.modify_blocks(start_time="7am", end_time="3pm", kind="WORK", clear_existing_envelope=True)
        self.assertIn("Blocks 1–16", out)
        self.assertIn("16 chunks", out)
        self.assertIn("7:00 AM to 3:00 PM", out)

        self.assertEqual(self.plan.get_logical_block(1).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(16).kind, BlockKind.WORK)
        self.assertNotEqual(self.plan.get_logical_block(17).kind, BlockKind.WORK)

    def test_clear_and_inspect_blocks_with_clock_times(self) -> None:
        # First assign tasks to blocks 28 to 30
        self.manager.modify_blocks(start_block=28, end_block=30, label="Deep Work")
        self.assertTrue(self.plan.get_logical_block(28).is_assigned)

        # Inspect using clock times (8:30 PM to 10:00 PM corresponds to blocks 28 to 30)
        insp_out = self.manager.inspect_blocks(start_time="8:30 PM", end_time="10:00 PM")
        self.assertIn("Block 28", insp_out)
        self.assertIn("Block 30", insp_out)

        # Clear using clock times
        clear_out = self.manager.clear_blocks(start_time="8:30 PM", end_time="10:00 PM")
        self.assertIn("3 blocks", clear_out)
        self.assertIn("Blocks 28–30", clear_out)
        self.assertFalse(self.plan.get_logical_block(28).is_assigned)

    def test_react_self_correction_on_discrepancy(self) -> None:
        # Simulates the transcript issue: Model draft claimed 15 chunks (Blocks 1-15),
        # but the tool executed 16 chunks (Blocks 1-16).
        mock_engine = MockInferenceEngine([
            {
                "role": "assistant",
                "content": "I have scheduled Work blocks from 07:00 AM to 03:00 PM (Blocks 1-15). This encompasses 15 chunks.",
                "tool_calls": [
                    {
                        "id": "call_work",
                        "name": "modify_blocks",
                        "arguments": {"start_time": "7am", "end_time": "3pm", "kind": "WORK"},
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "I have updated the schedule: Work is set from 07:00 AM to 03:00 PM (Blocks 1–16) for 16 chunks.",
                "tool_calls": [],
            }
        ])
        self.manager.inference_engine = mock_engine
        reply = self.manager.send_user_message("I actually did have work though, from 7 to 3")
        self.assertEqual(
            reply,
            "I have updated the schedule: Work is set from 07:00 AM to 03:00 PM (Blocks 1–16) for 16 chunks.",
        )
        # Ensure that blocks 1 through 16 are indeed WORK
        self.assertEqual(self.plan.get_logical_block(1).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(16).kind, BlockKind.WORK)


    def test_clear_blocks_all_preserves_work_and_sleep(self) -> None:
        # Assign task inside work (block 10)
        self.manager.modify_blocks(start_block=10, end_block=10, label="Work Task", task_id="task_work")
        # Assign daylight task (block 21: 5:00 PM - 5:30 PM, before sunset)
        self.manager.modify_blocks(start_block=21, end_block=21, label="Outdoor Walk", task_id="task_walk")
        # Assign dark task (block 25: 7:00 PM - 7:30 PM, after sunset)
        self.manager.modify_blocks(start_block=25, end_block=25, label="Dorico Composing", task_id="task_music")

        # Clear kind = ALL
        out = self.manager.clear_blocks(clear_kind="ALL")
        self.assertIn("Cleared all scheduled tasks and custom events", out)

        # Daylight discretionary block 21 is cleared back to daylight discretionary
        b21 = self.plan.get_logical_block(21)
        self.assertEqual(b21.kind, BlockKind.DAYLIGHT_DISCRETIONARY)
        self.assertEqual(b21.label, "")
        self.assertIsNone(b21.assigned_task_id)

        # Dark discretionary block 25 is cleared back to dark discretionary
        b25 = self.plan.get_logical_block(25)
        self.assertEqual(b25.kind, BlockKind.DARK_DISCRETIONARY)
        self.assertEqual(b25.label, "")
        self.assertIsNone(b25.assigned_task_id)

        # Work block 10 task is cleared, but remains WORK
        b10 = self.plan.get_logical_block(10)
        self.assertEqual(b10.kind, BlockKind.WORK)
        self.assertEqual(b10.label, "Work")
        self.assertIsNone(b10.assigned_task_id)

        # Other work blocks remain WORK
        b4 = self.plan.get_logical_block(4)
        self.assertEqual(b4.kind, BlockKind.WORK)

    def test_modify_blocks_work_defaults_to_clear_existing_envelope(self) -> None:
        # Work envelope is initially blocks 4-18 (8am-4pm)
        self.assertEqual(self.plan.get_logical_block(18).kind, BlockKind.WORK)
        
        # Modify work without explicitly passing clear_existing_envelope
        out = self.manager.modify_blocks(start_time="7am", end_time="3pm", kind="WORK")
        self.assertIn("Blocks 1–16", out)

        # Blocks 1-16 are work
        self.assertEqual(self.plan.get_logical_block(1).kind, BlockKind.WORK)
        self.assertEqual(self.plan.get_logical_block(16).kind, BlockKind.WORK)

        # Blocks 17 and 18 are replaced and restored to discretionary
        self.assertEqual(self.plan.get_logical_block(17).kind, BlockKind.DAYLIGHT_DISCRETIONARY)
        self.assertEqual(self.plan.get_logical_block(18).kind, BlockKind.DAYLIGHT_DISCRETIONARY)

    def test_contract_verifier_triggers_reflection_on_hallucinated_action(self) -> None:
        # Mock engine claims it cleared tasks, but emitted no tool calls (hallucination)
        mock_engine = MockInferenceEngine([
            {
                "role": "assistant",
                "content": "I have cleared all tasks for today.",
                "tool_calls": [],
            },
            {
                "role": "assistant",
                "content": "I have now cleared all tasks for today.",
                "tool_calls": [
                    {
                        "id": "clear_call",
                        "name": "clear_blocks",
                        "arguments": {"clear_kind": "ALL"},
                    }
                ],
            }
        ])
        self.manager.inference_engine = mock_engine
        reply = self.manager.send_user_message("Please clear all my tasks.")
        
        # Verify reflection turn happened by inspecting message history
        user_msgs = [m["content"] for m in self.manager.messages if m.get("role") == "user"]
        self.assertTrue(any("[CONTRACT VERIFICATION NOTICE]" in str(m) for m in user_msgs))
        self.assertEqual(reply, "I have now cleared all tasks for today.")

    def test_contract_verifier_suppresses_persistent_hallucination(self) -> None:
        # Mock engine claims it cleared tasks on turn 1 AND turn 2 without ever calling a tool
        mock_engine = MockInferenceEngine([
            {
                "role": "assistant",
                "content": "I have cleared all tasks for today.",
                "tool_calls": [],
            },
            {
                "role": "assistant",
                "content": "I have cleared all tasks for today as requested.",
                "tool_calls": [],
            }
        ])
        self.manager.inference_engine = mock_engine
        reply = self.manager.send_user_message("Please clear all my tasks.")
        
        # Final verifier guard intercepts the hallucination
        self.assertIn("I was unable to update your schedule because no valid modification directive could be executed", reply)

    def test_system_prompt_diurnal_today_alignment(self) -> None:
        from thirties_core.astronomy import set_debug_time
        from zoneinfo import ZoneInfo
        from datetime import datetime
        # At 12:23 AM on 2026-10-10, diurnal today is 2026-10-09
        set_debug_time(datetime(2026, 10, 10, 0, 23, tzinfo=ZoneInfo("America/New_York")))
        try:
            # Plan is for 2026-10-09 (Friday)
            self.manager.day_plan.target_date = date(2026, 10, 9)
            prompt = self.manager._build_system_prompt()
            self.assertIn("PLANNING DATE: Today (Friday, October 09, 2026)", prompt)

            # Plan is for 2026-10-10 (Saturday) -> Not diurnal today!
            self.manager.day_plan.target_date = date(2026, 10, 10)
            prompt_future = self.manager._build_system_prompt()
            self.assertIn("PLANNING DATE: Saturday, October 10, 2026", prompt_future)
            self.assertNotIn("PLANNING DATE: Today", prompt_future)
        finally:
            set_debug_time(None)


if __name__ == "__main__":
    unittest.main()
