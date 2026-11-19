"""Which item a removal or move the user asked for means is the model's reading (change_asks.py, change_scope.py).

The user named two items by a word of each, the model knew which two, and the old word-matching kept refusing one
of them. Now a request this turn, or in the user's last messages, lets the model's reading stand; a removal or move
nobody asked for is still refused; and taking things off the plan never ticks them or files them as dropped.
Invented fixtures: a late-shift potter who paddles a canoe.
"""

import unittest

from tests.test_conversation import ConversationTestCase, reply
from tests.test_if_time import Base
from thirties_core.models import SpanStatus
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

ITEMS = (("Glazing: celadon test tiles", "4:40 PM"), ("Kilnwash the shelves", "5:50 PM"),
         ("Paddle the north channel", "7:10 PM"))
ASKED = "glaze bit + kiln thing: off the schedule for this shift please"


class PlannedBase(Base):
    def setUp(self):
        super().setUp()
        for title, start in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def said(self, message, tool, args, before=(), shown=""):
        """The model's call of ``tool`` for the user's ``message``, after their earlier messages ``before``."""
        self.t.recent_said = list(before)
        self.t.prior_exchange = (before[0] if before else "", shown)
        try:
            with model_call():
                return self.say(message, tool, args)
        finally:
            self.t.recent_said = None
            self.t.prior_exchange = None

    def assert_lists_untouched(self):
        """No tick anywhere, no Dropped note, and the home list as it was."""
        self.assertNotIn("[x]", self.body("n_today"))
        self.assertFalse([n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped")])
        self.assertEqual(self.body("n_errands"), "- [ ] Call the bank\n- [ ] Buy stamps\n")
        self.assertEqual([u for u in self.api.updates if u[0] == "n_errands"], [])


class TestARemovalTheUserAskedFor(PlannedBase):
    def test_items_named_by_a_word_of_each_both_come_off(self):
        for title in ("Glazing: celadon test tiles", "Kilnwash the shelves"):
            out = self.said(ASKED, "remove_event", {"event": title})
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertEqual(self.planned(title), [])
        self.assertEqual(len(self.planned("Paddle the north channel")), 1)
        self.assert_lists_untouched()

    def test_a_drop_said_for_today_is_never_filed_as_dropped(self):
        self.say("glazing and kilnwash today", "add_to_today",
                 {"items": ["Glazing: celadon test tiles", "Kilnwash the shelves"]})
        self.assertIn("Kilnwash the shelves", self.body("n_today"))
        message = "Scrap the glaze bit and that kiln thing for today, I'm wiped."
        for title in ("Glazing: celadon test tiles", "Kilnwash the shelves"):
            for span in self.planned(title):
                out = self.said(message, "remove_event", {"event": span.id})
                self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertEqual(self.planned(title), [])
            self.assertNotIn(title, self.body("n_today"))
        self.assert_lists_untouched()

    def test_asked_again_after_it_didnt_go_through_they_come_off(self):
        before = [ASKED]
        shown = "I couldn't tell which items you meant there."
        for title in ("Glazing: celadon test tiles", "Kilnwash the shelves"):
            out = self.said("you still haven't taken those off the schedule", "remove_event", {"event": title},
                            before=before, shown=shown)
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertEqual(self.planned(title), [])
        self.assert_lists_untouched()


class TestARemovalNobodyAskedFor(PlannedBase):
    def test_no_request_words_this_turn_or_before_it_stays(self):
        out = self.said("kiln is cooling nicely, what comes after 2:20?", "remove_event",
                        {"event": "Kilnwash the shelves"}, before=["paddle went great this morning"])
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.planned("Kilnwash the shelves")), 1)
        self.assertNotIn("exact", out.lower())
        self.assert_lists_untouched()

    def test_a_no_after_a_request_isnt_one(self):
        out = self.said("no, leave the kiln thing be", "remove_event", {"event": "Kilnwash the shelves"},
                        before=[ASKED])
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.planned("Kilnwash the shelves")), 1)

    def test_a_request_further_back_no_longer_counts(self):
        out = self.said("ok what's the plan after the paddle", "remove_event", {"event": "Kilnwash the shelves"},
                        before=["thanks", "sounds good", ASKED])
        self.assertTrue(out.startswith(NOT_DONE), out)


class TestAMoveTheUserAskedFor(PlannedBase):
    def test_the_model_reading_of_which_item_stands(self):
        out = self.said("could the kiln thing slide to after the paddle", "update_event",
                        {"event": "Kilnwash the shelves", "start": "7:30 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.planned("Kilnwash the shelves")[0].start_dt.strftime("%H:%M"), "19:30")

    def test_asked_just_before_it_stands(self):
        out = self.said("those two, yeah, still waiting on it", "update_event",
                        {"event": "Glazing: celadon test tiles", "start": "8:30 PM"},
                        before=["glaze bit and kiln thing: slide both past supper"])
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_nobody_asked_for_a_move_it_stays(self):
        from thirties_core.unasked import LEAVE_OUT
        was = self.planned("Paddle the north channel")[0].start_dt
        out = self.said("the celadon came out a lovely green", "update_event",
                        {"event": "Paddle the north channel", "start": "9:30 PM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(LEAVE_OUT, out)
        self.assertEqual(self.planned("Paddle the north channel")[0].start_dt, was)


class TestInAConversation(ConversationTestCase):
    """The user's earlier message reaches the check through the conversation itself."""

    def setUp(self):
        super().setUp()
        for title, start in ITEMS:
            self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})

    def planned(self, title):
        return [s for s in self.manager.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def test_a_repeat_after_a_request_that_didnt_go_through(self):
        self.manager.brain.messages += [{"role": "user", "content": ASKED},
                                        {"role": "assistant", "content": "Which ones did you mean?"}]
        self.engine.responses = [
            reply('CALL remove_event {"event": "Glazing: celadon test tiles"}'),
            reply('CALL remove_event {"event": "Kilnwash the shelves"}'),
            reply("Both are off: the celadon tiles and the kilnwash.")]
        self.manager.send_user_message("you still haven't taken those off the schedule")
        self.assertEqual(self.planned("Glazing: celadon test tiles"), [])
        self.assertEqual(self.planned("Kilnwash the shelves"), [])
        self.assertEqual(len(self.planned("Paddle the north channel")), 1)


if __name__ == "__main__":
    unittest.main()
