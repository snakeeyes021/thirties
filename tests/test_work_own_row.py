"""Work and an item that begin together are shown on separate lines in the record of the day.

Seen in a simulated day: the whole day read "11:00 AM – 6:00 PM Work + Wedge the clay (until 12:30 PM)", and the user
read it as the clay taking the whole afternoon; it took several messages to sort out. The Work block came
from the hours the day was set up with, not from anything Thirties guessed. Now the work stretch and the items
inside it are separate rows, each with its own times, and a day with no work known shows none.

On invented items: someone on the late shift at a pottery studio (11:00 AM – 6:00 PM).
"""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.messages import whole_day_lines
from thirties_core.models import EventSpan, SpanCategory


class TestWorkHasItsOwnRow(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.general.work_start_thirty, self.sched.config.general.work_end_thirty = 22, 35
        self.sched.state_db.upsert_span(EventSpan("wedge", "Wedge the clay", at("11:00"), at("12:30"),
                                                  SpanCategory.ACTIVITY))
        self.plan, _ = self.sched.build_day_plan(DAY)

    def test_the_record_splits_work_from_the_first_item(self):
        lines = whole_day_lines(self.plan)
        self.assertIn("11:00 AM – 6:00 PM Work", lines)
        self.assertIn("11:00 AM – 12:30 PM Wedge the clay", lines)
        self.assertFalse(any("Work +" in line or "+ Work" in line for line in lines), lines)

    def test_a_day_with_no_work_known_shows_none(self):
        sched = make_scheduler()
        sched.config.general.work_days = []
        plan, _ = sched.build_day_plan(DAY)
        self.assertFalse(any(s.title == "Work" for s in plan.spans))


if __name__ == "__main__":
    unittest.main()
