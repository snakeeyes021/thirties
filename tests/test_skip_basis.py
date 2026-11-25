"""A skip needs a basis in the user's words, as a done does; which items it's for is the model's reading.

- **Named, pointed back to, or asked for in bulk**: the item named in the message, "those" or "skip it" after a reply
  that listed it, "skip the rest" after a list, or words answering a list without asking anything: the skip stands.
  Whether the words mean a skip, and which of the listed items, is the model's to read.
- **A stretch of the day**: words giving it by two times ("from 2:10 to 3:20 PM I was at the dye works") are a basis
  for an item planned to start inside it; for one outside it, or with one time alone, the skip said is the basis,
  and which items it's for is the model's reading.
- **No basis**: a question that names no item ("what's left this morning?"), or words with no skip in them, skip
  nothing; the model is told to ask.
- **Nobody judged**: the app's own controls and the background (nobody talking) skip as before.

Through the real log_outcome on a built day. Fixtures are invented: a morning of fibre work.
"""

import unittest

from tests.fixtures import at, build_day
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools

SKEINS, FLEECE, SHAWL, SPINDLE = "Dye the skeins", "Wash the fleece", "Pin the shawl", "Wind the spindle"
LISTED = f"Still to do this morning: {SKEINS}, {FLEECE} and {SHAWL}."


class FibreMorning(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:40"))
        self.addCleanup(set_debug_time, None)
        sched, plan, pending = build_day()
        self.t = PlanTools(plan, pending, sched)
        for title, start in ((SKEINS, "10:10 AM"), (FLEECE, "10:50 AM"), (SHAWL, "11:20 AM"), (SPINDLE, "2:40 PM")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 minutes"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def skip(self, message, title, shown=""):
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "skipped"})
        finally:
            self.t.user_message, self.t.prior_exchange = None, None

    def assert_skipped(self, out, title):
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.SKIPPED)

    def assert_held(self, out, title):
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("isn't marked skipped", out)
        self.assertNotIn("ask", out)  # never asks them to say it again
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED)


class TestASkipWithABasis(FibreMorning):

    def test_the_item_named(self):
        self.assert_skipped(self.skip("no time for the shawl today, skip it", SHAWL), SHAWL)

    def test_a_pointer_after_a_list(self):
        for title in (SKEINS, FLEECE, SHAWL):
            self.assert_skipped(self.skip("pass on those, I'm out until noon", title, LISTED), title)

    def test_a_pointer_after_a_reply_naming_one(self):
        self.assert_skipped(self.skip("skip it", FLEECE, f"{FLEECE} is next, at 10:50 AM."), FLEECE)

    def test_the_rest_covers_the_listed_items(self):
        for title in (SKEINS, FLEECE, SHAWL):
            self.assert_skipped(self.skip("none started, skip the rest", title, LISTED), title)

    def test_an_answer_to_a_list_naming_none_of_it(self):
        for title in (SKEINS, FLEECE):  # which of the listed items is the model's to read
            self.assert_skipped(self.skip("too tired, skipping this morning", title, LISTED), title)

    def test_the_item_named_beside_another(self):
        self.assert_skipped(self.skip("skip the shawl, the spindle waits", SHAWL), SHAWL)


SPAN = ("from 2:10 to 3:20 PM I was at the dye works, so skip whatever fell in there",
        "between 2:10 and 3:20 PM I was at the dye works, so skip whatever fell in there")


class TestAStretchOfTheDay(FibreMorning):

    def test_an_item_planned_inside_it_stands(self):
        for said in SPAN:
            with self.subTest(said=said):
                self.setUp()
                self.assert_skipped(self.skip(said, SPINDLE), SPINDLE)

    def test_an_item_planned_outside_it_is_the_models_reading(self):
        for said in SPAN:
            with self.subTest(said=said):
                self.assert_skipped(self.skip(said, SHAWL), SHAWL)
                self.span(SHAWL).status = SpanStatus.PLANNED

    def test_one_time_alone_is_the_models_reading(self):
        self.assert_skipped(self.skip("from 2:10 PM I was at the dye works, so skip whatever fell after", SPINDLE),
                            SPINDLE)


class TestASkipWithNoBasis(FibreMorning):

    def test_a_question_naming_no_item(self):
        for title in (SKEINS, SPINDLE):
            self.assert_held(self.skip("What's left this morning?", title), title)

    def test_a_question_naming_no_item_after_a_list(self):
        self.assert_held(self.skip("What's left this afternoon?", SPINDLE, LISTED), SPINDLE)
        self.assert_held(self.skip("What's left this morning?", SKEINS, LISTED), SKEINS)

    def test_words_with_no_skip_in_them(self):
        self.assert_held(self.skip("the dye works were busy this morning", SPINDLE, LISTED), SPINDLE)


class TestASkipForAnItemTheListDidntShow(FibreMorning):

    def test_the_skip_said_is_the_basis(self):
        self.assert_skipped(self.skip("too tired, skipping this morning", SPINDLE, LISTED), SPINDLE)


class TestNobodyJudged(FibreMorning):

    def test_the_background(self):
        out = self.t.execute("log_outcome", {"event": self.span(SPINDLE).id, "status": "skipped"})
        self.assert_skipped(out, SPINDLE)

    def test_the_apps_own_controls(self):
        self.t.user_message, self.t.by_hand = "What's left this morning?", True
        try:
            out = self.t.execute("log_outcome", {"event": self.span(SKEINS).id, "status": "skipped"})
        finally:
            self.t.user_message, self.t.by_hand = None, False
        self.assert_skipped(out, SKEINS)


if __name__ == "__main__":
    unittest.main()
