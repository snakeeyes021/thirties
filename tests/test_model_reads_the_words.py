"""The model reads what the user's words mean; Python refuses a change only when the words give it no basis at all.

Each case shows both sides: the model's reading of an indirect ask, a complaint, an idiom or a paraphrase goes
through, and a change with nothing in the words behind it is still refused. Invented data throughout.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE


class WordsCase(ConversationTestCase):
    """An evening with a few workshop items on it, the morning already sent."""

    ITEMS = (("Kiln wash the shelves", "4:40 PM"), ("Varnish the paddle", "6:10 PM"), ("Sand the canoe thwart", "7:20 PM"))

    def setUp(self):
        super().setUp()
        self.pt = self.manager.tools
        for title, start in self.ITEMS:
            out = self.pt.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)
        from thirties_core import morning
        morning.mark_done(self.pt._state_db, self.pt.day_plan.target_date, at("07:50"))

    def planned(self, title):
        return [s for s in self.manager.day_plan.spans if s.title == title]

    def said(self, tool, args, words, shown=""):
        self.pt.user_message = words
        self.pt.prior_exchange = ("", shown)
        try:
            return self.pt.execute(tool, args)
        finally:
            self.pt.user_message = None
            self.pt.prior_exchange = None

    def remove(self, title, words):
        return self.said("remove_event", {"event": title}, words)

    def tick(self, title, words, when="20:10"):
        set_debug_time(at(when))
        return self.said("log_outcome", {"event": title, "status": "done"}, words)

    def assertWent(self, out):
        self.assertFalse(out.startswith(NOT_DONE), out)

    def assertHeld(self, out):
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestAQuestionThatAsksForAChange(WordsCase):
    """An indirect ask worded as a question was read as a question only, and the model's change refused."""

    def test_what_asks_for_a_change(self):
        from thirties_core.said_for_it import asks_for_change, is_asking
        for asks in ("shouldn't we drop the kiln wash today?", "can the paddle varnish go?",
                     "why is the thwart sanding still on?", "how about we lose the kiln wash?",
                     "any reason to keep the paddle varnish?", "couldn't the thwart go at 8:30?"):
            self.assertTrue(asks_for_change(asks), asks)
            self.assertFalse(is_asking(asks), asks)
        for only_asks in ("is the kiln wash at 4:40?", "did I already varnish the paddle?", "what's after the thwart?",
                          "which would you take off?", "how long is the paddle varnish?",
                          "should the kiln wash come off?", "should I move the thwart to 8:30?"):
            self.assertFalse(asks_for_change(only_asks), only_asks)
            self.assertTrue(is_asking(only_asks), only_asks)

    def test_a_suggestion_takes_it_off(self):
        self.assertWent(self.remove("Kiln wash the shelves", "shouldn't we drop the kiln wash today?"))
        self.assertEqual(self.planned("Kiln wash the shelves"), [])

    def test_can_it_go_takes_it_off(self):
        self.assertWent(self.remove("Varnish the paddle", "can the paddle varnish go?"))
        self.assertEqual(self.planned("Varnish the paddle"), [])

    def test_a_suggested_move_goes_ahead(self):
        out = self.said("update_event", {"event": "Sand the canoe thwart", "start": "8:30 PM"},
                        "couldn't the thwart go at 8:30?")
        self.assertWent(out)
        self.assertEqual(self.planned("Sand the canoe thwart")[0].start_dt, at("20:30"))

    def test_a_tick_asked_for_in_a_question_goes_ahead(self):
        self.assertWent(self.tick("Varnish the paddle", "can we mark the paddle varnish off?"))

    def test_a_plain_question_still_takes_nothing_off(self):
        self.assertHeld(self.remove("Kiln wash the shelves", "is the kiln wash at 4:40?"))
        self.assertEqual(len(self.planned("Kiln wash the shelves")), 1)
        self.assertHeld(self.remove("Kiln wash the shelves", "which would you take off?"))
        self.assertEqual(len(self.planned("Kiln wash the shelves")), 1)

    def test_a_plain_question_still_ticks_nothing(self):
        self.assertHeld(self.tick("Varnish the paddle", "did I already varnish the paddle?"))
        self.assertHeld(self.tick("Varnish the paddle", "is the paddle varnish ticked?"))


if __name__ == "__main__":
    unittest.main()
