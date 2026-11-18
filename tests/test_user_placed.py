"""Where the user put an item, it stays; an item under way keeps its start; "not yet" never takes an item off.

Seen in simulated days: the user moved an item to an hour they were happy with, then started the item planned after
it early; starting it pushed theirs an hour and a half later. An item the user said was under way was moved to the
afternoon in a reply about when it ends. An item the user said wasn't due yet came off the plan. Now:

- starting something now leaves an item the user placed (by naming it in a move, or by giving its time) where it is,
  alongside, and says so;
- an item under way keeps its start unless the user gives it another time; its end can change;
- "it isn't time for X yet" keeps X on the plan.

On invented items: someone who works a late shift at a pottery studio and paddles a canoe on days off.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE


class PlacedCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.manager.tools
        out = self.t.execute("update_event", {"event": "Work", "start": "11:00 AM", "end": "6:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        for title, start, length in (("Wax the canoe paddle", "6:30 PM", "30 min"),
                                     ("Sort the glaze tests", "7:30 PM", "90 min")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def say(self, message, tool, args):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def span(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title)


class TestStartingSomethingEarly(PlacedCase):

    def test_an_item_the_user_moved_by_name_stays(self):
        out = self.say("could the paddle job slide a little later, once supper's eaten?", "update_event",
                       {"event": "Wax the canoe paddle", "start": "7:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("19:05"))
        out = self.say("diving into the glaze test tiles right away", "doing_now", {"item": "Sort the glaze tests"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Sort the glaze tests").start_dt, at("19:00"))
        self.assertEqual(self.span("Wax the canoe paddle").start_dt, at("19:00"))  # not pushed to 8:30
        self.assertIn("Wax the canoe paddle stays at 7:00 PM", out)

    def test_an_item_at_a_time_the_user_gave_stays(self):
        out = self.say("paddle waxing at 7 please", "update_event", {"event": "Wax the canoe paddle", "start": "7:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("19:05"))
        self.say("diving into the glaze test tiles right away", "doing_now", {"item": "Sort the glaze tests"})
        self.assertEqual(self.span("Wax the canoe paddle").start_dt, at("19:00"))

    def test_what_thirties_placed_itself_still_makes_room(self):
        out = self.t.execute("update_event", {"event": "Wax the canoe paddle", "start": "7:00 PM"})  # nobody talking
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("19:05"))
        self.say("diving into the glaze test tiles right away", "doing_now", {"item": "Sort the glaze tests"})
        self.assertEqual(self.span("Wax the canoe paddle").start_dt, at("20:30"))

    def test_in_a_turn_with_the_model(self):
        self.say("could the paddle job slide a little later, once supper's eaten?", "update_event",
                 {"event": "Wax the canoe paddle", "start": "7:00 PM"})
        set_debug_time(at("19:05"))
        self.engine.responses = [reply('CALL doing_now {"item": "Sort the glaze tests"}'),
                                 reply("Glaze tests are under way now.")]
        out = self.manager.send_user_message("diving into the glaze test tiles right away")
        self.assertIn("Glaze tests are under way now.", out)
        self.assertEqual(self.span("Wax the canoe paddle").start_dt, at("19:00"))


class TestAnItemUnderWay(PlacedCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:10"))
        out = self.say("loading the test kiln now, it fires for a couple of hours", "doing_now",
                       {"item": "Fire the test kiln", "duration": "2 hours"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("09:20"))

    def test_its_start_stays_on_a_question_about_its_end(self):
        out = self.say("hmm the test kiln really needs closer to three hours, when does that put it?", "update_event",
                       {"event": "Fire the test kiln", "start": "7:00 PM", "duration": "3 hours"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("under way", out)
        self.assertEqual(self.span("Fire the test kiln").start_dt, at("09:00"))

    def test_its_end_can_change(self):
        out = self.say("the test kiln runs closer to three hours", "update_event",
                       {"event": "Fire the test kiln", "duration": "3 hours"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        kiln = self.span("Fire the test kiln")
        self.assertEqual(kiln.start_dt, at("09:00"))
        self.assertEqual(kiln.end_dt, at("12:00"))

    def test_a_new_time_the_user_gives_moves_it(self):
        out = self.say("scrap that, I'll fire the test kiln at 7 tonight instead", "update_event",
                       {"event": "Fire the test kiln", "start": "7:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Fire the test kiln").start_dt, at("19:00"))


class TestNotYet(PlacedCase):

    def setUp(self):
        super().setUp()
        out = self.t.execute("add_event", {"title": "Drive to the lake", "start": "8:00 AM", "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("08:05"))

    def test_not_yet_keeps_it(self):
        out = self.say("whoa, the lake drive isn't yet, I'm still packing the dry bag", "remove_event",
                       {"event": "Drive to the lake"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertTrue(any(s.title == "Drive to the lake" for s in self.manager.day_plan.spans))

    def test_a_clear_word_still_takes_it_off(self):
        out = self.say("cancel the lake drive, the wind is up", "remove_event", {"event": "Drive to the lake"})
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
