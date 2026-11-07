"""What the user is told about where the day stands: missed items, the status line, the schedule (0.2 items 21-22)."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.memory import CheckinMutes
from thirties_core.messages import (MissedTracker, drop_repeat_checkins, missed_items, missed_lines, place_schedule,
                                    render_schedule, status_line)
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


def span(sid, title, start, end, status=SpanStatus.PLANNED):
    s = EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY)
    s.status = status
    return s


class MessagesCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        for s in (span("k", "Feed kitties", "09:00", "09:05"), span("c", "Caffeinate", "09:05", "09:10"),
                  span("w", "Water bottle", "09:10", "09:15", SpanStatus.DONE), span("t", "Throne", "09:30", "09:45"),
                  span("t2", "Throne", "09:45", "09:55"), span("s", "Shower", "10:00", "10:05"),
                  span("p", "PT", "10:05", "10:25"), span("l", "Lunch", "12:00", "13:00"),
                  span("d", "Dinner", "17:00", "18:00"), span("b", "Book", "17:00", "18:00")):
            self.sched.state_db.upsert_span(s)
        self.plan = self.sched.build_day_plan(DAY)[0]

    def manager(self, engine=None):
        plan, pending = self.sched.build_day_plan(DAY)
        return ConversationManager(plan, [], pending, self.sched, inference_engine=engine or MockInferenceEngine(),
                                   active_notes=[])


class TestMissed(MessagesCase):

    def test_missed_excludes_the_current_block_and_done_items_and_lists_a_title_once(self):
        titles = [s.title for s in missed_items(self.plan, at("10:10"))]
        self.assertEqual(titles, ["Feed kitties", "Caffeinate", "Throne"])  # not Shower/PT: still in that block
        self.assertEqual(missed_lines(missed_items(self.plan, at("10:10"))),
                         ["- Feed kitties", "- Caffeinate", "- Throne"])  # one per line (10-30 10:10)

    def test_an_item_counts_once_its_block_is_over(self):
        self.assertNotIn("Shower", [s.title for s in missed_items(self.plan, at("10:29"))])
        self.assertIn("Shower", [s.title for s in missed_items(self.plan, at("10:30"))])

    def test_the_tracker_says_when_something_new_fell_behind(self):
        tracker = MissedTracker(self.sched.state_db)
        missed = missed_items(self.plan, at("10:10"))
        self.assertTrue(tracker.anything_new(at("10:10"), missed))
        tracker.mark(at("10:10"), missed)
        self.assertFalse(tracker.anything_new(at("10:20"), missed))
        self.assertTrue(tracker.anything_new(at("10:30"), missed_items(self.plan, at("10:30"))))  # Shower, PT
        self.assertTrue(tracker.anything_new(at("10:10", 1), missed))  # a new day starts over


class TestStatusAndSchedule(MessagesCase):

    def test_status_line_names_the_block_and_what_is_in_it(self):
        self.assertEqual(status_line(self.plan, at("10:10")), "You're in the 10:00 AM block: Shower, then PT.")
        self.assertEqual(status_line(self.plan, at("15:10")), "You're in the 3:00 PM block: Work (until 4:00 PM).")

    def test_status_says_until_only_when_the_next_thing_doesnt_follow_on(self):
        self.assertEqual(status_line(self.plan, at("12:10")), "You're in the 12:00 PM block: Lunch (until 1:00 PM).")
        self.assertEqual(status_line(self.plan, at("12:10"), next_start=at("13:00")),
                         "You're in the 12:00 PM block: Lunch.")  # 10-29: superfluous before the next item

    def test_schedule_reads_from_now_with_the_missed_right_under_the_status(self):
        text = render_schedule(self.plan, at("10:10"))
        lines = text.splitlines()
        self.assertEqual(lines[:5], ["You're in the 10:00 AM block: Shower, then PT.", "Not marked yet:",
                                     "- Feed kitties", "- Caffeinate", "- Throne"])
        self.assertNotIn("9:00", text)  # 10-26: "Just show me 10 o'clock onwards"
        self.assertIn("- 5:00 PM – Dinner + Book", text)  # things at the same time: one entry
        self.assertIn("- 12:00 PM – Lunch", text)  # on top of Work, which the status says runs until 4

    def test_the_whole_day_when_asked(self):
        text = render_schedule(self.plan, at("10:10"), whole_day=True)
        self.assertIn("The whole day:", text)
        self.assertIn("Feed kitties", text.split("The whole day:")[1])

    def test_another_day_is_the_whole_day_without_a_status(self):
        text = render_schedule(self.plan, None, label="tomorrow")
        self.assertTrue(text.startswith("Here's tomorrow:"))
        self.assertNotIn("You're in", text)


class TestPlacing(unittest.TestCase):

    CANON = "You're in the 10:00 AM block: Shower.\n\n- 10:30 – Work\n- 12:00 – Work + Lunch"

    def test_the_models_own_rows_are_replaced_by_the_canonical_layout(self):
        model = "Here's the rest of today:\n- 10:30 AM – Work (until 12:00)\n- 12:00 – Lunch\n\nAnything else?"
        self.assertEqual(place_schedule(model, self.CANON),
                         "Here's the rest of today:\n" + self.CANON + "\n\nAnything else?")

    def test_a_reply_without_rows_gets_the_schedule_at_the_end(self):
        self.assertEqual(place_schedule("Moved lunch.", self.CANON), "Moved lunch.\n\n" + self.CANON)

    def test_the_models_own_status_and_missed_list_go_too(self):
        model = ("You're in the 10:00 block: Shower.\nNot marked yet:\n- Caffeinate\n- Breakfast\n\n"
                 "- 10:30 – Work\n\nAnything else?")
        self.assertEqual(place_schedule(model, self.CANON), self.CANON + "\n\nAnything else?")

    def test_a_reply_that_copied_it_is_left_alone(self):
        self.assertEqual(place_schedule("Sure:\n" + self.CANON, self.CANON), "Sure:\n" + self.CANON)


class TestRepeatCheckins(unittest.TestCase):

    def test_a_question_about_items_already_shown_is_dropped(self):
        missed = [span("c", "Caffeinate", "09:05", "09:10"), span("b", "Breakfast", "09:10", "09:30")]
        out, asked = drop_repeat_checkins("Logged PT! Did you get to Caffeinate or Breakfast?", missed,
                                          {"caffeinate", "breakfast"})
        self.assertEqual((out, asked), ("Logged PT!", []))

    def test_a_first_question_stays_and_is_remembered(self):
        missed = [span("c", "Caffeinate", "09:05", "09:10")]
        out, asked = drop_repeat_checkins("Logged PT! Did you get to Caffeinate?", missed, set())
        self.assertEqual((out, asked), ("Logged PT! Did you get to Caffeinate?", ["Caffeinate"]))

    def test_other_questions_stay(self):
        missed = [span("c", "Caffeinate", "09:05", "09:10")]
        out, _ = drop_repeat_checkins("Want me to move Caffeinate to 11?", missed, {"caffeinate"})
        self.assertEqual(out, "Want me to move Caffeinate to 11?")


class TestInTheConversation(MessagesCase):

    def test_show_schedule_is_laid_out_by_python_and_counts_as_shown(self):
        engine = MockInferenceEngine([reply("CALL show_schedule {}"),
                                      reply("Here you go:\n- 10:00 – Shower\n- 12:00 – Lunch\nAnything else?")])
        manager = self.manager(engine)
        out = manager.send_user_message("what's left?")
        self.assertIn("You're in the 10:00 block: Shower, then PT.\nNot marked yet:\n- Feed kitties", out)
        self.assertTrue(out.startswith("Here you go:\n") and out.endswith("Anything else?"), out)
        self.assertNotIn("- 10:00 – Shower", out)
        self.assertFalse(MissedTracker(self.sched.state_db).anything_new(at("10:10"), missed_items(self.plan, at("10:10"))))

    def test_a_reply_doesnt_ask_again_about_what_a_notice_listed(self):
        MissedTracker(self.sched.state_db).mark(at("10:00"), missed_items(self.plan, at("10:10")))
        engine = MockInferenceEngine([reply("Nice, PT logged. Did you get to Caffeinate or Throne?")])
        self.assertEqual(self.manager(engine).send_user_message("did PT"), "Nice, PT logged.")

    def test_muted_items_are_not_missed(self):
        CheckinMutes(self.sched.state_db).mute(at("23:00"), "k")
        from thirties_core.messages import missed_for
        manager = self.manager()
        self.assertNotIn("Feed kitties", [s.title for s in missed_for(manager.tools, at("10:10"))])

    def test_the_prompt_says_where_the_user_is_and_whats_been_shown(self):
        manager = self.manager()
        prompt = manager.brain.build_system_prompt()
        self.assertIn("NOW\nYou're in the 10:00 AM block: Shower, then PT.", prompt)
        self.assertIn("Not shown to the user yet: Feed kitties, Caffeinate, Throne.", prompt)
        MissedTracker(self.sched.state_db).mark(at("10:10"), missed_items(self.plan, at("10:10")))
        self.assertIn("already been shown all of these", manager.brain.build_system_prompt())


class TestItemHistory(MessagesCase):

    def test_what_is_recorded_and_nothing_made_up(self):
        manager = self.manager()
        out = manager.execute_tool("item_history", {"item": "Lunch"})
        self.assertIn("Lunch: 12:00 PM – 1:00 PM, planned.", out)
        self.assertIn("Lunch added (at 12:00 PM – 1:00 PM), caused by: background", out)  # stored with no cause
        with self.sched.state_db._get_connection() as conn:
            conn.execute("DELETE FROM plan_changes")
        out = manager.execute_tool("item_history", {"item": "Lunch"})
        self.assertIn("say you don't know", out)

    def test_list_writes_and_what_was_said(self):
        from thirties_core.joplin_writeback import WriteLog
        WriteLog(self.sched.state_db).record({"op": "complete", "note_id": "n1", "title": "Lunch",
                                              "_turn": "[telegram] had lunch"}, "Today", True,
                                             before="- [ ] Lunch", after="- [x] Lunch")
        engine = MockInferenceEngine([reply("Logged.")])
        manager = self.manager(engine)
        manager.send_user_message("lunch was great")
        out = manager.execute_tool("item_history", {"item": "Lunch"})
        self.assertIn("list write 'complete' to Today (ok), caused by: [telegram] had lunch", out)
        self.assertIn('the user said "lunch was great"', out)


if __name__ == "__main__":
    unittest.main()
