"""Unit tests for ConversationManager and the FrontierBrain turn loop."""

import unittest

from tests.fixtures import DAY, TZ, at, event, make_scheduler, occupant
from thirties_core.astronomy import get_current_time, set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine, parse_model_directives
from thirties_core.models import TaskItem


def reply(text: str) -> dict:
    """A model response as a text transport would deliver it."""
    content, calls = parse_model_directives(text)
    return {"role": "assistant", "content": content, "tool_calls": calls}


class ConversationTestCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        self.manager = self.make_manager()

    def make_manager(self, events=None, tasks=None, notes=None) -> ConversationManager:
        sched = make_scheduler(events)
        plan, pending = sched.build_day_plan(DAY)
        return ConversationManager(plan, tasks or [], pending, sched, inference_engine=self.engine, active_notes=notes or [])

    def work_id(self) -> str:
        return next(s.id for s in self.manager.day_plan.spans if s.title == "Work")


class TestSystemPrompt(ConversationTestCase):

    def test_prompt_has_schedule_in_clock_time_and_no_block_math(self) -> None:
        prompt = self.manager.messages[0]["content"]
        self.assertIn("Today, Thursday, October 15, 2026. Current time: 9:00 AM.", prompt)
        self.assertIn("8:30 AM – 4:00 PM: Work", prompt)
        self.assertIn(f"id={self.work_id()}", prompt)
        self.assertIn("Free time remaining: 4:00 PM – 11:00 PM", prompt)
        self.assertIn("CALL add_event", prompt)
        self.assertNotIn("Block 1", prompt)
        self.assertNotIn("2 blocks = 1 hour", prompt)

    def test_prompt_for_another_day(self) -> None:
        set_debug_time(at("09:00", 3))
        prompt = self.manager.brain.build_system_prompt()
        self.assertIn("(not today; today is Sunday, October 18)", prompt)
        self.assertIn("Free time: 7:00 AM", prompt)

    def test_prompt_is_refreshed_every_turn(self) -> None:
        self.manager.execute_tool("update_event", {"event": "Work", "end": "4:30 PM"})
        self.manager.send_user_message("thanks")
        self.assertIn("8:30 AM – 4:30 PM: Work", self.manager.messages[0]["content"])

    def test_unconfirmed_invites_listed(self) -> None:
        manager = self.make_manager(events=[event("dinner", "19:00", "21:00", "Team dinner", invite=True)])
        self.assertIn("- Team dinner, 7:00 PM – 9:00 PM id=cal:dinner", manager.messages[0]["content"])

    def test_backlog_presentation(self) -> None:
        tasks = [
            TaskItem(id="t_def", source_notebook="1. Tasks > Taxes", title="File 2025 Tax Return", deferred_count=4),
            TaskItem(id="t_creat", source_notebook="3. Creative > Music", title="Compose String Quartet Mvt 2",
                     parent_note_title="Current Commissions", parent_note_id="note_comm", deferred_count=1),
        ]
        notes = [
            {"id": "note_comm", "source_notebook": "3. Creative > Music", "title": "Current Commissions",
             "body": "- [ ] Compose String Quartet Mvt 2", "is_todo": False, "todo_completed": 0},
            {"id": "note_shop", "source_notebook": "1. Tasks", "title": "Shopping List",
             "body": "- [ ] Two suits", "is_todo": True, "todo_completed": 0},
        ]
        prompt = self.make_manager(tasks=tasks, notes=notes).messages[0]["content"]
        self.assertIn("[CHRONIC DEFERRAL: 4x]", prompt)
        self.assertIn("=== NOTEBOOK: 3. Creative > Music ===", prompt)
        self.assertIn("Reference / Purchasing Wishlist", prompt)
        self.assertIn("Compose String Quartet Mvt 2", prompt)


