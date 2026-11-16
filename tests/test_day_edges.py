"""An early-morning time never lands on the wrong day without saying so, and nothing leaves the day unseen.

The day's plan in these tests runs from 7:00 AM to 7:00 AM the next morning. Up at 6:40, the user asked for the
walk at 6:15: the walk moved to 6:15 the next morning while the tool said "6:15 AM"; shifted earlier instead, it
dropped off the day, so the schedule no longer had it while adding it again said it was already planned.
"""

from tests.fixtures import DAY, at
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE


class EdgeCase(RoutineCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("06:40"))
        out = self.t.execute("add_event", {"title": "Walk the dog", "start": "7:00 AM", "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.walk = self.spans(self.t, "Walk the dog")[0]

    def said(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None


class TestThisMorningBeforeThePlan(EdgeCase):
    def test_moving_it_to_this_morning_says_so_instead_of_moving_it_to_tomorrow(self):
        out = self.said("walk is at 6:15, now", "update_event", {"event": self.walk.id, "start": "6:15 AM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("before today's plan begins", out)
        self.assertIn("tomorrow morning", out)
        self.assertEqual(self.walk.start_dt, at("07:00"))  # left where it was, on today's plan

    def test_said_for_tomorrow_it_goes_and_the_result_names_the_day(self):
        out = self.said("walk at 6:15 tomorrow instead", "update_event", {"event": self.walk.id, "start": "6:15 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("tomorrow morning", out)
        self.assertEqual(self.walk.start_dt, at("06:15", 1))

    def test_in_the_evening_an_early_time_is_the_next_morning_and_says_so(self):
        set_debug_time(at("21:00"))
        out = self.said("put the walk at 6:15", "update_event", {"event": self.walk.id, "start": "6:15 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("tomorrow morning", out)

    def test_adding_at_this_mornings_time_says_so(self):
        out = self.said("feeding the cat at 6:30", "add_event",
                        {"title": "Feed the cat", "start": "6:30 AM", "duration": "15 minutes"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.spans(self.t, "Feed the cat"), [])

    def test_done_this_morning_is_recorded_this_morning(self):
        set_debug_time(at("06:55"))
        out = self.said("fed the cat at 6:30", "add_event", {"title": "Feed the cat", "start": "6:30 AM",
                                                             "duration": "15 minutes", "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("this morning", out)
        stored = [s for s in self.sched.state_db.get_spans_between(at("06:00"), at("07:00")) if s.title == "Feed the cat"]
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0].start_dt, stored[0].status), (at("06:30"), SpanStatus.DONE))
        from thirties_core.messages import whole_day_lines  # on the day's record, in its block; the plan starts at 7
        self.assertTrue(any(l.startswith("6:30 AM") and "Feed the cat" in l for l in whole_day_lines(self.t.day_plan)))
        self.assertNotIn("Feed the cat", [s.title for s in self.t.day_plan.spans_in_view()])  # not half on the plan
        self.assertEqual(self.t.day_plan.day_start, at("07:00"))


class TestNothingLeavesTheDayUnseen(EdgeCase):
    def test_a_shift_off_the_start_of_the_day_is_refused(self):
        out = self.said("walk an hour earlier", "update_event", {"event": self.walk.id, "shift": "-60 minutes"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("drop off", out)
        self.assertIn("Walk the dog", self.t.execute("show_schedule", {}))

    def test_the_duplicate_check_sees_the_same_day_as_the_schedule(self):
        stray = EventSpan("stray-1", "Water the ferns", at("06:00"), at("06:30"), SpanCategory.ACTIVITY)
        self.t.day_plan.spans.append(stray)  # off this day's plan, though still held
        self.assertNotIn("Water the ferns", self.t.execute("show_schedule", {}))
        out = self.said("water the ferns at 8", "add_event",
                        {"title": "Water the ferns", "start": "8:00 AM", "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)  # not "already planned" by an item the schedule doesn't show
        self.assertIn("Water the ferns", self.t.execute("show_schedule", {}))


