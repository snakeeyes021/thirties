"""Which item the user means is the model's call; a done is refused only when nothing said is a done, or the words
name the item as not done (said_for_it.py).

- The model's reading: "the chooks got watered" marks Water the hens beside Water the sourdough starter and
  Water the goats, which share only the verb; a nickname does too.
- Answering the app: after a reply naming only this item, a yes, a request for the tick or an insisting word marks
  it.
- Said again: a done said again after the app asked which marks it.
- Talk to the app beside a done ("quit checking on that one") never makes it read as under way.
- What else the words fit never stops a done: words that fit two items still to do as well, words that name
  another item or fit it better, a pointer back to a reply that named two items, a reply about another item.
- Still refused: no done said, and a no.

Every case goes through the real log_outcome path, alone and as whole turns with a mock model.
"""

import unittest

from tests.fixtures import at, build_day
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.done_reference import prior_exchange
from thirties_core.models import SpanStatus
from thirties_core.plain_done import by_the_models_reading, done_asks, to_the_app
from thirties_core.plan_tools import NOT_DONE, PlanTools

HENS = "Water the hens"
# none of the watering has come round yet, so the time of day picks none of them
ITEMS = (("Brush the goats", "9:50 AM"), ("Glaze the mugs", "10:50 AM"), ("Water the sourdough starter", "1:10 PM"),
         ("Oil the canoe paddles", "2:20 PM"), (HENS, "4:40 PM"))
NOW = "11:40"
# the same verb on two animals: words saying nothing beyond it fit both
BOTH_ANIMALS = ITEMS + (("Water the goats", "3:40 PM"),)
NOTHING_DONE = "Nothing the user said this turn is a done"


