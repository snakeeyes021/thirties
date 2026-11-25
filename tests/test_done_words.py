"""A done goes through for an item the user names in everyday words, and for one they already asked for.

- Word forms and common household words: "varnishing done" for Varnish, "calibration done" for Calibrate the
  kiln, "dog agility done" for Take the puppy to agility beside Agility drills.
- Across turns: the app asking whether they meant one item, answered with anything but a no, marks that item; and
  a done asked for earlier and refused stays asked for, so a "yes", "yes, the class" or a complaint ending in "?"
  completes it.
- Words that fit another item go on as the model read them; words that say no done tick nothing, on the plan or
  in Joplin.
"""

import unittest

from tests.fixtures import at, build_day
from tests.test_conversation import ConversationTestCase, reply
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.done_reference import prior_exchange
from thirties_core.done_words import kin, named, pending_dones
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools

ITEMS = (("Take the puppy to agility", "7:00 AM"), ("Agility drills", "7:30 AM"), ("Calibrate the kiln", "8:00 AM"),
         ("Hose the kayak", "8:30 AM"), ("Varnish", "9:00 AM"), ("Rake the gravel", "9:30 AM"),
         ("Surgeon visit", "11:00 AM"))
TITLES = [t for t, _ in ITEMS]
PUP = "Take the puppy to agility"


class TestEverydayWords(unittest.TestCase):

    def names(self, text, title):
        return named(text, title, TITLES)

    def test_word_forms(self):
        self.assertTrue(self.names("varnishing done", "Varnish"))
        self.assertTrue(self.names("surgical follow-up is over", "Surgeon visit"))  # same first four letters
        self.assertTrue(kin("laminate", ("lamination", "lamination")))

    def test_common_household_words(self):
        self.assertTrue(self.names("calibration done", "Calibrate the kiln"))
        self.assertTrue(named("puppy agility is over", "Dog agility class", ["Dog agility class", "Swim drills"]))
        self.assertTrue(self.names("dog agility done", PUP))
        self.assertTrue(named("starter got fed", "Feed the sourdough starter",
                              ["Feed the sourdough starter", "Feed the koi"]))

    def test_words_that_dont_point_to_it(self):
        self.assertFalse(self.names("dog agility done", "Agility drills"))
        self.assertFalse(self.names("calibration done", "Rake the gravel"))
        self.assertFalse(self.names("varnishing done", "Surgeon visit"))
        self.assertFalse(self.names("completed everything earlier", "Rake the gravel"))
        self.assertFalse(named("starter got fed", "Feed the koi", ["Feed the sourdough starter", "Feed the koi"]))
        self.assertFalse(self.names("agility done", PUP))  # shared with Agility drills: not clear alone


class WordsCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:40"))
        self.addCleanup(set_debug_time, None)
        sched, plan, pending = build_day()
        self.t = PlanTools(plan, pending, sched)
        for title, start in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                               "alongside": True})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, item, prior=("", ""), pending=None):
        self.t.user_message, self.t.prior_exchange, self.t.pending_dones = message, prior, pending
        try:
            return self.t.execute("log_outcome", {"event": self.span(item).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange, self.t.pending_dones = None, None, None


class TestDoneInEverydayWords(WordsCase):

    def test_marked(self):
        for message, item in (("varnishing done", "Varnish"), ("calibration done", "Calibrate the kiln"),
                              ("dog agility done", PUP)):
            out = self.done(message, item)
            self.assertFalse(out.startswith(NOT_DONE), (message, out))
            self.assertEqual(self.span(item).status, SpanStatus.DONE, message)

    def test_words_for_another_item_are_the_models_reading(self):
        for message, item in (("dog agility done", "Agility drills"), ("calibration done", "Rake the gravel"),
                              ("varnishing done", "Surgeon visit")):
            out = self.done(message, item)
            self.assertFalse(out.startswith(NOT_DONE), (message, out))
            self.assertEqual(self.span(item).status, SpanStatus.DONE, message)

    def test_no_done_word_is_still_refused(self):
        out = self.done("kayak is clean", "Calibrate the kiln")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Calibrate the kiln").status, SpanStatus.PLANNED)


class TestAQuestionAboutOneItem(WordsCase):

    def test_anything_but_a_no_confirms_it(self):
        asked = ("the furball had a great class", "Did you mean Take the puppy to agility?")
        out = self.done("yep", PUP, asked)
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_no_is_refused(self):
        asked = ("the furball had a great class", "Did you mean Take the puppy to agility?")
        for message, item in (("no", PUP), ("nope, the other thing", PUP), ("no, the kiln", PUP)):
            out = self.done(message, item, asked)
            self.assertTrue(out.startswith(NOT_DONE), (message, item, out))
        self.assertEqual(self.span(PUP).status, SpanStatus.PLANNED)

    def test_a_yes_for_another_item_is_the_models_reading(self):
        out = self.done("yes", "Agility drills", ("", f"Did you mean {PUP}?"))
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_an_offer_is_no_such_question(self):
        offered = ("", "Want me to move the kiln to 10:00?")
        self.assertTrue(self.done("ok", "Calibrate the kiln", offered).startswith(NOT_DONE))
        self.assertEqual(self.span("Calibrate the kiln").status, SpanStatus.PLANNED)

    def test_a_question_about_two_items_needs_more(self):
        asked = ("", "Did you mean Take the puppy to agility or Agility drills?")
        self.assertTrue(self.done("yes", PUP, asked).startswith(NOT_DONE))


class TestADoneAskedForBefore(WordsCase):

    def history(self, said, item, *after):
        """The conversation after the user asked for a done that was refused as not pointing to the item, as an
        earlier conversation holds such a refusal."""
        refused = (f"NOT_DONE: The user's words don't point to {item}, so it isn't marked done. If they meant it, "
                   "ask which item they finished.")
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": said},
                    {"role": "assistant", "content": "", "tool_calls": [{"name": "log_outcome"}]},
                    {"role": "tool", "name": "log_outcome", "content": refused},
                    {"role": "assistant", "content": f"Did you mean {item}?"}]
        for text in after:
            messages.append({"role": "user", "content": text})
            messages.append({"role": "assistant", "content": "Which item did you mean?"})
        return messages

    def test_still_asked_for_until_done(self):
        messages = self.history("the furball had a great class", PUP, "yes")
        self.assertEqual(pending_dones(messages), [PUP])
        out = self.done("yes, the class", PUP, prior_exchange(messages), pending_dones(messages))
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(PUP).status, SpanStatus.DONE)

    def test_a_complaint_ending_in_a_question_mark_completes_it(self):
        messages = self.history("the furball had a great class", PUP, "yes")
        out = self.done("is it still not ticked?", PUP, prior_exchange(messages), pending_dones(messages))
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(PUP).status, SpanStatus.DONE)

    def test_the_question_rule_still_holds_for_other_items(self):
        messages = self.history("the furball had a great class", PUP, "yes")
        out = self.done("is it still not ticked?", "Rake the gravel", prior_exchange(messages),
                        pending_dones(messages))
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Rake the gravel").status, SpanStatus.PLANNED)

    def test_a_no_calls_it_off(self):
        messages = self.history("the furball had a great class", PUP, "no, never mind")
        self.assertEqual(pending_dones(messages), [])
        out = self.done("ok", PUP, prior_exchange(messages), pending_dones(messages))
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_only_a_done_the_user_asked_for(self):
        messages = self.history("the furball had a great class", PUP)
        messages[1]["content"] = "what's after this"  # the model tried a done nobody asked for
        self.assertEqual(pending_dones(messages), [])

    def test_marked_since_is_no_longer_asked_for(self):
        messages = self.history("the furball had a great class", PUP)
        messages.append({"role": "user", "content": "yes"})
        messages.append({"role": "tool", "name": "log_outcome", "content": f"Marked {PUP} done."})
        self.assertEqual(pending_dones(messages), [])


