"""Not yours today (not_yours.py): someone else's events in a section of their own, at the end, each hideable.
Invented events throughout."""

import unittest

from tests.fixtures import DAY, make_scheduler
from tests.test_calendar_choices import partners
from thirties_core.plan_tools import PlanTools


class NotYoursTest(unittest.TestCase):
    def setUp(self):
        self.sched = make_scheduler([partners("play", "19:00", "21:00", title="School play"),
                                     partners("band", "14:00", "15:30", title="Band practice")])
        self.sched.config.calendar.modes = {"jamie": "show"}
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched)

    def test_its_own_section_last_in_time_order(self):
        text = self.t.schedule_text()
        section = text.split("Not yours today:\n")[1]
        self.assertEqual(section, "Band practice, 2:00 PM – 3:30 PM\nSchool play, 7:00 PM – 9:00 PM")
        self.assertNotIn("Band", text.split("Not yours today:")[0])

    def test_hide_that_one_for_today_only(self):
        out = self.t.execute("hide_for_today", {"event": "the play"})
        self.assertTrue(out.startswith("Hidden for today: School play."), out)
        self.assertNotIn("School play", self.t.schedule_text())
        self.assertEqual(len(self.t.execute("not_yours_today", {}).splitlines()), 1)  # the Agenda's list too
        tomorrow, _ = self.sched.build_day_plan(DAY.replace(day=DAY.day + 1))
        from thirties_core.not_yours import hidden
        self.assertEqual(hidden(self.sched.state_db, tomorrow.target_date), [])  # a choice about one day
        self.assertIn("School play", [s.title for s in self.t.day_plan.spans_in_view()])  # nothing else changed

    def test_hiding_asks_which_when_unclear_and_never_touches_your_own(self):
        self.t.execute("add_event", {"title": "Dinner", "start": "6:00 PM", "duration": "1 hour"})
        out = self.t.execute("hide_for_today", {"event": "Dinner"})
        self.assertTrue(out.startswith("NOT_DONE"), out)
        self.assertIn("Dinner", self.t.schedule_text())

    def test_not_mine_stays_for_the_whole_series(self):
        play = next(s for s in self.t.day_plan.spans_in_view() if s.title == "School play")
        self.t.execute("resolve_calendar_event", {"event": play.id, "attend": False, "series": True})
        self.assertNotIn("School play", self.t.schedule_text())
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertNotIn("School play", [s.title for s in plan.spans_in_view()])  # every time, from now on

    def test_hiding_a_whole_calendar_still_needs_the_word_calendar(self):
        from thirties_core.intents import guard
        tool, _args, note = guard("set_calendar_mode", {"calendar": "Jamie", "mode": "ignore"}, "hide the play")
        self.assertEqual(tool, "")
        self.assertIn("Hide that event instead", note)


if __name__ == "__main__":
    unittest.main()