class PlainCase(unittest.TestCase):
    items = ITEMS

    def setUp(self):
        set_debug_time(at(NOW))
        self.addCleanup(set_debug_time, None)
        sched, plan, pending = build_day()
        self.t = PlanTools(plan, pending, sched)
        for title, start in self.items:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                               "alongside": True})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, item, prior=("", ""), asks=None):
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, prior, asks
        try:
            return self.t.execute("log_outcome", {"event": self.span(item).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange, self.t.done_asks = None, None, None

    def assertMarked(self, message, item, *args):
        out = self.done(message, item, *args)
        self.assertFalse(out.startswith(NOT_DONE), (message, out))
        self.assertEqual(self.span(item).status, SpanStatus.DONE, message)
        return out

    def assertRefused(self, message, item, *args):
        out = self.done(message, item, *args)
        self.assertTrue(out.startswith(NOT_DONE), (message, out))
        self.assertEqual(self.span(item).status, SpanStatus.PLANNED, message)
        return out


class TestTheModelsReading(PlainCase):

    def test_everyday_name_for_the_animal(self):
        self.assertMarked("the chooks got watered", HENS)

    def test_a_nickname(self):
        self.assertMarked("the girls are watered, finally", HENS)

    def test_fits_another_item_better(self):
        self.assertTrue(by_the_models_reading(self.t, "chickens watered", HENS))
        self.assertFalse(by_the_models_reading(self.t, "chickens watered", "Brush the goats"))
        self.assertMarked("the chooks got watered", "Brush the goats")

    def test_another_item_named_goes_on_with_the_note_it_had(self):
        self.assertIn(f"may also mean {HENS}", self.assertMarked("the hens got watered", "Water the sourdough starter"))
        self.assertMarked("the mugs are glazed", HENS)

    def test_no_done_words_no_done(self):
        self.assertRefused("the chooks are getting watered", HENS)
        self.assertRefused("the chooks still need watering", HENS)

    def test_nothing_to_go_on(self):
        self.assertIn(NOTHING_DONE, self.assertRefused("hm, ok", HENS))


class TestTwoItemsFit(PlainCase):
    items = BOTH_ANIMALS

    def test_words_that_say_nothing_beyond_the_tie_are_the_models_reading(self):
        self.assertMarked("the watering's all done", HENS)
        self.assertMarked("all watered now", "Water the goats")

    def test_a_word_beyond_the_tie_is_the_models_to_read(self):
        self.assertMarked("the chooks got watered", HENS)
        self.assertEqual(self.span("Water the goats").status, SpanStatus.PLANNED)

    def test_the_other_animal_named_goes_on_with_a_note(self):
        self.assertIn("may also mean Water the goats", self.assertMarked("the goats got watered", HENS))


class TestAnsweringTheApp(PlainCase):
    items = BOTH_ANIMALS

    def asked(self, item=HENS):
        return ("the watering's all done", f"Just to be sure, did you mean {item}?")

    def test_pointing_back_after_the_question(self):
        for message in ("as I wrote, they're watered", "go ahead and tick that", "yes, that one"):
            with self.subTest(message=message):
                self.assertMarked(message, HENS, self.asked())
                self.span(HENS).status = SpanStatus.PLANNED

    def test_insisting_on_the_obvious(self):
        self.assertMarked("obviously. quit checking on that one", HENS, self.asked())

    def test_a_statement_naming_only_it(self):
        shown = ("", "Water the hens is still open; the rest of the afternoon is free.")
        self.assertMarked("tick it", HENS, shown)

    def test_a_reply_with_its_name_loosely(self):
        shown = ("", "The hens' water is the one still waiting for a tick.")
        self.assertMarked("as I wrote. tick it", HENS, shown)

    def test_another_item_than_the_one_asked_about(self):
        self.assertMarked("go ahead and tick that", "Water the goats", self.asked())

    def test_a_reply_naming_two_open_items(self):
        shown = ("", "Water the hens and Water the goats are both still open.")
        self.assertMarked("go ahead and tick that", HENS, shown)

    def test_a_no(self):
        self.assertRefused("no, not that one", HENS, self.asked())


class TestSaidAgain(PlainCase):
    items = BOTH_ANIMALS

    def test_the_done_said_again(self):
        asks = {HENS: "the watering's all done"}
        self.assertMarked("the watering's all DONE", HENS, ("", "Which one did you finish?"), asks)

    def test_said_again_for_another_item(self):
        asks = {HENS: "the watering's all done"}
        self.assertMarked("the watering's all done", "Water the goats", ("", "Which one did you finish?"), asks)

    def test_nothing_asked_before(self):
        self.assertMarked("the watering's all DONE", HENS, ("", "Which one did you finish?"))

    def test_a_reply_that_doesnt_say_which(self):
        self.assertIn(NOTHING_DONE, self.assertRefused("ok", HENS, ("", "Which one did you finish?")))

    def test_the_record_of_dones_asked_for(self):
        refusal = f"NOT_DONE: The user's words don't point to {HENS}, so it isn't marked done. Ask."
        messages = [{"role": "user", "content": "the critters got watered"},
                    {"role": "tool", "name": "log_outcome", "content": refusal},
                    {"role": "assistant", "content": "Which one did you finish?"}]
        self.assertEqual(done_asks(messages), {HENS: "the critters got watered"})
        self.assertEqual(done_asks(messages + [{"role": "user", "content": "no, never mind"}]), {})
        marked = messages + [{"role": "user", "content": "the hens"},
                             {"role": "tool", "name": "log_outcome", "content": f"Marked {HENS} done."}]
        self.assertEqual(done_asks(marked), {})


class TestTalkToTheApp(PlainCase):

    def test_a_complaint_beside_a_done(self):
        asked = ("", f"Did you mean {HENS}?")
        self.assertMarked("yes!! you can skip the check", HENS, asked)

    def test_a_complaint_with_the_done_named(self):
        self.assertMarked("the chooks got watered, why do you keep asking", HENS)

    def test_what_is_talk_to_the_app(self):
        self.assertTrue(to_the_app("quit checking on that one"))
        self.assertTrue(to_the_app("you don't need to check"))
        self.assertFalse(to_the_app("it isn't done, you know"))
        self.assertFalse(to_the_app("the hose is running"))

    def test_under_way_is_still_no_done(self):
        asked = ("", f"Did you mean {HENS}?")
        self.assertRefused("yes, it's still going", HENS, asked)
        self.assertRefused("yes, but you know it's still going", HENS, asked)


class TestWholeTurns(ConversationTestCase):
    """The user's words, the model's call and its reply, turn after turn: the item the model read ends up done."""

    def setUp(self):
        super().setUp()
        from thirties_core import morning
        set_debug_time(at(NOW))
        morning.mark_done(self.manager.scheduler.state_db, self.manager.day_plan.target_date, at("07:40"))
        for title, start in BOTH_ANIMALS:
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
        tools = [m for m in self.manager.brain.messages if m.get("role") == "tool"]
        return tools[-1]["content"] if call else ""

    def test_everyday_name_in_one_turn(self):
        self.assertFalse(self.turn("chooks got watered", HENS, "Marked.").startswith(NOT_DONE))
        self.assertEqual(self.status(HENS), SpanStatus.DONE)

    def answered(self, answer):
        self.turn("the watering's all done", HENS, f"Did you mean {HENS}?", call=False)
        out = self.turn(answer, HENS, "Marked.")
        self.assertFalse(out.startswith(NOT_DONE), (answer, out))
        self.assertEqual(self.status(HENS), SpanStatus.DONE)
        self.assertEqual(self.status("Water the goats"), SpanStatus.PLANNED)

    def test_pointing_back_with_the_done_again(self):
        self.answered("as I wrote, they're watered")

    def test_pointing_back_with_mark_it(self):
        self.answered("go ahead and tick that")

    def test_pointing_back_with_a_yes(self):
        self.answered("yes, that one")

    def test_the_done_said_again_after_which_one(self):
        self.turn("the watering's all done", HENS, "Which one did you finish?", call=False)
        self.assertNotEqual(self.status(HENS), SpanStatus.DONE)
        self.turn("the watering's all DONE!", HENS, "Marked.")
        self.assertEqual(self.status(HENS), SpanStatus.DONE)

    def test_a_complaint_beside_the_answer(self):
        self.turn("the watering's all done", HENS, f"Did you mean {HENS}?", call=False)
        out = self.turn("yes obviously, you can skip the check", HENS, "Marked.")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.status(HENS), SpanStatus.DONE)

    def test_two_items_fit_and_nothing_was_asked(self):
        out = self.turn("the watering's all done", "Water the goats", "Marked the goats; was it the hens too?")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.status("Water the goats"), SpanStatus.DONE)
        self.assertEqual(self.status(HENS), SpanStatus.PLANNED)
        out = self.turn("ok", HENS, "Which one?")
        self.assertFalse(out.startswith(NOT_DONE), out)  # a yes to whether the hens were done too
        self.assertEqual(self.status(HENS), SpanStatus.DONE)


if __name__ == "__main__":
    unittest.main()
