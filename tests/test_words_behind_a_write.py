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


class TestAKeepIsNoRemoval(WordsCase):
    """A keep or a restore naming the item, or a busy word about something else, let the item come off."""

    def test_a_restore_or_keep_never_takes_it_off(self):
        for words in ("put the paddle varnish back where I had it", "keep the paddle varnish",
                      "drop the thwart sanding but keep the paddle varnish",
                      "tonight is way too packed, keep the paddle varnish though"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Varnish the paddle", words))
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)

    def test_the_other_item_still_comes_off(self):
        self.assertWent(self.remove("Sand the canoe thwart", "drop the thwart sanding but keep the paddle varnish"))

    def test_load_words_mean_a_full_day(self):
        from thirties_core.change_asks import LOAD_SAID_RE
        for said in ("too much today", "my day is packed", "lighten the day", "this evening is way too full",
                     "I can't fit it all in", "I'm swamped", "tonight is overloaded"):
            self.assertTrue(LOAD_SAID_RE.search(said), said)
        for said in ("I packed the van", "need to clear out the kiln shelves at 5", "the van is packed",
                     "lighten the glaze a shade", "too much salt in the soup", "the shop was swamped with sawdust"):
            self.assertFalse(LOAD_SAID_RE.search(said), said)

    def test_busy_words_about_something_else_take_nothing_off(self):
        for words in ("I packed the van for the lake", "the van is packed to the roof"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Varnish the paddle", words))
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)

    def test_load_words_cover_only_the_stretch_they_name(self):
        self.assertHeld(self.remove("Varnish the paddle", "this morning is way too packed"))
        self.assertEqual(len(self.planned("Varnish the paddle")), 1)
        self.assertWent(self.remove("Varnish the paddle", "this evening is way too packed"))


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


class TestOffTodaysListOnTheSameGrounds(LeaveBase):
    """Removing an item on words about a full day left its Today line, or refused an untimed one."""

    def untimed_line(self, title):
        self.api.notes["n_today"]["body"] += f"- [ ] {title}\n"

    def test_a_full_day_takes_an_untimed_line_off(self):
        self.untimed_line("Oil the hinges")
        out = self.say("today is way too packed", "remove_event", {"event": "Oil the hinges"})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertNotIn("Oil the hinges", self.body("n_today"))
        self.assert_guarded()

    def test_a_full_day_takes_a_timed_items_line_off_too(self):
        set_debug_time(at("09:45"))
        self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        span = self.planned("Fix the gate")[0]
        out = self.say("today is way too packed", "remove_event", {"event": span.id})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertEqual(self.planned("Fix the gate"), [])
        self.assertNotIn("Fix the gate", self.body("n_today"))
        self.assert_guarded()

    def test_busy_words_about_something_else_or_a_keep_take_no_line_off(self):
        self.untimed_line("Oil the hinges")
        for words in ("I packed the van", "keep the hinges on today"):
            with self.subTest(words=words):
                out = self.say(words, "remove_event", {"event": "Oil the hinges"})
                self.assertTrue(out.startswith("NOT_DONE"), out)
                self.assertIn("Oil the hinges", self.body("n_today"))


class ShopEvening(WordsCase):
    """An evening of shop jobs and errands, for words that only look like a done."""

    ITEMS = (("Fix the bandsaw fence", "4:10 PM"), ("Mail the studio grant form", "4:40 PM"),
             ("Bake the rye loaf", "5:10 PM"), ("Buy oat milk", "5:40 PM"), ("Glass supplier visit", "6:10 PM"),
             ("Renew the kiln permit", "6:40 PM"), ("Restock the clay bins", "7:10 PM"),
             ("Check kiln wiring", "7:40 PM"))

    def others(self, title):
        return [t for t, _ in self.ITEMS if t != title]

    def assertNoTick(self, cases):
        from thirties_core.write_guard import refused
        for title, words in cases:
            with self.subTest(title=title, words=words):
                self.assertNotEqual(refused({"op": "complete", "title": title}, f"[app] {words}", None), "")
                self.assertHeld(self.tick(title, words))

    def assertTicks(self, cases):
        from thirties_core.write_guard import refused
        for title, words in cases:
            with self.subTest(title=title, words=words):
                self.assertEqual(refused({"op": "complete", "title": title}, f"[app] {words}", None), "")
                self.assertWent(self.tick(title, words))
                self.reopen(title)

    def reopen(self, title):
        from thirties_core.models import SpanStatus
        for s in self.planned(title):
            s.status = SpanStatus.PLANNED


class TestGettingItDoneIsNoDone(ShopEvening):
    """A start with a done word in it ("getting the fence fixed") read as a finish and ticked the item."""

    def test_under_way_or_being_done_ticks_nothing(self):
        self.assertNoTick((("Fix the bandsaw fence", "I'm at the hardware shop getting the bandsaw fence fixed"),
                           ("Fix the bandsaw fence", "getting the bandsaw fence fixed at the shop"),
                           ("Fix the bandsaw fence", "I'm getting the bandsaw fence done"),
                           ("Renew the kiln permit", "on my way to get the kiln permit renewed")))

    def test_done_said_of_it_still_ticks(self):
        self.assertTicks((("Fix the bandsaw fence", "got the bandsaw fence fixed"),
                          ("Fix the bandsaw fence", "getting the bandsaw fence sorted took a while, but it's fixed"),
                          ("Renew the kiln permit", "the kiln permit is renewed")))


