"""The pipeline's Python-owned failures from the third study round, as deterministic scenarios.

Each test scripts what the understand step returned (so no model runs) and checks what Python did with it. The
items are invented; each docstring says what went wrong, not what anyone said.
"""

import unittest

from tests.fixtures import at
from tests.test_slm_brain import SlmTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus


def span(plan, title):
    return next((s for s in plan.spans if s.title == title), None)


class TestOutcomes(SlmTestCase):

    def add(self, title, start, duration="30 min"):
        self.say(f"{title} at {start}", {"type": "add", "title": title, "start": start, "duration": duration})
        self.assertIsNotNone(span(self.m.day_plan, title))

    def test_starting_an_item_added_earlier_marks_it_underway(self):
        """An item added in the evening, then reported as started: the app said it couldn't find it."""
        self.add("Sketch garden poster ideas", "8:00 PM")
        set_debug_time(at("21:10"))
        out = self.say("sketching now", {"type": "outcome", "target": "Sketch garden poster ideas",
                                         "status": "started", "started": "9:10"})
        self.assertNotIn("couldn't find", out)
        self.assertIn("Sketch garden poster ideas", out)

    def test_done_late_by_part_of_its_name(self):
        """At the bedtime check-in an item named by part of its title, done earlier, wasn't found."""
        self.add("Prep logo concepts", "7:00 PM")
        set_debug_time(at("22:15"))
        out = self.say("logo concepts happened", {"type": "outcome", "target": "Logo concepts", "status": "done",
                                                  "finished": "now"})
        self.assertNotIn("couldn't find", out)
        self.assertEqual(span(self.m.day_plan, "Prep logo concepts").status, SpanStatus.DONE)

    def test_a_failed_outcome_says_why(self):
        """Every failure said "couldn't find it on today's plan", even when the item was there."""
        self.add("Water the ferns", "7:00 PM")
        out = self.say("ferns are done", {"type": "outcome", "target": "Water the ferns", "status": "done",
                                          "finished": "not a time at all"})
        self.assertNotIn("couldn't find Water the ferns", out)


class TestMoves(SlmTestCase):

    def test_a_done_item_is_never_moved_by_a_near_name(self):
        """A move aimed at something no longer on the plan landed on a done item that shared a few words."""
        self.say("clear the bins by the water heater at 5", {"type": "add", "title": "Clear the bins by the water heater",
                                                             "start": "5:00 PM", "duration": "30 min"})
        self.say("bins are done", {"type": "outcome", "target": "Clear the bins by the water heater",
                                   "status": "done"})
        before = span(self.m.day_plan, "Clear the bins by the water heater").start_dt
        out = self.say("the water heater visit moved to 7", {"type": "move", "target": "Water heater visit",
                                                             "start": "7:00 PM"})
        self.assertNotIn("Moved Clear the bins", out)
        s = span(self.m.day_plan, "Clear the bins by the water heater")
        self.assertEqual(s.status, SpanStatus.DONE)
        self.assertEqual(s.start_dt, before)

    def test_yesterday_is_ignored_for_an_item_only_on_today(self):
        """Moves of today's items came back tagged with yesterday and went wrong (a one-hour item ran to midnight)."""
        self.say("invoice review 7 to 8", {"type": "add", "title": "Invoice review", "start": "7:00 PM",
                                           "end": "8:00 PM"})
        out = self.say("invoice review until 9 instead", {"type": "move", "target": "Invoice review", "end": "9:00",
                                                          "day": "yesterday"})
        s = span(self.m.day_plan, "Invoice review")
        self.assertEqual((s.start_dt, s.end_dt), (at("19:00"), at("21:00")), out)


class TestNoInternalText(SlmTestCase):

    def test_no_ids_in_what_the_person_reads(self):
        """A reply carried a tool result with an item id in it."""
        out = self.say("breakfast at 5", {"type": "add", "title": "Breakfast", "start": "5:00 PM", "duration": "1 hour"})
        self.assertNotIn("[id ", out)
        self.assertNotIn("task_id", out)

    def test_an_item_placed_in_a_stated_window_reads_like_any_add(self):
        """An item with no time but a window the person gave came back as the raw tool result, id and all."""
        out = self.say("between 7 and 9 tonight I need to fold laundry",
                       {"type": "add", "title": "Fold laundry", "duration": "30 min"})
        self.assertIn("Added Fold laundry: 7:00 PM", out)
        self.assertNotIn("[id ", out)


