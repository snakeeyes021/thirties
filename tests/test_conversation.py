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
        self.engine.set_next_response(reply("The dentist is at 3, so the gym can't go then. How about 4?"))
        out = self.manager.send_user_message("gym at 3")
        self.assertNotIn("All set", out)  # the draft was written before the refusal
        self.assertEqual(out, "The dentist is at 3, so the gym can't go then. How about 4?")
        self.assertIn("didn't write a reply", self.engine.call_history[-1][-1]["content"])

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

    def test_changes_to_other_days_are_not_mistaken_for_no_change(self) -> None:
        self.engine.set_next_response(reply("I've added the NAS move tomorrow at 9.\n"
                                            'CALL add_event {"title": "Move NAS", "start": "9:00 AM", "duration": "1 hour", "day": "tomorrow"}'))
        out = self.manager.send_user_message("NAS tomorrow at 9")
        self.assertEqual(out, "I've added the NAS move tomorrow at 9.")

    def test_suggestion_without_changes_passes_through(self) -> None:
        self.engine.set_next_response(reply("How about composing from 5 to 7?"))
        out = self.manager.send_user_message("what should I do tonight?")
        self.assertEqual(out, "How about composing from 5 to 7?")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_empty_reply_falls_back_to_tool_result(self) -> None:
        self.engine.set_next_response(reply('CALL remove_event {"event": "Work"}'))
        self.engine.set_next_response(reply(""))
        self.engine.set_next_response(reply(""))
        out = self.manager.send_user_message("day off")
        self.assertEqual(out, "Done.")  # never raw tool output, never "Understood."
        self.assertIsNone(occupant(self.manager.day_plan, "10:00"))

    def test_reply_narrating_the_conversation_is_asked_again(self) -> None:
        self.engine.set_next_response(reply("Notice that the previous turn ran the tools. Let's check rule 9."))
        self.engine.set_next_response(reply("You're free all afternoon."))
        out = self.manager.send_user_message("am I free later?")
        self.assertEqual(out, "You're free all afternoon.")

    def test_call_written_mid_sentence_still_runs(self) -> None:
        self.engine.set_next_response(reply('Moving it now.CALL add_event {"title": "Walk", "start": "5:00 PM", "duration": "30 min"}'))
        self.engine.set_next_response(reply("Walk is at 5."))
        out = self.manager.send_user_message("walk at 5")
        self.assertEqual(occupant(self.manager.day_plan, "17:00"), "Walk")
        self.assertNotIn("CALL", out)

    def test_the_chat_shows_one_reply_per_turn(self) -> None:
        self.engine.responses = [
            {"role": "assistant", "content": "Let me check your day.",
             "tool_calls": [{"name": "list_schedule", "arguments": {}}]},
            reply("Wait, looking at the transcript, the user asked about tonight."),
            reply("You're free from 4:00 until 11:00."),
        ]
        out = self.manager.send_user_message("am I free tonight?")
        self.assertEqual(out, "You're free from 4:00 until 11:00.")
        self.assertEqual([m["content"] for m in self.manager.history()],
                         ["am I free tonight?", "You're free from 4:00 until 11:00."])  # no drafts, no notes to itself

    def test_minutes_are_said_as_thirties_unless_asked(self) -> None:
        self.engine.responses = [reply("Sunset is at 6:28 tonight."), reply("Sunset is at 6:28 tonight.")]
        self.assertEqual(self.manager.send_user_message("when's sunset?"), "Sunset is at 6:00 tonight.")
        self.assertEqual(self.manager.send_user_message("what's the exact time of sunset?"), "Sunset is at 6:28 tonight.")
        self.engine.responses = [reply("Your break is in the 10:45 thirty.")]
        self.assertEqual(self.manager.send_user_message("when's my break?"),
                         "Your break is in the 10:30 thirty.")  # the thirty it's in, never the next one
        self.engine.responses = [reply("Got it: your break is at 3:15, not 3:00.")]
        self.assertEqual(self.manager.send_user_message("my break is 3:15, not 3:00, adherence is tracked"),
                         "Got it: your break is at 3:15, not 3:00.")  # they gave the minute: they're asking for it

    def test_rounds_are_capped(self) -> None:
        for _ in range(5):
            self.engine.set_next_response(reply("CALL list_schedule"))
        self.manager.send_user_message("show me")
        self.assertEqual(len(self.engine.call_history), 4)  # three rounds, then one ask for the missing reply


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

    def test_an_opening_greeting_is_kept_and_seen_by_the_model(self) -> None:
        sched = make_scheduler()
        engine = MockInferenceEngine([reply("Got it, you're going.")])
        first = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=engine, active_notes=[])
        first.add_assistant_message("You have an unconfirmed event: 'Dinner'. Are you attending?")
        first.send_user_message("yes")
        self.assertIn("Are you attending?", str(engine.call_history[0]))
        again = ConversationManager(sched.build_day_plan(DAY)[0], [], [], sched, inference_engine=engine, active_notes=[])
        self.assertEqual([m["role"] for m in again.history()], ["assistant", "user", "assistant"])

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



class TestChoosingTheBrain(ConversationTestCase):

    def setUp(self) -> None:
        super().setUp()
        from unittest.mock import patch
        warm = patch.object(ConversationManager, "warm_in_background")  # don't load Gemma in unit tests
        warm.start()
        self.addCleanup(warm.stop)

    def test_switching_keeps_the_conversation_and_sticks_on_this_device(self) -> None:
        self.engine.set_next_response(reply("Morning!"))
        self.manager.send_user_message("hi")
        self.assertEqual(self.manager.brain_name, "frontier")
        out = self.manager.send_user_message("/brain gemma")
        self.assertIn("Gemma (on this device)", out)
        self.assertEqual(type(self.manager.brain).__name__, "SlmBrain")
        self.assertIn("hi", [m["content"] for m in self.manager.history()])
        # Another client on the same database (the Telegram bridge) follows on its next refresh.
        sched = self.manager.scheduler
        other = ConversationManager(self.manager.day_plan, [], [], sched, inference_engine=self.engine, active_notes=[])
        self.assertEqual(other.brain_name, "slm")
        from thirties_core.settings import Settings
        Settings(sched.state_db).set("assistant.brain", "frontier")
        other.refresh()
        self.assertEqual(type(other.brain).__name__, "FrontierBrain")
        self.assertIn("hi", [m["content"] for m in other.history()])

    def test_unknown_brain_is_refused(self) -> None:
        self.assertIn("Unknown brain", self.manager.send_user_message("/brain gpt"))
        self.assertEqual(self.manager.brain_name, "frontier")



class TestSummaries(ConversationTestCase):

    def test_new_conversation_summarizes_the_old_one_and_search_finds_it(self):
        self.engine.set_next_response(reply('CALL add_event {"title": "Dentist prep", "start": "5:00 PM", "duration": "30 min"}'))
        self.engine.set_next_response(reply("Added Dentist prep at 5."))
        self.manager.send_user_message("can you add dentist prep at 5")
        old = self.manager.conversation.id
        self.manager.new_conversation()
        from thirties_core.memory import ConversationStore
        summary = ConversationStore(self.manager.scheduler.state_db).get(old).summary
        self.assertIn('started with "can you add dentist prep at 5"', summary)
        self.assertIn("Added Dentist prep", summary)
        out = self.manager.execute_tool("search_history", {"query": "dentist"})
        self.assertTrue(out.splitlines()[1].startswith("- 2026-10-15 (summary"), out)


if __name__ == "__main__":
    unittest.main()
