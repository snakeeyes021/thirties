"""The model's reading stands, but a write still needs words behind it.

The model reads indirect asks, idioms and paraphrases (test_model_reads_the_words.py). These are the cases where the
words give no basis at all for the change, or say something else: an add nobody's words touch, a give-up or a
grumble read as a finish, a keep read as a removal, a busy van read as a busy day, a question read as a done. Each
stays refused, and the readings around it still go through. Invented data throughout.
"""

import unittest

from tests.fixtures import at
from tests.test_leave_today import LeaveBase
from tests.test_model_reads_the_words import WordsCase
from thirties_core.astronomy import set_debug_time


class AddCase(WordsCase):
    def add(self, title, words, start="9:10 PM", **more):
        from thirties_core.plan_log import model_call
        with model_call():
            return self.said("add_event", {"title": title, "start": start, "duration": "30 min", **more}, words)


class TestAGiveUpOrAGrumbleIsNoDone(WordsCase):
    """Phrases of giving up, grumbling or place read as a finish, and ticked the item."""

    NOT_DONE = ("I've given up on the kiln permit", "I gave up on the kiln permit",
                "the kiln permit has bugged me all week", "I've dreaded the kiln permit", "I've skipped the kiln permit",
                "dealt with the kiln permit badly", "the kiln permit folder is out of the way of the door",
                "the fumes knocked me out, kiln permit later", "the kiln permit is in, or so I thought",
                "the kiln permit is in the mail")

    def test_the_list_tick_is_refused(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Mail the kiln permit"}
        for words in self.NOT_DONE + ("the kiln door stuck again",):
            with self.subTest(words=words):
                self.assertNotEqual(refused(op, f"[app] {words}", None), "")

    def test_says_done_reads_none_of_them_as_done(self):
        from thirties_core.write_guard import says_done
        for words in self.NOT_DONE:
            with self.subTest(words=words):
                self.assertFalse(says_done(words, "Mail the kiln permit"))
        self.assertFalse(says_done("the kiln door stuck again", "Fix the kiln door"))

    def test_a_report_must_name_it_or_point_back(self):
        from thirties_core.write_guard import says_done
        self.assertFalse(says_done("the kiln permit is due friday, I've written to the clay supplier",
                                   "Mail the kiln permit"))
        self.assertTrue(says_done("the kiln permit, I've sent it", "Mail the kiln permit"))
        self.assertTrue(says_done("I've mailed it", "Mail the kiln permit"))

    def test_a_report_ahead_of_time_ticks_nothing(self):
        from thirties_core.write_guard import says_done
        for words in ("once I've written the glaze notes I'll rest", "when I've written up the glaze notes"):
            with self.subTest(words=words):
                self.assertFalse(says_done(words, "Write the glaze notes"))
        self.assertTrue(says_done("I've written the glaze notes", "Write the glaze notes"))

    def test_the_turn_tick_is_refused(self):
        for words in ("I've given up on the paddle varnish", "the paddle varnish has bugged me all week",
                      "I've skipped the paddle varnish", "dealt with the paddle varnish badly",
                      "the paddle varnish is in, or so I thought"):
            with self.subTest(words=words):
                self.assertHeld(self.tick("Varnish the paddle", words))


class TestAQuestionIsNoDone(WordsCase):
    """A question with a change word in it counted as a request, and its done word as the user's done."""

    def test_questions_that_only_ask(self):
        from thirties_core.said_for_it import asks_for_change
        for asks in ("why not?", "why is the kiln still hot?", "can I get the paddle varnish done in 30 minutes?",
                     "can the kiln wash wait till the shelves are done?", "what about the kiln wash?",
                     "do we still have sandpaper?"):
            self.assertFalse(asks_for_change(asks), asks)
        for change in ("can you drop the kiln wash?", "can we move the thwart sanding to 8:40?",
                       "shouldn't we drop the kiln wash?", "why is the kiln wash still on today?",
                       "why is the thwart sanding still on the plan?", "can we take the kiln wash off?",
                       "could we push the paddle varnish?", "can we skip the thwart sanding?"):
            self.assertTrue(asks_for_change(change), change)

    def test_a_done_word_inside_a_question_ticks_nothing(self):
        for title, words in (("Varnish the paddle", "can I get the paddle varnish done in 30 minutes?"),
                             ("Kiln wash the shelves", "can the kiln wash wait till the shelves are done?"),
                             ("Varnish the paddle", "shouldn't the paddle varnish be done by now?")):
            with self.subTest(words=words):
                self.assertHeld(self.tick(title, words))

    def test_the_list_tick_reads_a_question_the_same_way(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Varnish the paddle"}
        self.assertNotEqual(refused(op, "[app] can I get the paddle varnish done in 30 minutes?", None), "")


class TestAWordForWhichKindIsAName(WordsCase):
    """An extra word ahead of the item's verb was read as saying which kind of item."""

    def test_a_shared_verb_is_no_kind(self):
        from thirties_core.done_fit import names_something_else
        self.assertTrue(names_something_else("the car wash is done", "Kiln wash the shelves"))
        self.assertTrue(names_something_else("the cabinet paint is bought", "Paint the fence"))
        self.assertTrue(names_something_else("the ash paddle is varnished, the paddle rack too", "Varnish the paddle"))
        self.assertEqual(names_something_else("the ash paddle is varnished", "Varnish the paddle"), [])

    def test_the_tick_is_refused(self):
        self.assertHeld(self.tick("Kiln wash the shelves", "the car wash is done"))


class TestADoneBesideFineIsAbout(WordsCase):
    """A done for another thing beside "fine" ticked the item said to be fine."""

    def test_the_list_tick_is_refused(self):
        from thirties_core.write_guard import refused, says_done
        op = {"op": "complete", "title": "Varnish the paddle"}
        for words in ("the paddle varnish is fine, the thwart sanding is done",
                      "the paddle varnish is fine, the dishes are done"):
            with self.subTest(words=words):
                self.assertFalse(says_done(words, "Varnish the paddle"))
                self.assertNotEqual(refused(op, f"[app] {words}", None), "")
        self.assertEqual(refused(op, "[app] the paddle varnish went fine, all wrapped", None), "")

    def test_the_turn_tick_is_refused(self):
        self.assertHeld(self.tick("Varnish the paddle", "the paddle varnish is fine, the dishes are done"))


if __name__ == "__main__":
    unittest.main()