class TestTurnLoop(ConversationTestCase):

    def test_tool_calls_execute_and_reply_is_shown(self) -> None:
        self.engine.set_next_response(reply(f'Got it.\nCALL update_event {{"event": "{self.work_id()}", "end": "4:30 PM"}}'))
        self.assertEqual(self.manager.send_user_message("work went til 4:30"), "Got it.")
        self.assertEqual(occupant(self.manager.day_plan, "16:00"), "Work")
        self.assertEqual(len(self.engine.call_history), 1)  # no follow-up needed

    def test_read_only_tool_gets_a_follow_up_with_results(self) -> None:
        self.engine.set_next_response(reply('CALL find_open_time {"duration": "2 hours"}'))
        self.engine.set_next_response(reply("You're free from 4 to 11 PM."))
        self.assertEqual(self.manager.send_user_message("when am I free?"), "You're free from 4 to 11 PM.")
        follow_up_messages = self.engine.call_history[1]
        self.assertTrue(any(m.get("role") == "tool" and "Free:" in m["content"] for m in follow_up_messages))

    def test_not_done_gets_a_follow_up(self) -> None:
        self.manager = self.make_manager(events=[event("dentist", "15:00", "16:00", "Dentist")])
        self.engine.set_next_response(reply('Done!\nCALL add_event {"title": "Gym", "start": "2:30 PM", "end": "4:00 PM"}'))
        self.engine.set_next_response(reply("The dentist is at 3, so I can only fit 2:30 to 3. Want that?"))
        out = self.manager.send_user_message("gym 2:30 to 4")
        self.assertIn("only fit", out)
        self.assertNotIn("Gym", [s.title for s in self.manager.day_plan.spans])

    def test_stale_draft_is_not_shown_after_a_refusal(self) -> None:
        self.manager = self.make_manager(events=[event("dentist", "15:00", "16:00", "Dentist")])
        self.engine.set_next_response(reply('All set!\nCALL add_event {"title": "Gym", "start": "3:00 PM", "duration": "1 hour"}'))
        self.engine.set_next_response(reply(""))
        out = self.manager.send_user_message("gym at 3")
        self.assertNotIn("All set", out)
        self.assertTrue(out.startswith("I couldn't do that:") and "Dentist" in out, out)

    def test_claim_without_calls_triggers_one_correction(self) -> None:
        self.engine.set_next_response(reply("I've scheduled your walk."))
        self.engine.set_next_response(reply('CALL add_event {"title": "Walk", "start": "5:00 PM", "duration": "30 min"}'))
        self.engine.set_next_response(reply("Walk is on for 5 PM."))
        out = self.manager.send_user_message("walk at 5")
        self.assertEqual(occupant(self.manager.day_plan, "17:00"), "Walk")
        self.assertIn("[System check", self.engine.call_history[1][-1]["content"])
        self.assertEqual(out, "Walk is on for 5 PM.")

    def test_persistent_false_claim_is_not_shown_to_user(self) -> None:
        self.engine.set_next_response(reply("I've scheduled your walk."))
        self.engine.set_next_response(reply("I've scheduled your walk."))
        out = self.manager.send_user_message("walk at 5")
        self.assertIn("wasn't able to change", out)

    def test_suggestion_without_changes_passes_through(self) -> None:
        self.engine.set_next_response(reply("How about composing from 5 to 7?"))
        out = self.manager.send_user_message("what should I do tonight?")
        self.assertEqual(out, "How about composing from 5 to 7?")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_empty_reply_falls_back_to_tool_result(self) -> None:
        self.engine.set_next_response(reply('CALL remove_event {"event": "Work"}'))
        self.engine.set_next_response(reply(""))
        out = self.manager.send_user_message("day off")
        self.assertTrue(out.startswith("Removed Work"), out)

    def test_rounds_are_capped(self) -> None:
        for _ in range(5):
            self.engine.set_next_response(reply("CALL list_schedule"))
        self.manager.send_user_message("show me")
        self.assertEqual(len(self.engine.call_history), 3)


class TestPersistence(ConversationTestCase):

    def test_turns_survive_a_new_manager(self) -> None:
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        engine = MockInferenceEngine([reply("Sure thing.")])
        first = ConversationManager(plan, [], pending, sched, inference_engine=engine, active_notes=[])
        first.send_user_message("plan my evening")
        again = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=engine, active_notes=[])
        self.assertEqual(again.history(), [{"role": "user", "content": "plan my evening"},
                                           {"role": "assistant", "content": "Sure thing."}])

    def test_refresh_follows_new_conversations_but_keeps_a_chosen_old_one(self) -> None:
        sched = make_scheduler()
        engine = MockInferenceEngine()
        desk = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=engine, active_notes=[])
        desk.send_user_message("morning planning")
        morning = desk.conversation.id
        phone = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=engine, active_notes=[])
        phone.new_conversation()
        phone.send_user_message("evening from the phone")
        desk.refresh()
        self.assertEqual(desk.history()[0]["content"], "evening from the phone")  # followed the new one
        desk.switch_conversation(morning)
        phone.send_user_message("more from the phone")
        desk.refresh()
        self.assertEqual(desk.conversation.id, morning)  # kept the deliberate choice


class TestSlashCommands(ConversationTestCase):

    def test_night_and_reset(self) -> None:
        self.assertIn("10:30 PM", self.manager.send_user_message("/night"))
        now = get_current_time(TZ)
        self.assertEqual((now.hour, now.minute), (22, 30))
        self.assertIn("cleared", self.manager.send_user_message("/reset"))
        self.assertEqual(self.engine.call_history, [])


if __name__ == "__main__":
    unittest.main()
