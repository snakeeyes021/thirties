"""A done said beside other asks, for an item that shares its words with one already done.

- A done the model read stands when the words say it happened, share a word (or a word for it) with the item, and
  no open item fits them better: an item already done (a routine step ticked earlier), an item named only in
  another ask beside the done ("the bench sanding can wait") and a later item whose name is a word of this one's
  are no rivals.
- Words that fit an open item as well or better, or name nothing of this one, go on as the model read them, with a
  note naming what they fit; words with no done in them are refused.
- An item that gives others their meal ("Feed the volunteers breakfast") is no meal of the user's own: ticking it
  never moves the user's own meal.
- A reply that moved one item and skipped two never has a line added saying a skipped one couldn't move.
- An item that needs daylight, kept in place when work runs late, is said to need daylight, never to run into
  bedtime.

Every done goes through the real log_outcome path, alone and in whole turns with a mock model.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from tests.test_routines import RoutineCase, TITLES
from thirties_core.astronomy import set_debug_time
from thirties_core.claim_parts import claimed_parts
from thirties_core.kinds import is_meal
from thirties_core.meals import meal_word
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

STEP = "Wax the skis"
SKIS = "Wax the skis for the race"
ROUND = ["Wax the skis", "Oil the gate hinge", "Sweep the porch"]
ITEMS = ((SKIS, "10:40 AM"), ("Sand the bench", "11:10 AM"), ("Glaze the mugs", "1:30 PM"), ("Race", "3:50 PM"))
NOW = "12:30"
NOTHING_DONE = "Nothing the user said this turn is a done"


class GarageCase(RoutineCase):
    """An early garage round, its ski step ticked; waxing the skis for the race still open, the bench and the mugs
    open, and the race itself later on."""

    def setUp(self):
        super().setUp()
        TITLES["n_garage"] = "Garage round"
        self.addCleanup(TITLES.pop, "n_garage", None)
        self.api.notes["n_garage"] = {"id": "n_garage", "body": "".join(f"- [ ] {s}\n" for s in ROUND)}
        self.catalog.set_role("n_garage", "routine", "Garage round")
        self.api.notes["n_routine"]["body"] = ""
        self.api.notes["n_clean"]["body"] = ""
        self.t = self.tools_for(self.t.day_plan.target_date)
        out = self.t.execute("put_routine", {"routine": "Garage round",
                                             "parts": [{"start": "9:10 AM", "items": ROUND}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        for title, start in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                               "alongside": True})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at(NOW))
        self.t.by_hand = True
        self.assertFalse(self.t.execute("log_outcome", {"event": self.span(STEP).id, "status": "done"})
                         .startswith(NOT_DONE))
        self.t.by_hand = False

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def outcome(self, message, item, status="done"):
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", ""), None
        try:
            return self.t.execute("log_outcome", {"event": self.span(item).id, "status": status})
        finally:
            self.t.user_message, self.t.prior_exchange, self.t.done_asks = None, None, None

    def assertMarked(self, message, item=SKIS):
        out = self.outcome(message, item)
        self.assertFalse(out.startswith(NOT_DONE), (message, out))
        self.assertEqual(self.span(item).status, SpanStatus.DONE, message)
        return out

    def assertRefused(self, message, item=SKIS):
        out = self.outcome(message, item)
        self.assertTrue(out.startswith(NOT_DONE), (message, out))
        self.assertEqual(self.span(item).status, SpanStatus.PLANNED, message)
        return out


class TestTheDoneStands(GarageCase):

    def test_the_step_is_part_of_the_routine_and_done(self):
        self.assertEqual(self.span(STEP).status, SpanStatus.DONE)
        self.assertEqual(self.span("Oil the gate hinge").status, SpanStatus.PLANNED)

    def test_beside_a_skip(self):
        for message in ("The skis got waxed, the bench sanding can wait",
                        "Skis were waxed a while ago; bench can wait", "the skis are waxed"):
            with self.subTest(message=message):
                self.assertMarked(message)
                self.span(SKIS).status = SpanStatus.PLANNED

    def test_the_skip_beside_it_goes_through_too(self):
        message = "Skis were waxed a while ago; bench can wait"
        self.assertMarked(message)
        out = self.outcome(message, "Sand the bench", "skipped")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span("Sand the bench").status, SpanStatus.SKIPPED)


class TestStillRefused(GarageCase):

    def test_only_the_other_ask(self):
        self.assertIn(NOTHING_DONE, self.assertRefused("The bench sanding can wait"))


class TestAnotherItemsDoneIsANote(GarageCase):
    """Wording that fits another entry better leaves the tick standing, with a note naming that entry."""

    def test_another_open_item_said_done(self):
        self.assertIn(f"fits Sand the bench and says nothing of {SKIS}", self.assertMarked("the bench got sanded"))
        self.span(SKIS).status = SpanStatus.PLANNED
        out = self.assertMarked("The mugs got glazed, the bench sanding can wait")
        self.assertIn(f"Glaze the mugs and says nothing of {SKIS}", out)

    def test_the_later_item_said_done(self):
        self.assertIn(f"fits Race as well as {SKIS}", self.assertMarked("finished the race"))

    def test_the_other_open_item_is_still_its_own(self):
        out = self.assertMarked("Skis were waxed a while ago; bench can wait", "Glaze the mugs")
        self.assertIn("says nothing of Glaze the mugs", out)


class TestAPointerSaidAgain(GarageCase):
    """After a reply listing several open items, a pointer alone is the model's pick of them, at once and said
    again; an open item the reply didn't list goes on with a note naming the items the reply listed."""
    SHOWN = ("", f"Still open: {SKIS}, Glaze the mugs and Sand the bench.")

    def pointed(self, message, item=SKIS, asks=None):
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, self.SHOWN, asks
        try:
            return self.t.execute("log_outcome", {"event": self.span(item).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange, self.t.done_asks = None, None, None

    def test_at_once_it_stands(self):
        out = self.pointed("that one's finished")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(SKIS).status, SpanStatus.DONE)

    def test_said_again_it_stands(self):
        out = self.pointed("that one's finished!", asks={SKIS: "that one's finished"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(SKIS).status, SpanStatus.DONE)

    def test_said_again_for_another_listed_item_it_stands(self):
        out = self.pointed("that one's finished!", "Glaze the mugs", asks={SKIS: "that one's finished"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(SKIS).status, SpanStatus.PLANNED)

    def test_an_item_the_reply_did_not_list_goes_on_with_a_note(self):
        out = self.pointed("that one's finished", "Oil the gate hinge")
        self.assertIn("points back to a reply that named", out)
        self.assertEqual(self.span("Oil the gate hinge").status, SpanStatus.DONE)


class TestAMealForOthers(unittest.TestCase):

    def test_what_is_a_meal(self):
        self.assertEqual(meal_word("Brunch"), "brunch")
        self.assertEqual(meal_word("Early breakfast"), "breakfast")
        self.assertTrue(is_meal("Breakfast"))

    def test_whoever_is_fed(self):
        for title in ("Feed the volunteers breakfast", "Feeding the night shift at brunch"):
            self.assertIsNone(meal_word(title), title)
            self.assertFalse(is_meal(title), title)

    def test_a_meal_the_user_has_is_theirs(self):
        for title in ("Have an early breakfast", "Eat brunch on the deck", "Get a quick breakfast"):
            self.assertIsNotNone(meal_word(title), title)
            self.assertTrue(is_meal(title), title)


class TestTickingOthersMeal(ConversationTestCase):

    def test_the_users_own_meal_stays_put(self):
        tools = self.manager.tools
        for title, start in (("Feed the volunteers breakfast", "7:50 AM"), ("Brunch", "8:40 AM")):
            tools.execute("add_event", {"title": title, "start": start, "duration": "30 minutes", "alongside": True})
        spans = {s.title: s for s in self.manager.day_plan.spans}
        brunch = (spans["Brunch"].start_dt, spans["Brunch"].end_dt)
        set_debug_time(at("08:35"))
        tools.by_hand = True
        out = tools.execute("log_outcome", {"event": spans["Feed the volunteers breakfast"].id, "status": "done"})
        tools.by_hand = False
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual((spans["Brunch"].start_dt, spans["Brunch"].end_dt), brunch)
        self.assertNotIn("Brunch", out)


class TestClaimParts(unittest.TestCase):

    def test_a_bare_name_belongs_to_the_statement_before_it(self):
        import re
        moved = re.compile(r"\bmoved\b")
        titles = ["Glaze the mugs", "Oil the canoe paddles", "Sand the bench"]

        def named(part):
            return [t for t in titles if t.lower() in part.lower()]
        sentence = "Sand the bench is moved to 3:10, and I've skipped Glaze the mugs and Oil the canoe paddles"
        self.assertEqual([p.strip() for p in claimed_parts(sentence, moved, named)],
                         ["Sand the bench is moved to 3:10"])
        opening = "Glaze the mugs and Oil the canoe paddles are moved"
        self.assertEqual(len(claimed_parts(opening, moved, named)), 2)


class TestWholeTurns(ConversationTestCase):

    def setUp(self):
        super().setUp()
        from thirties_core import morning
        set_debug_time(at(NOW))
        morning.mark_done(self.manager.scheduler.state_db, self.manager.day_plan.target_date, at("07:40"))
        for title, start in ((STEP, "9:10 AM"),) + ITEMS + (("Oil the canoe paddles", "2:50 PM"),):
            self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 minutes",
                                                     "alongside": True})
        self.manager.tools.by_hand = True
        self.manager.tools.execute("log_outcome", {"event": self.span(STEP).id, "status": "done"})
        self.manager.tools.by_hand = False

    def span(self, title):
        return next(s for s in self.manager.day_plan.spans if s.title == title)

    def tool_results(self):
        return [m["content"] for m in self.manager.brain.messages if m.get("role") == "tool"]

    def test_the_done_and_the_skip_in_one_turn(self):
        for said in ("The skis got waxed, the bench sanding can wait", "Skis were waxed a while ago; bench can wait"):
            with self.subTest(said=said):
                calls = (f'CALL log_outcome {{"event": "{self.span(SKIS).id}", "status": "done"}}\n'
                         f'CALL log_outcome {{"event": "{self.span("Sand the bench").id}", "status": "skipped"}}')
                self.engine.set_next_response(reply(calls))
                self.engine.set_next_response(reply("The skis are ticked and the bench is skipped."))
                self.manager.send_user_message(said)
                self.assertFalse(any(r.startswith(NOT_DONE) for r in self.tool_results()[-2:]), self.tool_results())
                self.assertEqual(self.span(SKIS).status, SpanStatus.DONE)
                self.assertEqual(self.span("Sand the bench").status, SpanStatus.SKIPPED)
                self.span(SKIS).status = self.span("Sand the bench").status = SpanStatus.PLANNED

    def test_skipped_items_are_never_said_to_have_failed_to_move(self):
        said = "Mugs and paddles are off today; bench sanding at 4:40 instead."
        calls = "\n".join([
            f'CALL log_outcome {{"event": "{self.span("Glaze the mugs").id}", "status": "skipped"}}',
            f'CALL log_outcome {{"event": "{self.span("Oil the canoe paddles").id}", "status": "skipped"}}',
            f'CALL update_event {{"event": "{self.span("Sand the bench").id}", "start": "4:40 PM"}}'])
        self.engine.set_next_response(reply(calls))
        answer = "Sand the bench is moved to 4:40 PM, and I've skipped Glaze the mugs and Oil the canoe paddles."
        self.engine.set_next_response(reply(answer))
        self.engine.set_next_response(reply(answer))
        shown = self.manager.send_user_message(said)
        self.assertFalse(any(r.startswith(NOT_DONE) for r in self.tool_results()), self.tool_results())
        self.assertEqual(self.span("Oil the canoe paddles").status, SpanStatus.SKIPPED)
        self.assertEqual(self.span("Sand the bench").start_dt, at("16:40"))
        text = str(shown if isinstance(shown, str) else self.manager.brain.messages[-1]["content"])
        self.assertNotIn("couldn't move", text)
        self.assertNotIn("still at", text)
        self.assertIn("skipped Glaze the mugs and Oil the canoe paddles", text)


class TestPastDaylightIsNotBedtime(ConversationTestCase):

    def test_an_item_that_needs_daylight(self):
        set_debug_time(at("18:20"))
        tools = self.manager.tools
        for title, start in (("Glaze the mugs", "4:10 PM"), ("Rake the side path", "4:40 PM")):
            tools.execute("add_event", {"title": title, "start": start, "duration": "30 minutes"})
        path = next(s for s in self.manager.day_plan.spans if s.title == "Rake the side path")
        facts = tools._facts().get(path.id)
        facts.daylight = True
        tools._facts().set(path.id, facts)
        tools.user_message = "wrapped up at 6:10"
        out = tools.execute("log_day_times", {"work_ended": "6:10 PM"})
        self.assertNotIn("bedtime", out)
        self.assertIn("Needs daylight", out)
        self.assertIn("Rake the side path", out)


if __name__ == "__main__":
    unittest.main()
