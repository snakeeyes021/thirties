"""Where a missed item goes next (replan.py): the plan's own examples, with invented items."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, build_day
from thirties_core.astronomy import set_debug_time
from thirties_core.kinds import DAILY, ONCE, ONGOING
from thirties_core.plan_tools import PlanTools
from thirties_core.replan import place_once, replan, spread_ongoing


class ReplanTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("21:00"))
        self.addCleanup(set_debug_time, None)
        self.sched, plan, events = build_day()
        self.t = PlanTools(plan, events, self.sched)

    def fill(self, offset):
        day = (DAY + timedelta(days=offset)).isoformat()
        plan = self.t.plan_for(day)
        for s in [s for s in plan.spans if s.title == "Work"]:  # a day off, then filled to the brim
            self.t.execute("remove_event", {"event": s.id, "day": day})
        self.t.execute("add_event", {"title": f"Busy {offset}", "start": "7:00 AM", "end": "11:30 PM", "day": day,
                                     "fixed": True})

    def test_a_one_off_goes_to_tomorrow(self):
        p = place_once(self.t, "Mail the warranty card", 30, DAY)
        self.assertEqual((p.day, p.why), (DAY + timedelta(days=1), "tomorrow"))

    def test_with_the_next_two_days_full_it_goes_to_the_day_after_those(self):
        self.fill(1)
        self.fill(2)
        p = place_once(self.t, "Read for the pottery class", 60, DAY, due=DAY + timedelta(days=21))
        self.assertEqual(p.day, DAY + timedelta(days=3))
        self.assertIn("the days before are full", p.why)

    def test_never_past_its_deadline(self):
        self.fill(1)
        self.fill(2)
        p = place_once(self.t, "Send the form", 30, DAY, due=DAY + timedelta(days=2))
        self.assertIsNone(p.day)
        self.assertIn("before it's due", p.why)

    def test_missed_time_on_something_ongoing_goes_over_its_next_sessions(self):
        for offset in (1, 2):
            day = (DAY + timedelta(days=offset)).isoformat()
            self.t.execute("add_event", {"title": "Garden app dev", "start": "6:00 PM", "duration": "1 hour",
                                         "day": day})
        p = spread_ongoing(self.t, "Garden app dev", 60, DAY)
        self.assertEqual(len(p.extended), 2, p.why)
        for offset in (1, 2):
            plan = self.t.plan_for((DAY + timedelta(days=offset)).isoformat())
            dev = next(s for s in plan.spans if s.title == "Garden app dev")
            self.assertEqual(dev.end_dt - dev.start_dt, timedelta(minutes=90))  # an extra half hour each

    def test_a_daily_thing_simply_ends(self):
        class A:
            pt = self.t
        self.assertIsNone(replan(A, "Lunch", DAILY, 30, DAY).day)
        self.assertEqual(replan(A, "Mail the warranty card", ONCE, 30, DAY).day, DAY + timedelta(days=1))
        self.assertIsNotNone(replan(A, "Garden app dev", ONGOING, 60, DAY))


if __name__ == "__main__":
    unittest.main()
