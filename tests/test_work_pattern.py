"""Work follows what the user says (work_pattern.py): no guessed Work block on a day nobody said is a work day; a
correction for one day changes that day only; the same correction on the same weekday a week later becomes the usual
week; a stretch of days off never does.

The test person works at a pottery studio, 11:00 AM - 6:00 PM, Wednesday to Sunday, and takes the canoe out on days
off.
"""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.config import ThirtiesConfig
from thirties_core.models import SpanCategory
from thirties_core.plan_tools import PlanTools
from thirties_core.scheduler import DeterministicScheduler, StateDatabase
from thirties_core.calendar_engine import MockCalendarEngine
from thirties_core.week import day_hours, work_days
from thirties_core.work_pattern import corrections

THU = DAY                        # Thursday, Oct 15
WED = DAY - timedelta(days=1)


def work_on(plan):
    return [s for s in plan.spans if s.category == SpanCategory.WORK]


class TestNothingGuessed(unittest.TestCase):

    def test_a_week_nobody_gave_has_no_work(self):
        self.assertEqual(ThirtiesConfig().general.work_days, [])
        from tests.fixtures import temp_dir
        from pathlib import Path
        sched = DeterministicScheduler(ThirtiesConfig(), StateDatabase(Path(temp_dir("work_")) / "s.sqlite"),
                                       MockCalendarEngine([]))
        for offset in range(7):
            plan, _ = sched.build_day_plan(DAY + timedelta(days=offset))
            self.assertEqual(work_on(plan), [], plan.target_date)
            self.assertTrue(any(s.category == SpanCategory.SLEEP for s in plan.spans))  # sleep is still planned


class PatternCase(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        gen = self.sched.config.general
        gen.work_days = ["wed", "thu", "fri", "sat", "sun"]
        gen.work_start_thirty, gen.work_end_thirty = 22, 35  # 11:00 AM - 6:00 PM

    @property
    def gen(self):
        return self.sched.config.general

    def tools_on(self, day, hhmm="09:00"):
        set_debug_time(at(hhmm, (day - DAY).days))
        plan, pending = self.sched.build_day_plan(day)
        return PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db))

    def say(self, t, message, tool, args):
        t.user_message = message
        try:
            return t.execute(tool, args)
        finally:
            t.user_message = None

    def off_today(self, day, message="no work today, taking the canoe out"):
        t = self.tools_on(day)
        return self.say(t, message, "set_days_off", {"start": "today"})

    def hours_today(self, day, start, end, message="the studio pushed my shift back an hour"):
        t = self.tools_on(day)
        work = work_on(t.day_plan)[0]
        return self.say(t, message, "update_event", {"event": work.id, "start": start, "end": end})

    def work_times(self, day):
        plan, _ = self.sched.build_day_plan(day)
        return [(s.start_dt, s.end_dt) for s in work_on(plan)]


class TestOneDayOnly(PatternCase):

    def test_a_day_off_changes_that_day_only(self):
        out = self.off_today(THU)
        self.assertEqual(self.work_times(THU), [], out)
        self.assertIn("thu", work_days(self.gen))
        self.assertEqual(self.work_times(THU + timedelta(days=7)), [(at("11:00", 7), at("18:00", 7))])
        self.assertNotIn("usual week", out)

    def test_other_hours_for_one_day_change_that_day_only(self):
        out = self.hours_today(WED, "12:00 PM", "7:00 PM")
        self.assertEqual(self.work_times(WED), [(at("12:00", -1), at("19:00", -1))], out)
        self.assertEqual((day_hours(self.gen, WED).work_start, day_hours(self.gen, WED).work_end), (22, 35))
        self.assertEqual(self.work_times(WED + timedelta(days=7)), [(at("11:00", 6), at("18:00", 6))])
        self.assertEqual(corrections(self.sched.state_db), [{"date": WED.isoformat(), "work": [24, 37]}])


class TestRepeatedBecomesTheWeek(PatternCase):

    def test_two_thursdays_off_running_make_thursday_a_day_off(self):
        self.off_today(THU)
        out = self.off_today(THU + timedelta(days=7))
        self.assertNotIn("thu", work_days(self.gen))
        self.assertIn("Thursdays are now days off", out)
        self.assertEqual(self.work_times(THU + timedelta(days=14)), [])
        self.assertEqual(self.work_times(DAY + timedelta(days=15)), [(at("11:00", 15), at("18:00", 15))])  # Friday

    def test_work_taken_off_the_day_counts_as_a_day_off(self):
        for day in (THU, THU + timedelta(days=7)):
            t = self.tools_on(day)
            self.say(t, "studio shift is cancelled for me", "remove_event", {"event": work_on(t.day_plan)[0].id})
        self.assertNotIn("thu", work_days(self.gen))

    def test_two_wednesdays_with_the_same_hours_change_wednesdays(self):
        self.hours_today(WED, "12:00 PM", "7:00 PM")
        out = self.hours_today(WED + timedelta(days=7), "12:00 PM", "7:00 PM")
        self.assertIn("usual work on Wednesdays now", out)
        hours = day_hours(self.gen, WED)
        self.assertEqual((hours.works, hours.work_start, hours.work_end), (True, 24, 37))
        self.assertEqual((day_hours(self.gen, THU).work_start, day_hours(self.gen, THU).work_end), (22, 35))
        self.assertEqual(self.work_times(WED + timedelta(days=14)), [(at("12:00", 13), at("19:00", 13))])

    def test_different_corrections_teach_nothing(self):
        self.hours_today(WED, "12:00 PM", "7:00 PM")
        self.hours_today(WED + timedelta(days=7), "1:00 PM", "7:00 PM", "kiln room's booked, I'm in at one")
        self.assertEqual((day_hours(self.gen, WED).work_start, day_hours(self.gen, WED).work_end), (22, 35))

    def test_a_day_off_and_a_change_of_hours_teach_nothing(self):
        self.off_today(WED)
        self.hours_today(WED + timedelta(days=7), "12:00 PM", "7:00 PM")
        self.assertIn("wed", work_days(self.gen))
        self.assertEqual(day_hours(self.gen, WED).work_start, 22)

    def test_a_stretch_of_days_off_is_no_pattern(self):
        t = self.tools_on(THU)
        self.say(t, "I'm off work through next Saturday, canoe trip", "set_days_off",
                 {"through": (THU + timedelta(days=9)).strftime("%b %d")})
        t = self.tools_on(THU + timedelta(days=7))
        self.say(t, "no work today either", "set_days_off", {"start": "today"})
        self.assertIn("thu", work_days(self.gen))

    def test_a_late_start_recorded_is_not_a_correction(self):
        for day in (WED, WED + timedelta(days=7)):
            t = self.tools_on(day, "12:30")
            self.say(t, "got to work at 12", "log_day_times", {"work_started": "12:00 PM"})
        self.assertEqual(day_hours(self.gen, WED).work_start, 22)
        self.assertEqual(corrections(self.sched.state_db), [])


if __name__ == "__main__":
    unittest.main()