class TestTheWholeExchange(ConversationTestCase):
    """The user's done goes through on the model's reading at once; when the model asks which first, the reply
    finishes it. Words that say no done mark nothing."""

    def setUp(self):
        super().setUp()
        from thirties_core import morning
        morning.mark_done(self.manager.scheduler.state_db, self.manager.day_plan.target_date, at("07:30"))
        for title, start in ITEMS:
            self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                                     "alongside": True})

    def status(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title).status

    def turn(self, said, title, answer, call=True):
        """One turn: the model calls log_outcome for the item (or, with call=False, only asks), then replies."""
        if call:
            item = next(s for s in self.manager.day_plan.spans if s.title == title)
            self.engine.set_next_response(reply(f'CALL log_outcome {{"event": "{item.id}", "status": "done"}}'))
        self.engine.set_next_response(reply(answer))
        self.manager.send_user_message(said)

    def test_the_models_reading_goes_through_at_once(self):
        self.turn("the furball had a great class", PUP, "Marked the puppy's class.")
        self.assertEqual(self.status(PUP), SpanStatus.DONE)
        self.assertNotEqual(self.status("Agility drills"), SpanStatus.DONE)

    def test_asked_first_then_yes(self):
        self.turn("the furball had a great class", PUP, f"Did you mean {PUP}?", call=False)
        self.assertNotEqual(self.status(PUP), SpanStatus.DONE)
        self.turn("yes", PUP, "Marked.")
        self.assertEqual(self.status(PUP), SpanStatus.DONE)

    def test_no_done_said_marks_nothing(self):
        self.turn("the furball's class is at seven", "Rake the gravel", "Noted.")
        self.turn("ok", "Rake the gravel", "Which one?")
        self.turn("is it still not ticked?", "Rake the gravel", "Which one?")
        self.assertNotEqual(self.status("Rake the gravel"), SpanStatus.DONE)

    def test_a_no_calls_it_off(self):
        self.turn("the furball had a great class", PUP, f"Did you mean {PUP}?", call=False)
        self.turn("no", PUP, "Which one, then?")
        self.turn("ok", PUP, "Which one?")
        self.assertNotEqual(self.status(PUP), SpanStatus.DONE)


class TestNoJoplinTickForWordsThatNeverPointed(ListsTestCase):
    """Data guard: words that say no done tick nothing in Joplin, even with an earlier request standing; a done the
    model read for another item goes on as its reading."""

    def setUp(self):
        super().setUp()
        self.tasks = {t.title: t for t in self.services.tasks}
        for title, start in (("Call the bank", "12:30 PM"), ("Buy stamps", "1:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                         "task_id": self.tasks[title].id})

    def done(self, message, title, pending=None):
        span = next(s for s in self.t.day_plan.spans if s.title == title)
        self.t.user_message, self.t.prior_exchange, self.t.pending_dones = message, ("", ""), pending
        try:
            return self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange, self.t.pending_dones = None, None, None

    def test_nothing_written(self):
        writes = len(self.api.updates)
        for message, pending in (("yes", ["Call the bank"]), ("is it still not ticked?", ["Call the bank"])):
            out = self.done(message, "Buy stamps", pending)
            self.assertTrue(out.startswith(NOT_DONE), (message, out))
        self.assertEqual(len(self.api.updates), writes)
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))
        out = self.done("yes", "Call the bank", ["Call the bank"])
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [x] Call the bank", self.body("n_errands"))

    def test_a_done_read_for_another_item_goes_on(self):
        out = self.done("the banking call is finished", "Buy stamps")
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
