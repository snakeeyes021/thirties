"""Times said relative to now ("glazed it 35 minutes ago", "began 25 min ago", "at it since 9:45") are worked out by
Python and recorded as said (relative_times.py): never in the future, never before the day began. With no time said,
an outcome is recorded as before, except that a done is never put later than now.

The test person works a late shift at a pottery studio (11:00 AM - 6:00 PM) and paddles a canoe before work.
"""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import PlanTools
from thirties_core.relative_times import amount_before, said_times


class TestReadingTheWords(unittest.TestCase):
    NOW = at("15:10")

    def said(self, message, default="end"):
        return said_times(message, None, self.NOW, at("07:00"), default)

    def test_amounts(self):
        for words, minutes in (("30 minutes", 30), ("30", 30), ("an hour", 60), ("an hour and a half", 90),
                               ("half an hour", 30), ("20 min", 20), ("a couple hours", 120), ("ten minutes", 10),
                               ("1 hour and 15 minutes", 75), ("about 40 mins", 40), ("a few minutes", 5),
                               ("1.5 hours", 90), ("2 and a half hours", 150)):
            self.assertEqual(amount_before("glazed the mugs " + words), minutes, words)
        self.assertIsNone(amount_before("the canoe"))
        self.assertIsNone(amount_before("40 hours"))  # not about today

    def test_an_end_or_a_start(self):
        self.assertEqual(self.said("glazed the mugs 30 minutes ago"), (None, at("14:40")))
        self.assertEqual(self.said("started trimming 20 min ago"), (at("14:50"), None))
        self.assertEqual(self.said("trimming since 2", "end"), (at("14:00"), None))
        self.assertEqual(self.said("since 9am"), (at("09:00"), None))
        self.assertEqual(self.said("started 40 min ago and finished 10 min ago"), (at("14:30"), at("15:00")))
        self.assertEqual(self.said("wrapped up the kiln log at 2:45"), (None, at("14:45")))
        self.assertEqual(self.said("75 minutes ago", "start"), (at("13:55"), None))

    def test_never_later_than_now_nor_before_the_day(self):
        self.assertEqual(self.said("since 4"), (at("07:00"), None))  # 4 AM: before the day began
        self.assertEqual(self.said("twelve hours ago"), (None, at("07:00")))
        start, end = said_times("since 3:20", None, self.NOW, at("07:00"))
        self.assertLessEqual(start, self.NOW)

    def test_a_clock_time_with_no_start_or_end_word_is_left_to_the_model(self):
        self.assertEqual(self.said("the canoe trip is at 6 tomorrow"), (None, None))
        self.assertEqual(self.said("glazed the mugs"), (None, None))

    def test_the_clause_about_the_item(self):
        about = lambda clause: "buckets" in clause  # noqa: E731
        self.assertEqual(said_times("paddles finished at 12:30, and the buckets wrapped up 40 min ago", about,
                                    self.NOW, at("07:00")), (None, at("14:30")))


