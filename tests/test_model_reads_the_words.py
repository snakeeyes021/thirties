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



class TestAComplaintAboutAnItem(WordsCase):
    """A complaint about an item with the model's removal: refused as no ask."""

    def test_a_complaint_naming_it_is_the_models_reading(self):
        out = self.remove("Varnish the paddle", "how come the paddle varnish is on tonight? never planned that")
        self.assertWent(out)
        self.assertIn("Note:", out)  # the model can put it back if a move was meant
        self.assertEqual(self.planned("Varnish the paddle"), [])

    def test_an_objection_with_a_reason_takes_it_off(self):
        out = self.remove("Sand the canoe thwart", "why is the thwart sanding still there, the canoe went back")
        self.assertWent(out)
        self.assertEqual(self.planned("Sand the canoe thwart"), [])

    def test_saying_the_plan_is_too_full_lets_the_model_choose(self):
        self.assertWent(self.remove("Sand the canoe thwart", "tonight is way too packed"))
        self.assertEqual(self.planned("Sand the canoe thwart"), [])

    def test_an_ask_to_take_it_off_carries_no_note(self):
        out = self.remove("Varnish the paddle", "take the paddle varnish off")
        self.assertWent(out)
        self.assertNotIn("Note:", out)

    def test_words_with_no_basis_still_take_nothing_off(self):
        for words in ("the glaze came out a lovely green", "how come the kiln ran so hot?"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Varnish the paddle", words))
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)



class TestEverydayWaysOfSayingDone(WordsCase):
    """A finish said in everyday words, or reported without a done word, was refused as no done said."""

    def test_everyday_done_words_tick(self):
        for title, words in (("Varnish the paddle", "squared away the paddle varnish"),
                             ("Sand the canoe thwart", "knocked out the thwart sanding"),
                             ("Kiln wash the shelves", "dealt with the kiln wash"),
                             ("Varnish the paddle", "that's one less thing, the paddle varnish is in"),
                             ("Sand the canoe thwart", "thwart sanding is out of the way")):
            with self.subTest(words=words):
                self.fresh_item(title)
                self.assertWent(self.tick(title, words))

    def test_a_report_without_a_done_word_ticks(self):
        for title, words in (("Varnish the paddle", "I've given the paddle its varnish coat"),
                             ("Sand the canoe thwart", "the canoe thwart has been sanded smooth")):
            with self.subTest(words=words):
                self.fresh_item(title)
                self.assertWent(self.tick(title, words))

    def test_not_ahead_asked_or_onward_still_ticks_nothing(self):
        for title, words in (("Sand the canoe thwart", "the thwart sanding isn't squared away"),
                             ("Varnish the paddle", "once the paddle varnish is dealt with I'll start on the oars"),
                             ("Kiln wash the shelves", "has the kiln wash been dealt with?"),
                             ("Sand the canoe thwart", "paddle varnish knocked out, on to the thwart sanding"),
                             ("Varnish the paddle", "I haven't given the paddle its varnish coat")):
            with self.subTest(words=words):
                self.fresh_item(title)
                self.assertHeld(self.tick(title, words))

    def test_the_list_tick_goes_through_on_the_same_words(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Mail the glaze samples"}
        for words in ("the glaze samples are in", "glaze samples: squared away", "I've sent the glaze samples off"):
            self.assertEqual(refused(op, f"[app] {words}", None), "", words)
        for words in ("the glaze samples aren't in", "once the glaze samples are dealt with", "the glaze samples"):
            self.assertNotEqual(refused(op, f"[app] {words}", None), "", words)

    def fresh_item(self, title):
        for s in self.planned(title):
            s.status = s.status.PLANNED



class TestADoneBesideHowItWent(WordsCase):
    """"Fine" or "good" beside a done word refused the done as if the item were still going."""

    def test_the_done_wins_over_fine(self):
        for title, words in (("Varnish the paddle", "the paddle varnish went fine, all wrapped"),
                             ("Sand the canoe thwart", "thwart sanding was ok and it's squared away"),
                             ("Kiln wash the shelves", "kiln wash turned out good, done")):
            with self.subTest(words=words):
                for s in self.planned(title):
                    s.status = s.status.PLANNED
                self.assertWent(self.tick(title, words))

    def test_going_or_fine_alone_still_ticks_nothing(self):
        for title, words in (("Sand the canoe thwart", "the thwart sanding is going"),
                             ("Varnish the paddle", "the paddle varnish is fine"),
                             ("Kiln wash the shelves", "kiln wash is going fine, about done"),
                             ("Varnish the paddle", "the paddle varnish is fine, the thwart sanding is done")):
            with self.subTest(words=words):
                self.assertHeld(self.tick(title, words))

    def test_the_list_tick_follows_the_same_reading(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Mail the glaze samples"}
        self.assertEqual(refused(op, "[app] the glaze samples went fine, all wrapped", None), "")
        self.assertNotEqual(refused(op, "[app] the glaze samples are fine", None), "")



class TestAWordForWhichKind(WordsCase):
    """A word saying which one ("the ash paddle") was read as naming something else, and the done refused."""

    def listed(self, *titles):
        from thirties_core.models import TaskItem
        tasks = [TaskItem(id=f"t_{i}", source_notebook="1. Tasks", title=t, is_checkbox_item=True,
                          parent_note_title="Workshop", parent_note_id="n_workshop") for i, t in enumerate(titles)]
        for t in tasks:
            t.list_role = "list"
        self.manager.tools.assistant.s.tasks[:] = tasks
        return tasks

    def test_which_kind_ticks_the_item(self):
        self.assertWent(self.tick("Varnish the paddle", "the ash paddle is varnished"))

    def test_another_item_with_that_word_still_holds_it(self):
        self.listed("Oil the ash paddle")
        out = self.tick("Varnish the paddle", "the ash paddle is varnished")
        self.assertHeld(out)
        self.assertIn("Oil the ash paddle", out)  # the list item that fits all the words is named

    def test_a_different_thing_named_still_holds_it(self):
        self.assertHeld(self.tick("Varnish the paddle", "the paddle rack is varnished"))

    def test_what_says_which_kind(self):
        from thirties_core.done_fit import names_something_else
        self.assertEqual(names_something_else("the ash paddle is varnished", "Varnish the paddle"), [])
        self.assertEqual(names_something_else("finished the stoneware bisque load", "Bisque load"), [])
        self.assertTrue(names_something_else("the ash paddle is varnished", "Varnish the paddle", [],
                                             ["Split the ash logs"]))
        self.assertTrue(names_something_else("the paddle rack is varnished", "Varnish the paddle"))


if __name__ == "__main__":
    unittest.main()
