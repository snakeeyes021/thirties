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


class TestAnAddNeedsWordsThatTouchIt(AddCase):
    """Any message with a sentence in it let the model add an item nobody's words touched."""

    def test_an_add_in_a_turn_about_something_else_is_refused(self):
        for words in ("move the paddle varnish to 8:40", "the thwart sanding is done",
                      "kiln wash went fine, all wrapped", "I need to push the paddle varnish to 8:40"):
            with self.subTest(words=words):
                self.assertHeld(self.add("Polish the brass hinges", words))
                self.assertEqual(self.planned("Polish the brass hinges"), [])

    def test_a_reworded_second_copy_is_refused(self):
        for title in ("Varnish paddle", "Varnish the paddles"):
            with self.subTest(title=title):
                self.assertHeld(self.add(title, "move the paddle varnish to 8:40", start="8:40 PM", again=True))
        for again in (True, False):
            with self.subTest(again=again):
                self.assertHeld(self.add("Paddle varnish", "the thwart sanding is done", start="8:40 PM",
                                         again=again))
        self.assertEqual(len([s for s in self.manager.day_plan.spans if "arnish" in s.title]), 1)

    def test_a_reworded_copy_named_with_a_new_time_moves_the_one_copy(self):
        out = self.add("Paddle varnish", "the paddle varnish at 8:40 instead", start="8:40 PM")
        self.assertWent(out)
        varnish = [s for s in self.manager.day_plan.spans if "arnish" in s.title]
        self.assertEqual(len(varnish), 1)
        self.assertEqual(varnish[0].start_dt, at("20:40"))

    def test_a_second_session_asked_for_goes_on(self):
        self.assertWent(self.add("Varnish paddle", "another paddle varnish coat at 8:40", start="8:40 PM",
                                 again=True))
        self.assertEqual(len([s for s in self.manager.day_plan.spans if "arnish" in s.title]), 2)

    def test_an_errand_said_goes_on_under_the_models_name(self):
        for title, words, start in (("Sharpener pickup", "I still have to collect my chisels from the sharpener before 9:10", "9:10 PM"),
                             ("Hardware store run", "need a tin of spar varnish and some sandpaper tonight", "9:40 PM"),
                             ("Polish the brass hinges", "the brass hinges could use a polish later", "10:10 PM"),
                             ("Sweep the shop", "fit something quick in after the thwart", "10:40 PM")):
            with self.subTest(words=words):
                self.assertWent(self.add(title, words, start=start))
                self.assertEqual(len(self.planned(title)), 1)


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


class TestATickAskedInAQuestion(WordsCase):
    """A tick asked for in a question went through on the plan and was refused on the list."""

    def test_both_agree(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Varnish the paddle"}
        self.assertEqual(refused(op, "[app] can we mark the paddle varnish off?", None), "")
        self.assertWent(self.tick("Varnish the paddle", "can we mark the paddle varnish off?"))

    def test_a_tick_asked_for_another_item_is_not_this_ones(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Varnish the paddle"}
        self.assertNotEqual(refused(op, "[app] can we mark the thwart sanding off?", None), "")
        self.assertHeld(self.tick("Varnish the paddle", "can we mark the thwart sanding off?"))


class TestStartedBesideAnotherDone(WordsCase):
    """A done word about something else in the same clause made a start read as a finish."""

    def test_started_stays_started(self):
        from thirties_core.started import not_finished
        words = "started the paddle varnish right after I picked up the brush"
        self.assertEqual(not_finished(words, "Varnish the paddle"), "begun")
        self.assertHeld(self.tick("Varnish the paddle", words))

    def test_a_done_about_the_item_still_counts(self):
        from thirties_core.started import not_finished
        self.assertEqual(not_finished("the paddle varnish I started at 6:10 is done", "Varnish the paddle"), "")


if __name__ == "__main__":
    unittest.main()
