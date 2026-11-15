"""Meal windows (meals.py): a meal resists being pushed out of its window, and gives way only when the day is full.
Invented items throughout."""

import unittest

from tests.fixtures import DAY, at, build_day
from thirties_core.astronomy import set_debug_time
from thirties_core.meals import meal_word, window
from thirties_core.plan_tools import PlanTools


def span(t, title):
    return next(s for s in t.day_plan.spans if s.title == title)


class WindowTest(unittest.TestCase):
    def test_a_meal_has_a_window_and_a_meal_with_someone_doesnt(self):
        self.assertEqual(meal_word("Lunch"), "lunch")
        self.assertIsNone(meal_word("Lunch with Sam"))  # an appointment
        opens, closes = window("Lunch", at("07:00"))
        self.assertEqual((opens, closes), (at("11:30"), at("14:00")))
        self.assertEqual(window("Fish food", at("07:00"), usual=at("17:00")), (at("16:00"), at("18:30")))
        self.assertIsNone(window("Fold laundry", at("07:00")))


class MealTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched, plan, events = build_day()
        self.t = PlanTools(plan, events, self.sched)
        for s in [s for s in self.t.day_plan.spans if s.title == "Work"]:  # a day off: room to move things
            self.t.execute("remove_event", {"event": s.id})

    def test_a_redraw_keeps_lunch_in_its_window(self):
        """A redraw used to place every item still to do first, and the meal well past its window."""
        self.t.execute("add_event", {"title": "Lunch", "start": "12:00 PM", "duration": "30 min"})
        for title, start in (("Sort the mail", "10:00 AM"), ("Repot the fern", "11:00 AM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "1 hour"})
        set_debug_time(at("12:40"))  # nothing marked yet: the redraw puts both still-to-do items first
        out = self.t.execute("redraw", {})
        lunch = span(self.t, "Lunch")
        self.assertLessEqual(lunch.end_dt, at("14:00"), out)  # still in its window
        self.assertEqual(lunch.start_dt, at("13:00"))  # at the earliest room in it, from now
        self.assertNotIn("late today", out)

    def test_with_no_room_in_its_window_it_gives_way_and_says_why(self):
        self.t.execute("add_event", {"title": "Lunch", "start": "12:00 PM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Dentist", "start": "11:30 AM", "end": "2:30 PM", "fixed": True})
        set_debug_time(at("11:10"))
        out = self.t.execute("redraw", {})
        self.assertIn("Lunch is late today: no free stretch for it before 2:00 PM.", out)
        self.assertEqual(span(self.t, "Lunch").start_dt, at("14:30"))  # the first room after

    def test_a_move_the_user_didnt_ask_for_never_pushes_lunch_out(self):
        self.t.execute("add_event", {"title": "Lunch", "start": "1:30 PM", "duration": "30 min"})
        self.t.user_message = "the bike tune-up has to go after the call"
        out = self.t.execute("update_event", {"event": "Lunch", "start": "3:00 PM"})
        self.assertTrue(out.startswith("NOT_DONE"), out)
        self.assertIn("window ends at 2:00 PM", out)
        self.t.user_message = "lunch can wait until 3 today"
        out = self.t.execute("update_event", {"event": "Lunch", "start": "3:00 PM"})
        self.assertFalse(out.startswith("NOT_DONE"), out)  # the user named it: it goes


if __name__ == "__main__":
    unittest.main()
