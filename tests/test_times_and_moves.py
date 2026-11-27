"""A time the user gives stands however they gave it, and a reply never says "couldn't move" of an item that is
where it was asked to be.

Invented items only: a bike co-op's workshop day, at odd times.

- A time the model passes from the user's words stands, even one worked out from another item ("a bit after the
  tuning ended"): Python only checks it isn't in the future and that the start is before the end, and estimates
  only when the model passes no time (said_or_estimated.py). The answer says which times were the user's.
- A move that puts an item where it was asked, or leaves it there, is a success. A line about a move that failed
  is read from the plan at the end of the turn (landed_moves.py).
"""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools


class TimesCase(unittest.TestCase):
    """Mid-afternoon at the co-op, with the tuning behind and the rims still ahead."""

    def setUp(self):
        set_debug_time(at("15:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db))
        for title, start in (("Tune the loaner bikes", "1:10 PM"), ("True the wheel rims", "5:40 PM")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, title, **times):
        self.t.user_message = message
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done", **times})
        finally:
            self.t.user_message = None


class TestATimeTheModelPassesStands(TimesCase):

    def test_a_time_given_against_another_item_is_stored_as_passed(self):
        out = self.done("the rims came out true a little past the moment the tuning ended", "True the wheel rims",
                        finished="1:50 PM", estimated=True)
        s = self.span("True the wheel rims")
        self.assertEqual(s.actual_end, at("13:50"), out)
        self.assertNotEqual(s.actual_end, s.planned_end)

    def test_both_times_passed_are_the_users_and_the_answer_says_so(self):
        out = self.done("the rims took me from about twenty past one until a quarter past two",
                        "True the wheel rims", started="1:20 PM", finished="2:15 PM")
        s = self.span("True the wheel rims")
        self.assertEqual((s.actual_start, s.actual_end), (at("13:20"), at("14:15")), out)
        self.assertEqual(s.time_source, "reported")
        self.assertIn("Start 1:20 PM is the user's; end 2:15 PM is the user's", out)

    def test_an_end_passed_with_no_start_says_the_start_is_estimated(self):
        out = self.done("trueing the rims happened once tuning had wrapped", "True the wheel rims",
                        finished="1:50 PM")
        self.assertIn("end 1:50 PM is the user's", out)
        self.assertIn("is estimated", out)
        self.assertEqual(self.span("True the wheel rims").time_source, "estimated")

    def test_no_time_passed_is_estimated_and_said_so(self):
        out = self.done("rims are done", "True the wheel rims")
        self.assertIn("estimated", out)
        self.assertLessEqual(self.span("True the wheel rims").actual_end, at("15:10"))

    def test_a_time_in_the_future_is_still_refused(self):
        out = self.done("the rims finished a while after the tuning", "True the wheel rims", finished="6:45 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("later than now", out)

    def test_a_start_that_isnt_before_the_end_is_refused(self):
        from thirties_core.assistant_tools import ToolError
        from thirties_core.said_or_estimated import start_before_end
        with self.assertRaises(ToolError) as caught:
            start_before_end(at("14:30"), at("13:50"))
        self.assertIn("isn't before the end", str(caught.exception))
        start_before_end(at("13:20"), at("13:50"))  # fine
        out = self.done("the rims took until the tuning ended, give or take", "True the wheel rims",
                        started="2:30 PM", finished="1:50 PM")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("True the wheel rims").status, SpanStatus.PLANNED)

    def test_a_time_said_as_a_clock_still_stands(self):
        self.done("wheel truing ended 2:20 by the shop clock", "True the wheel rims", finished="2:20 PM")
        self.assertEqual(self.span("True the wheel rims").actual_end, at("14:20"))


class TestAMoveThatLandedIsNeverCouldnt(ConversationTestCase):
    """The pads and the tire patch end the turn exactly where the user wanted them."""

    def setUp(self):
        super().setUp()
        t = self.manager.tools
        for title, start in (("Swap the brake pads", "10:20 AM"), ("Patch the spare tubes", "2:10 PM")):
            out = t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def start_of(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title).start_dt

    def test_an_item_moved_twice_to_the_same_place_says_nothing_about_couldnt(self):
        move = 'CALL update_event {"event": "Swap the brake pads", "start": "11:40 AM"}'
        text = "Brake pads shifted over to 11:40 AM."
        self.engine.responses = [reply(move), reply(move), reply(text), reply(text), reply(text)]
        out = self.manager.send_user_message("brake pads to 11:40")
        self.assertEqual(self.start_of("Swap the brake pads").hour, 11)
        self.assertNotIn("couldn't", out.lower(), out)
        self.assertNotIn("is still", out, out)

    def test_a_second_call_that_changes_nothing_says_nothing_about_couldnt(self):
        self.engine.responses = [
            reply('CALL update_event {"event": "Swap the brake pads", "start": "11:40 AM"}'),
            reply('CALL update_event {"event": "Patch the spare tubes", "start": "2:30 PM"}'),
            reply("Brake pads shifted over to 11:40 AM, spare tubes kept at 2:30 PM."),
            reply("Brake pads shifted over to 11:40 AM, spare tubes kept at 2:30 PM."),
            reply("Brake pads shifted over to 11:40 AM, spare tubes kept at 2:30 PM.")]
        out = self.manager.send_user_message("brake pads to 11:40, spare tubes at 2:30")
        self.assertEqual(self.start_of("Patch the spare tubes"), at("14:30"))
        self.assertNotIn("couldn't", out.lower(), out)
        self.assertNotIn("Patch the spare tubes is still", out)

    def test_an_item_moved_away_and_back_to_where_asked_is_not_a_failure(self):
        self.engine.responses = [
            reply('CALL update_event {"event": "Patch the spare tubes", "start": "3:40 PM"}'),
            reply('CALL update_event {"event": "Patch the spare tubes", "start": "2:30 PM"}'),
            reply("Spare tubes kept at 2:30 PM."), reply("Spare tubes kept at 2:30 PM."),
            reply("Spare tubes kept at 2:30 PM.")]
        out = self.manager.send_user_message("spare tubes at 2:30")
        self.assertEqual(self.start_of("Patch the spare tubes"), at("14:30"))
        self.assertNotIn("couldn't", out.lower(), out)

    def test_a_move_that_really_didnt_happen_is_still_said(self):
        said = "Spare tubes shifted much earlier."
        self.engine.responses = [reply('CALL update_event {"event": "Patch the spare tubes", "shift": "-20 hours"}'),
                                 reply(said), reply(said), reply(said)]
        out = self.manager.send_user_message("spare tubes twenty hours earlier")
        self.assertEqual(self.start_of("Patch the spare tubes"), at("14:30"))
        self.assertIn("Patch the spare tubes is still", out)
        self.assertIn("couldn't move it", out)


if __name__ == "__main__":
    unittest.main()
