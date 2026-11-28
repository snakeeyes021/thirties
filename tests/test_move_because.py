"""A move the user's words ask for goes ahead when the model quotes those words (because), whatever words they used.

Judging from the wording whether a message asks for a move failed both ways: a placement said relative to another
item uses no move word, so every move the model made for it was refused and the user had to say it again; and a
general ask to rearrange around a new event named no meal, so the meal's move was refused for its window. Now
update_event takes ``because``, the exact span of the user's words that ask for the change, and Python only checks
that the quote is really theirs (add_source.py). The envelope, calendar events, what the user said holds every day,
and the meal window stay as they were when no real quote asks for the change. A move to another day is judged on
that day's meal window, not today's. Invented items only: volunteers at a bike co-op.
"""

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE


class MoveCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.manager.tools
        for title, start, length in (("Grease the hub", "10:20 AM", "30 min"), ("Lunch", "12:00 PM", "30 min"),
                                     ("True the wheel", "1:20 PM", "30 min")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def say(self, message, args, tool="update_event"):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def start(self, title):
        return next(s.start_dt for s in self.manager.day_plan.spans if s.title == title)


class TestAPlacementWithNoMoveWord(MoveCase):
    SAID = "The sticky part belongs just past the 2:40 volunteer huddle."

    def test_the_move_goes_ahead_with_a_quote_of_the_words(self):
        out = self.say(self.SAID, {"event": "True the wheel", "start": "3:00 PM",
                                   "because": "belongs just past the 2:40 volunteer huddle"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.start("True the wheel"), at("15:00"))

    def test_with_no_quote_it_is_refused_and_the_model_is_told_how_to_cite(self):
        out = self.say(self.SAID, {"event": "True the wheel", "start": "3:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("hasn't asked for anything to move", out)
        self.assertIn("because", out)
        self.assertIn("Don't mention this move", out)  # a move nobody asked for stays unannounced
        self.assertEqual(self.start("True the wheel"), at("13:30"))

    def test_a_quote_the_user_never_wrote_is_refused(self):
        out = self.say(self.SAID, {"event": "True the wheel", "start": "3:00 PM",
                                   "because": "ratchet straps are stacked by the door"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.start("True the wheel"), at("13:30"))

    def test_a_quote_from_the_users_earlier_message_counts(self):
        self.t.prior_exchange = (self.SAID, "Noted.")
        out = self.say("Cheers, back after the break.", {"event": "True the wheel", "start": "3:00 PM",
                                                         "because": "just past the 2:40 volunteer huddle"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.start("True the wheel"), at("15:00"))


class TestAMealPastItsWindow(MoveCase):
    SAID = "Rearrange whatever clashes with the new spoke clinic."

    def test_a_bulk_ask_with_a_quote_lets_a_meal_go_past_its_window(self):
        out = self.say(self.SAID, {"event": "Lunch", "start": "2:40 PM",
                                   "because": "Rearrange whatever clashes with the new spoke clinic"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertGreaterEqual(self.start("Lunch"), at("14:30"))

    def test_without_a_quote_the_window_holds(self):
        out = self.say(self.SAID, {"event": "Lunch", "start": "2:40 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("window ends at 2:00 PM", out)
        self.assertIn("because", out)

    def test_a_quote_the_user_never_wrote_leaves_the_window_in_place(self):
        out = self.say(self.SAID, {"event": "Lunch", "start": "2:40 PM", "because": "spare inner tubes are on the shelf"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.start("Lunch"), at("12:00"))


class TestAMoveToAnotherDay(MoveCase):
    SAID = "Clear today, everything goes to tomorrow."

    def tomorrows(self, title):
        plan = self.t.plan_for("tomorrow")
        return next((s for s in plan.spans if s.title == title), None)

    def test_a_meal_moved_to_tomorrow_is_judged_on_tomorrows_window(self):
        out = self.say(self.SAID, {"event": "Lunch", "to_day": "tomorrow", "because": "everything goes to tomorrow"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIsNotNone(self.tomorrows("Lunch"), out)

    def test_a_time_past_that_days_window_is_still_held(self):
        out = self.say(self.SAID, {"event": "Lunch", "to_day": "tomorrow", "start": "3:00 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("window ends at 2:00 PM", out)
        self.assertIsNone(self.tomorrows("Lunch"))

    def test_an_item_with_no_move_word_goes_to_tomorrow_on_a_quote(self):
        said = "The wheel business can wait till the day after today."
        out = self.say(said, {"event": "True the wheel", "to_day": "tomorrow",
                              "because": "can wait till the day after today"})
        self.assertFalse(out.startswith(NOT_DONE), out)  # to a later day this week (the user didn't say tomorrow)
        self.assertNotIn("True the wheel", [s.title for s in self.manager.day_plan.spans])


class TestTheOtherFloorsStay(MoveCase):
    def test_work_still_changes_only_when_the_user_talks_about_it(self):
        said = "The sticky part belongs just past the 2:40 volunteer huddle."
        out = self.say(said, {"event": "Work", "end": "5:00 PM", "because": "belongs just past the 2:40 volunteer huddle"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_what_the_user_said_holds_every_day_still_needs_the_item_named(self):
        out = self.t.execute("set_item_facts", {"event": "Grease the hub", "every_day": "true", "at": "10:20 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        said = "The sticky part belongs just past the 2:40 volunteer huddle."
        out = self.say(said, {"event": "Grease the hub", "start": "3:00 PM",
                              "because": "belongs just past the 2:40 volunteer huddle"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("every day", out)
        self.assertEqual(self.start("Grease the hub"), at("10:30"))
