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

    def test_a_plain_question_still_ticks_nothing_here_too(self):
        # taking it off today on a question is the model's reading; a tick on one is still held
        self.assertHeld(self.tick("Kiln wash the shelves", "is the kiln wash at 4:40?"))

    def test_a_removal_said_in_a_question_is_the_models_reading(self):
        # no wording check says no to a removal: the model read the user's question as the ask, and it stands
        self.assertWent(self.remove("Kiln wash the shelves", "why on earth is the kiln wash still there? take it off?"))
        self.assertEqual(self.planned("Kiln wash the shelves"), [])

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

    def test_asking_for_less_on_a_full_plan_lets_the_model_choose(self):
        # which item a full day gives up is the model's reading on today's plan (it can be put back)
        self.assertWent(self.remove("Sand the canoe thwart", "tonight is way too packed"))
        self.assertEqual(self.planned("Sand the canoe thwart"), [])

    def test_an_ask_to_take_it_off_carries_no_note(self):
        out = self.remove("Varnish the paddle", "take the paddle varnish off")
        self.assertWent(out)
        self.assertNotIn("Note:", out)

    def test_on_today_the_models_reading_stands_and_says_what_came_off(self):
        out = self.remove("Varnish the paddle", "how come the kiln ran so hot?")
        self.assertWent(out)
        self.assertIn("Removed Varnish the paddle", out)



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

    def test_another_item_with_that_word_carries_a_note(self):
        self.listed("Oil the ash paddle")
        out = self.tick("Varnish the paddle", "the ash paddle is varnished")
        self.assertWent(out)
        self.assertIn("Oil the ash paddle", out)  # the list item that fits all the words is named

    def test_a_different_thing_said_goes_on_with_a_note(self):
        out = self.tick("Varnish the paddle", "the paddle rack is varnished")
        self.assertWent(out)
        self.assertIn('"paddle rack"', out)  # whether they meant another item is the model's to read

    def test_what_says_which_kind(self):
        from thirties_core.done_fit import names_something_else
        self.assertEqual(names_something_else("the ash paddle is varnished", "Varnish the paddle"), [])
        self.assertEqual(names_something_else("finished the stoneware bisque load", "Bisque load"), [])
        self.assertTrue(names_something_else("the ash paddle is varnished", "Varnish the paddle", [],
                                             ["Split the ash logs"]))
        self.assertTrue(names_something_else("the paddle rack is varnished", "Varnish the paddle"))



class TestAnAddTheModelNames(WordsCase):
    """An add the model named in its own words was refused because its title wasn't the user's phrase. The name is
    the model's to choose; the words it quotes in because are what the add stands on."""

    def add(self, title, words, start="9:10 PM", **more):
        from thirties_core.plan_log import model_call
        with model_call():
            return self.said("add_event", {"title": title, "start": start, "duration": "30 min", **more}, words)

    def test_the_models_name_for_it_goes_on(self):
        self.assertWent(self.add("Hardware store run", "I need sandpaper and a tin of spar varnish before 9:10",
                                 because="need sandpaper"))
        self.assertEqual(len(self.planned("Hardware store run")), 1)

    def test_tone_words_alone_add_nothing(self):
        for words in ("thanks!", "lol", "hmm"):
            for because in ({}, {"because": "need sandpaper"}):
                with self.subTest(words=words, because=because):
                    self.assertHeld(self.add("Hardware store run", words, **because))
                    self.assertEqual(self.planned("Hardware store run"), [])

    def test_a_second_copy_of_whats_planned_still_isnt_added(self):
        for again in (False, True):
            with self.subTest(again=again):
                out = self.add("Varnish the paddle", "I need sandpaper first", start="8:40 PM", again=again)
                self.assertHeld(out)
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)
        self.assertWent(self.add("Varnish the paddle", "a second paddle varnish coat at 8:40", start="8:40 PM",
                                 again=True))
        self.assertEqual(len(self.planned("Varnish the paddle")), 2)



class TestTheModelMayAsk(WordsCase):
    """A reply asking the user to choose, or asking leave for a change, was sent back for a rewrite."""

    def test_a_choice_question_reaches_the_user(self):
        from tests.test_conversation import reply
        self.engine.set_next_response(reply("Would you rather unload the bisque before the paddle varnish or after it?"))
        out = self.manager.send_user_message("I need to unload the bisque this afternoon")
        self.assertEqual(out, "Would you rather unload the bisque before the paddle varnish or after it?")
        self.assertEqual(len(self.engine.call_history), 1)

    def test_asking_leave_reaches_the_user(self):
        from tests.test_conversation import reply
        self.engine.set_next_response(reply("Your wrist sounds sore. Would you like me to take the thwart sanding "
                                            "off tonight?"))
        out = self.manager.send_user_message("my wrist is acting up again")
        self.assertIn("Would you like me to take the thwart sanding off tonight?", out)
        self.assertEqual(len(self.engine.call_history), 1)

    def test_talk_of_the_workings_is_still_sent_back(self):
        from tests.test_conversation import reply
        for _ in range(3):
            self.engine.set_next_response(reply("My internal planning session put the paddle at 6:10. Want it "
                                                "later?"))
        out = self.manager.send_user_message("is the paddle still on?")
        self.assertNotIn("internal planning session", out)


if __name__ == "__main__":
    unittest.main()
