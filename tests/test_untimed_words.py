"""Any clear way of saying an item gets no time today is a fast yes for If there's time (if_time.py).

The words are never a no: whether the user asked is the model's call (pencil_in, or add_to_today's only_if_time).
Fixtures are invented.
"""

import unittest

from tests.test_list_placement import ListsTestCase
from thirties_core import if_time
from thirties_core.plan_tools import NOT_DONE

NO_TIME = (
    "no clock time on the gutters at all, just after the roofer",
    "don't give it a time",
    "dont give the shed sweep a time",
    "the bike tune-up doesn't need a time",
    "whenever",
    "repot the fern whenever",
    "whenever you can",
    "no set time for the shed",
    "sometime today",
    "unscheduled is fine",
    "leave it unscheduled",
    "keep it untimed please",
    "no time on it",
    "without a set time",
    "any time today works",
    "at some point I need to oil the hinges",
    "the time doesn't matter",
    "I don't mind when",
    "don't put a time on the shed sweep",
)

TIMED_OR_SILENT = (
    "add the gutters for today",
    "oil the hinges at 3",
    "it can't be whenever, it has to be before noon",
    "don't leave it unscheduled",
    "please don't leave the gutters untimed",
    "whenever you think fits best",
    "sometime this afternoon",
    "I have no time to oil the hinges today",
    "give it a time after lunch",
    "time to head out",
    "",
    None,
)


class WordingTest(unittest.TestCase):
    def test_any_clear_way_of_saying_no_time_counts(self):
        for said in NO_TIME:
            self.assertTrue(if_time.asked_untimed(said), said)

    def test_a_time_or_nothing_said_about_time_doesnt(self):
        for said in TIMED_OR_SILENT:
            self.assertFalse(if_time.asked_untimed(said), said)


class PencilInTest(ListsTestCase):
    def say(self, message, items):
        self.t.user_message = message
        try:
            return self.t.execute("pencil_in", {"items": items})
        finally:
            self.t.user_message = None

    def test_said_in_other_words_it_goes_under_if_theres_time(self):
        self.t.execute("add_event", {"title": "Clean the gutters", "start": "4:00 PM", "duration": "30 min"})
        out = self.say("no clock time on the gutters at all, just after the roofer leaves", ["Clean the gutters"])
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertFalse(any(s.title == "Clean the gutters" for s in self.t.day_plan.spans))
        self.assertIn("Clean the gutters", if_time.items(self.api.notes["n_today"]))
        for message, item in (("unscheduled is fine for the hinges", "Oil the hinges"),
                              ("don't give the fern a time, whenever", "Repot the fern")):
            out = self.say(message, [item])
            self.assertFalse(out.startswith(NOT_DONE), out)
            self.assertIn(item, if_time.items(self.api.notes["n_today"]))

    def test_the_words_are_never_a_no_the_models_call_stands(self):
        out = self.say("the hinges can wait, keep them loose today", ["Oil the hinges"])
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Oil the hinges", if_time.items(self.api.notes["n_today"]))


if __name__ == "__main__":
    unittest.main()