class TestWhatDidntHappenIsNoReport(ShopEvening):
    """Wishes, guesses and things gone wrong read as reports that the item happened."""

    def test_should_would_wish_and_must_tick_nothing(self):
        self.assertNoTick((("Mail the studio grant form", "ugh, I should've mailed it at 4:40"),
                           ("Mail the studio grant form", "would've mailed the grant form if the post was open"),
                           ("Mail the studio grant form", "could've sent the grant form this morning"),
                           ("Mail the studio grant form", "wish I had sent the grant form"),
                           ("Mail the studio grant form", "I must've mailed it")))

    def test_the_report_words_say_so(self):
        from thirties_core.write_guard import REPORTED_RE
        for words in ("I should've mailed it", "would've written it", "could've sent it", "must've posted it",
                      "wish I had sent it", "if I had sent it", "my keys have gone missing", "the milk's gone off",
                      "it's taken forever", "it's been rescheduled", "it has been cancelled"):
            self.assertIsNone(REPORTED_RE.search(words), words)
        for words in ("I've mailed it", "it's gone", "I had sent it", "they've been varnished"):
            self.assertIsNotNone(REPORTED_RE.search(words), words)

    def test_gone_off_or_called_off_ticks_nothing(self):
        self.assertNoTick((("Buy oat milk", "the oat milk's gone off"),
                           ("Glass supplier visit", "it's been rescheduled to Thursday"),
                           ("Glass supplier visit", "the glass supplier visit got postponed"),
                           ("Glass supplier visit", "the glass supplier visit has been cancelled"),
                           ("Glass supplier visit", "they rescheduled the glass supplier visit")))

    def test_the_items_own_verb_still_ticks(self):
        from thirties_core.write_guard import says_done
        self.assertTrue(says_done("cancelled the sander rental", "Cancel the sander rental"))
        self.assertTrue(says_done("rescheduled the glass supplier visit", "Reschedule the glass supplier visit"))


class TestAPlanOrAWishIsNoDone(ShopEvening):
    """Everyday done words said of a plan, a wish or something still to come ticked the item."""

    def test_wanting_it_off_the_plate_or_being_dealt_with_ticks_nothing(self):
        self.assertNoTick((("Renew the kiln permit", "I want to get the kiln permit off my plate"),
                           ("Renew the kiln permit", "I want to get it off my plate"),
                           ("Renew the kiln permit", "it's being dealt with"),
                           ("Renew the kiln permit", "the kiln permit will be dealt with"),
                           ("Renew the kiln permit", "knocked out by a headache all afternoon")))

    def test_the_everyday_dones_still_tick(self):
        self.assertTicks((("Renew the kiln permit", "the kiln permit is off my plate"),
                          ("Renew the kiln permit", "got the kiln permit off my plate"),
                          ("Renew the kiln permit", "the kiln permit has been dealt with"),
                          ("Renew the kiln permit", "knocked out the kiln permit")))


class TestInTheOvenIsNoDone(ShopEvening):
    """ "The loaf is in" (just gone in the oven) ticked the bake."""

    def test_in_the_oven_ticks_nothing(self):
        self.assertNoTick((("Bake the rye loaf", "the rye loaf is in"),
                           ("Bake the rye loaf", "the rye loaf is in, ready at 5:50"),
                           ("Bake the rye loaf", "it's in")))

    def test_handed_in_still_ticks(self):
        self.assertTicks((("Mail the studio grant form", "the grant form is in"),
                          ("Renew the kiln permit", "the kiln permit is in")))
        from thirties_core.write_guard import refused  # a bare "it" points at none of the planned items
        self.assertEqual(refused({"op": "complete", "title": "Mail the studio grant form"}, "[app] it's in", None), "")


class TestOnlyWhatIsTakenBackHolds(ShopEvening):
    """A thought about something else, or a repeat said with once, held a done that was said."""

    def test_a_done_beside_another_thought_ticks(self):
        self.assertTicks((("Fix the bandsaw fence", "I thought it would take longer, but the bandsaw fence is done"),
                          ("Mail the studio grant form", "Mailed the grant form. I thought it would take longer."),
                          ("Fix the bandsaw fence", "once again I've fixed the bandsaw fence"),
                          ("Fix the bandsaw fence", "Once again I've fixed the bandsaw fence myself")))

    def test_taken_back_still_holds(self):
        self.assertNoTick((("Fix the bandsaw fence", "I thought it was fixed, but the fence slips again"),
                           ("Fix the bandsaw fence", "the bandsaw fence is fixed, or so I thought"),
                           ("Mail the studio grant form", "I thought I'd mailed the grant form")))


if __name__ == "__main__":
    unittest.main()