class TestDaysAndFutureOutcomes(SlmTestCase):

    def test_a_day_nobody_said_is_ignored(self):
        """Moves came back tagged with yesterday when nobody spoke of yesterday; an end of 12:00 became midnight."""
        set_debug_time(at("11:00"))
        self.say("budget review 11 to 12", {"type": "add", "title": "Budget review", "start": "11:00 AM",
                                            "end": "12:00 PM"})
        self.say("budget review until 1", {"type": "move", "target": "Budget review", "end": "1:00",
                                           "day": "yesterday"})
        s = span(self.m.day_plan, "Budget review")
        self.assertEqual((s.start_dt, s.end_dt), (at("11:00"), at("13:00")))

    def test_done_later_is_a_plan_not_a_report(self):
        """'Finishing it before 2:30' at 11:00 came back as done at 2:30, and the reply said it couldn't be found."""
        set_debug_time(at("11:00"))
        self.say("order the boots at 1", {"type": "add", "title": "Order the boots", "start": "1:00 PM",
                                          "duration": "30 min"})
        out = self.say("finishing the boots before 2:30", {"type": "outcome", "target": "Order the boots",
                                                            "status": "done", "finished": "2:30"})
        self.assertNotIn("couldn't find", out)
        self.assertEqual(span(self.m.day_plan, "Order the boots").status, SpanStatus.PLANNED)


class TestDoneStaysDone(SlmTestCase):

    def test_done_by_another_wording_marks_the_planned_item(self):
        """An item reported done in other words was ticked, but its planned copy stayed open and the bedtime
        check-in asked about it again."""
        set_debug_time(at("12:35"))
        self.say("sort the shed shelves 10 to 11", {"type": "add", "title": "Sort the shed shelves",
                                                    "start": "10:00 AM", "end": "11:00 AM"})
        self.say("shed shelving is organized", {"type": "outcome", "target": "Organize the shed shelving units",
                                                "status": "done", "finished": "12:35 PM"})
        self.assertEqual(span(self.m.day_plan, "Sort the shed shelves").status, SpanStatus.DONE)

    def test_whats_done_in_a_message_isnt_added_again_in_it(self):
        """An item marked done was re-added under a longer name in the same turn."""
        self.say("change the sheets at 5", {"type": "add", "title": "Change the sheets", "start": "5:00 PM",
                                            "duration": "30 min"})
        out = self.say("stripped the sheets and started the washer",
                       {"type": "outcome", "target": "Change the sheets", "status": "done"},
                       {"type": "add", "title": "Wash and change the sheets", "start": "now", "duration": "30 min"})
        self.assertIsNone(span(self.m.day_plan, "Wash and change the sheets"), out)


class TestUnaskedChanges(SlmTestCase):

    def test_naming_an_appointment_to_plan_around_it_never_removes_it(self):
        """A message describing an appointment's window came back with a removal, and the appointment was
        declined."""
        out = self.say("the dentist is 6 to 6:30 so I'm out then; I'll read before it",
                       {"type": "remove", "target": "Dentist"},
                       {"type": "add", "title": "Read", "before": "Dentist", "duration": "30 min"})
        self.assertNotIn("Declined", out)
        dentist = span(self.m.day_plan, "Dentist")
        self.assertIsNotNone(dentist)
        self.assertNotEqual(dentist.status, SpanStatus.SKIPPED)

    def test_asking_to_cancel_still_removes(self):
        out = self.say("cancel the dentist", {"type": "remove", "target": "Dentist"})
        self.assertIn("Declined Dentist", out)

    def test_now_for_a_move_is_this_minute(self):
        """"Breakfast is now" moved breakfast to the half hour before, already past."""
        set_debug_time(at("16:40"))
        self.say("snack at 7", {"type": "add", "title": "Snack", "start": "7:00 PM", "duration": "30 min"})
        self.say("snack is now actually", {"type": "move", "target": "Snack", "start": "now"})
        self.assertEqual(span(self.m.day_plan, "Snack").start_dt, at("16:40"))


if __name__ == "__main__":
    unittest.main()