class RelativeCase(unittest.TestCase):
    """Thursday: work at the studio 11:00 AM - 6:00 PM; canoe paddles and glaze buckets in the morning, the kiln
    loaded in the evening."""

    def setUp(self):
        self.now("10:40")
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        gen = self.sched.config.general
        gen.work_start_thirty, gen.work_end_thirty = 22, 35  # 11:00 AM - 6:00 PM
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db))
        for title, start in (("Wax the canoe paddles", "9:00 AM"), ("Sort the glaze buckets", "9:30 AM"),
                             ("Load the kiln", "7:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes"})

    @staticmethod
    def now(hhmm):
        set_debug_time(at(hhmm))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def run_tool(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def done(self, message, title, **times):
        return self.run_tool(message, "log_outcome", {"event": self.span(title).id, "status": "done", **times})

    def times(self, title):
        s = self.span(title)
        return s.actual_start, s.actual_end


class TestOutcomes(RelativeCase):

    def test_done_a_while_ago_ends_then(self):
        out = self.done("waxed the canoe paddles 30 minutes ago", "Wax the canoe paddles")
        self.assertEqual(self.times("Wax the canoe paddles"), (at("09:40"), at("10:10")), out)
        self.assertEqual(self.span("Wax the canoe paddles").status, SpanStatus.DONE)
        self.assertEqual(self.span("Wax the canoe paddles").time_source, "estimated")  # only the end was said

    def test_a_time_the_model_worked_out_wrong_is_replaced(self):
        self.done("paddles waxed 65 minutes ago", "Wax the canoe paddles", finished="9:15 AM")
        self.assertEqual(self.times("Wax the canoe paddles")[1], at("09:35"))

    def test_each_item_gets_its_own_time(self):
        message = "finished the glaze buckets ten minutes ago; the paddles I'd wrapped up at 9:20"
        self.done(message, "Wax the canoe paddles")
        self.done(message, "Sort the glaze buckets")
        self.assertEqual(self.times("Wax the canoe paddles")[1], at("09:20"))
        self.assertEqual(self.times("Sort the glaze buckets")[1], at("10:30"))

    def test_an_item_planned_for_later_is_never_recorded_in_the_future(self):
        self.done("loaded the kiln 15 min ago", "Load the kiln")
        self.assertEqual(self.times("Load the kiln"), (at("09:55"), at("10:25")))

    def test_no_time_said_and_planned_hours_later_still_ends_by_now(self):
        self.done("kiln is loaded", "Load the kiln")
        start, end = self.times("Load the kiln")
        self.assertLessEqual(end, at("10:40"))
        self.assertEqual(end - start, timedelta(minutes=30))

    def test_started_a_while_ago_and_done_now(self):
        self.done("did the glaze buckets, started 20 minutes ago, done now", "Sort the glaze buckets")
        self.assertEqual(self.times("Sort the glaze buckets"), (at("10:20"), at("10:40")))

    def test_started_and_finished_both_said_are_reported(self):
        self.done("sorted the glaze buckets, began 50 minutes ago, finished 10 minutes ago", "Sort the glaze buckets")
        self.assertEqual(self.times("Sort the glaze buckets"), (at("09:50"), at("10:30")))
        self.assertEqual(self.span("Sort the glaze buckets").time_source, "reported")

    def test_never_before_the_day_began(self):
        self.now("07:30")
        self.done("paddles waxed 65 minutes ago", "Wax the canoe paddles")
        self.assertEqual(self.times("Wax the canoe paddles")[1], at("07:00"))

    def test_without_the_users_words_times_are_as_given(self):
        self.t.execute("log_outcome", {"event": self.span("Wax the canoe paddles").id, "status": "done",
                                       "started": "9:05 AM", "finished": "9:35 AM"})
        self.assertEqual(self.times("Wax the canoe paddles"), (at("09:05"), at("09:35")))

    def test_something_done_that_was_never_planned(self):
        out = self.run_tool("patched the canoe seat 45 minutes ago", "log_outcome",
                            {"event": "Patch the canoe seat", "status": "done", "new": True})
        seat = self.span("Patch the canoe seat")
        self.assertEqual(seat.actual_end, at("09:55"), out)


class TestUnderWay(RelativeCase):

    def test_doing_now_since_a_clock_time(self):
        out = self.run_tool("been wedging clay since 10", "doing_now", {"item": "Wedge clay"})
        wedge = self.span("Wedge clay")
        self.assertEqual((wedge.actual_start, wedge.start_dt), (at("10:00"), at("10:00")), out)
        self.assertIn("started at 10:00 AM", out)

    def test_doing_now_started_a_while_ago(self):
        self.run_tool("started wedging clay 20 min ago", "doing_now", {"item": "Wedge clay"})
        wedge = self.span("Wedge clay")
        self.assertEqual((wedge.actual_start, wedge.start_dt), (at("10:20"), at("10:00")))
        self.assertEqual(wedge.status, SpanStatus.PLANNED)  # under way, not done

    def test_doing_now_with_no_time_starts_now(self):
        self.run_tool("wedging clay", "doing_now", {"item": "Wedge clay"})
        self.assertEqual(self.span("Wedge clay").actual_start, at("10:40"))

    def test_started_early_a_while_ago(self):
        self.run_tool("started loading the kiln 15 minutes ago", "still_doing", {"event": "Load the kiln"})
        kiln = self.span("Load the kiln")
        self.assertEqual((kiln.actual_start, kiln.start_dt), (at("10:25"), at("10:25")))
        self.assertGreater(kiln.end_dt, at("10:40"))


class TestDayTimes(RelativeCase):

    def test_work_started_a_while_ago(self):
        self.now("11:35")
        out = self.run_tool("started work 20 minutes ago", "log_day_times", {"work_started": "11:00 AM"})
        self.assertIn("started work 11:15 AM", out)

    def test_woke_a_while_ago(self):
        self.now("08:00")
        out = self.run_tool("woke up 85 minutes ago", "log_day_times", {"woke": "7:00 AM"})
        self.assertIn("woke 6:35 AM", out)  # before the day's canvas: a wake time isn't held to it

    def test_each_time_goes_with_its_own_words(self):
        self.now("11:35")
        out = self.run_tool("woke up four hours ago and started work 20 minutes ago", "log_day_times",
                            {"woke": "7:00 AM", "work_started": "11:00 AM"})
        self.assertIn("woke 7:35 AM", out)
        self.assertIn("started work 11:15 AM", out)

    def test_clock_times_the_user_gave_stay_as_given(self):
        self.now("11:35")
        out = self.run_tool("I woke up at 8 and started work at 11:10", "log_day_times",
                            {"woke": "8:00 AM", "work_started": "11:10 AM"})
        self.assertIn("woke 8:00 AM", out)
        self.assertIn("started work 11:10 AM", out)


if __name__ == "__main__":
    unittest.main()
