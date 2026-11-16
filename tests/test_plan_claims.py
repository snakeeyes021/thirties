"""What a reply says was moved, finished or taken off is checked against the plan and today's list.

Seen in simulated days: "I've moved it off today's list" over an item still on the list; a calendar event said to
end at the half hour after its real end (that block's end passed as its time); and a sentence about two moves, one
of which never happened, passing because the other did. A move that did happen inside the same half hour is never
sent back. On invented items only.
"""

from unittest.mock import MagicMock

from tests.fixtures import DAY, at, event
from tests.test_conversation import ConversationTestCase, reply
from tests.test_today import TodayTestCase
from thirties_core.inference import MockInferenceEngine
from thirties_core.plan_tools import NOT_DONE


class TestClaimsAboutThePlan(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(events=[event("cal-dentist", "14:00", "14:45", "Dentist check-up")])
        for title, start in (("Sand the bench", "10:00 AM"), ("Wash the van", "5:00 PM")):
            out = self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title)

    def test_a_calendar_event_said_to_end_at_its_blocks_end_is_sent_back(self):
        true = "Your Dentist check-up is 2:00 PM to 2:45 PM, as on your calendar."
        self.engine.responses = [reply("Your Dentist check-up is set from 2:00 PM to 3:00 PM on your calendar."),
                                 reply(true)]
        out = self.manager.send_user_message("when's the dentist again?")
        self.assertEqual(out, true)

    def test_its_real_times_pass(self):
        said = "Your Dentist check-up is 2:00 PM to 2:45 PM."
        self.engine.responses = [reply(said)]
        self.assertEqual(self.manager.send_user_message("when's the dentist again?"), said)

    def test_two_moves_in_one_sentence_when_one_didnt_happen(self):
        """The bench's end, which the user gave, is 10:45: its half hour ends at 11:00, the time claimed for it."""
        t = self.manager.tools
        t.user_message = "the bench is 10 to 10:45"
        try:
            t.execute("update_event", {"event": "Sand the bench", "start": "10:00 AM", "end": "10:45 AM"})
        finally:
            t.user_message = None
        self.assertEqual(self.span("Sand the bench").end_dt, at("10:45"))
        true = "I moved Wash the van to 6:00 PM; Sand the bench is still at 10:00 AM."
        self.engine.responses = [
            reply('CALL update_event {"event": "Wash the van", "start": "6:00 PM"}'),
            reply("I moved Sand the bench to 11:00 AM and Wash the van to 6:00 PM."),
            reply(true)]
        out = self.manager.send_user_message("bench at 11 and the van at 6 pls")
        self.assertEqual(self.span("Sand the bench").start_dt, at("10:00"))
        self.assertEqual(out, true)

    def test_a_move_inside_the_same_half_hour_is_not_sent_back(self):
        said = "Moved Sand the bench to 10:15 AM.\n\n10:00 AM – Sand the bench"
        self.engine.responses = [reply('CALL update_event {"event": "Sand the bench", "start": "10:15 AM"}'),
                                 reply(said)]
        out = self.manager.send_user_message("bench at 10:15 instead")
        self.assertEqual(self.span("Sand the bench").start_dt, at("10:15"))
        self.assertEqual(out, said)
        self.assertEqual(self.engine.responses, [])

    def test_a_claimed_move_that_was_refused_is_sent_back(self):
        true = "Your Dentist check-up comes from your calendar, so it's still at 2:00 PM."
        self.engine.responses = [
            reply('CALL update_event {"event": "Dentist check-up", "start": "3:00 PM"}'),
            reply("Moved your Dentist check-up to 3:00 PM."),
            reply(true)]
        out = self.manager.send_user_message("can the dentist go later")
        self.assertEqual(out, true)

    def test_a_claimed_done_that_didnt_happen_is_sent_back(self):
        true = "Sand the bench isn't marked yet: did you finish it?"
        self.engine.responses = [
            reply('CALL log_outcome {"event": "Wash the van", "status": "done"}'),
            reply("Sand the bench and Wash the van are marked done."),
            reply(true)]
        out = self.manager.send_user_message("van's washed")
        self.assertEqual(out, true)


class TestClaimsAboutTodaysList(TodayTestCase):
    def setUp(self):
        super().setUp()
        from thirties_core.conversation import ConversationManager
        self.engine = MockInferenceEngine()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, self.services.tasks, pending, self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)
        self.engine.responses = [reply("Morning!")]
        self.m.send_user_message("morning")  # the day's first message (its set-up places what's on Today)
        t = self.m.tools
        t.execute("add_event", {"title": "Fold the towels", "start": "2:00 PM", "duration": "30 min"})
        t.execute("add_event", {"title": "Wash the van", "start": "5:00 PM", "duration": "30 min"})
        t.by_hand = True
        try:
            t.execute("remove_event", {"event": "Fold the towels"})  # off the schedule, still on today's list
        finally:
            t.by_hand = False
        self.assertIn("- [ ] Fold the towels", self.body("n_today"))

    def test_saying_its_off_the_list_while_it_is_still_on_is_sent_back(self):
        true = "Fold the towels is still on today's list with no time. Want it off the list?"
        self.engine.responses = [reply('CALL update_event {"event": "Wash the van", "start": "6:00 PM"}'),
                                 reply("The van is at 6:00 PM now. I've also moved the towels off today's list."),
                                 reply(true)]
        out = self.m.send_user_message("van at 6. and the towels, off the list")
        self.assertEqual(out, true)

    def test_once_it_is_off_the_claim_passes(self):
        said = "Done: the towels are off today's list."
        self.engine.responses = [reply('CALL remove_event {"event": "Fold the towels"}'), reply(said)]
        out = self.m.send_user_message("take the towels off the list")
        self.assertNotIn("Fold the towels", self.body("n_today"))
        self.assertEqual(out, said)


if __name__ == "__main__":
    import unittest
    unittest.main()
