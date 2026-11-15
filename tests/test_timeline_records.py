"""Creating in the Timeline's past blocks and in the night: a past block takes a record of what happened (done,
its drawn times, never planned), and the night takes items like any other time. Invented items."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.messages import missed_items
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.stats import outcome_stats


def tools_at(now: str) -> PlanTools:
    set_debug_time(at(now))
    sched = make_scheduler()
    plan, pending = sched.build_day_plan(DAY)
    return PlanTools(plan, pending, sched)


class TestRecordsOfWhatHappened(unittest.TestCase):

    def tearDown(self) -> None:
        set_debug_time(None)

    def test_a_past_block_takes_a_record_done_at_its_drawn_times(self) -> None:
        t = tools_at("18:00")
        out = t.execute("add_event", {"title": "Fixed the bike chain", "start": "1:30 PM", "end": "2:30 PM",
                                      "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("as done", out)
        span = next(s for s in t.day_plan.spans if s.title == "Fixed the bike chain")
        self.assertEqual(span.status, SpanStatus.DONE)
        self.assertEqual((span.actual_start, span.actual_end), (at("13:30"), at("14:30")))  # exactly as drawn
        self.assertEqual((span.start_dt, span.end_dt), (at("13:30"), at("14:30")))
        self.assertEqual(span.time_source, "reported")  # the user's own times: they count in the statistics
        self.assertNotIn(span, missed_items(t.day_plan, at("18:00")))  # never "missed", never asked about
        self.assertEqual(outcome_stats(t.day_plan.spans, at("18:00")).done, 1)
        stored = t.scheduler.build_day_plan(DAY)[0]  # saved, as done
        self.assertEqual(next(s.status for s in stored.spans if s.title == "Fixed the bike chain"), SpanStatus.DONE)

    def test_a_minute_time_is_kept_and_a_fixed_routine_never_blocks_a_record(self) -> None:
        from thirties_core.routines import Anchor, RoutineBook
        t = tools_at("18:00")
        RoutineBook(t._state_db).save_anchor(Anchor("Water the ferns", "14:00", 15, fixed=True))
        t.execute("add_event", {"title": "Water the ferns", "start": "2:00 PM", "duration": "15 min", "fixed": True})
        out = t.execute("add_event", {"title": "Phone call with the landlord", "start": "1:50 PM", "end": "2:20 PM",
                                      "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in t.day_plan.spans if s.title == "Phone call with the landlord")
        self.assertEqual(span.start_dt, at("13:50"))  # a record isn't snapped to blocks

    def test_only_what_is_already_over(self) -> None:
        t = tools_at("18:00")
        out = t.execute("add_event", {"title": "Sorting the shed", "start": "5:30 PM", "end": "6:30 PM",
                                      "happened": True})
        self.assertTrue(out.startswith(NOT_DONE) and "hasn't come yet" in out, out)
        self.assertNotIn("Sorting the shed", [s.title for s in t.day_plan.spans])


class TestCreatingAtNight(unittest.TestCase):

    def tearDown(self) -> None:
        set_debug_time(None)

    def test_the_night_takes_an_item_like_any_other_time(self) -> None:
        t = tools_at("18:00")
        sleep = next(s for s in t.day_plan.spans if s.title == "Sleep")
        out = t.execute("add_event", {"title": "Watch the meteor shower", "start": "1:00 AM", "duration": "1 hour"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        span = next(s for s in t.day_plan.spans if s.title == "Watch the meteor shower")
        self.assertEqual(span.start_dt, at("01:00", 1))  # after midnight, on this day's night
        self.assertTrue(sleep.start_dt <= span.start_dt < sleep.end_dt)  # inside the sleep window
        self.assertEqual(span.status, SpanStatus.PLANNED)
        self.assertEqual(next(s for s in t.day_plan.spans if s.title == "Sleep").end_dt, sleep.end_dt)  # untouched


if __name__ == "__main__":
    unittest.main()
