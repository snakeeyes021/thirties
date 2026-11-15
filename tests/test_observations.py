"""Observations from the statistics go into the profile, once a week. Invented history throughout."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import AssistantTools, Services
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.observations import lasting, merged, refresh_if_due
from thirties_core.plan_tools import PlanTools


class MergeTest(unittest.TestCase):
    def test_only_lasting_patterns(self):
        facts = ["Of 12 planned activities with a known outcome, 9 were done, 1 partly done and 2 skipped (79% completion).",
                 "3 past planned activities have no recorded outcome yet.",
                 "'ironing' usually takes about 1.5x the planned time, about 45m (4 times measured).",
                 "Plans in the morning get done 90% of the time, versus 40% in the evening.",
                 "'Clean the gutters' has been put off 4 times."]
        self.assertEqual(lasting(facts), facts[2:])

    def test_the_users_own_lines_stay_and_last_weeks_are_replaced(self):
        current = ("- Likes to batch errands on Saturdays\n"
                   "- 'ironing' usually takes about 2x the planned time (from the statistics, 2026-10-01)")
        out = merged(current, ["'ironing' usually takes about 1.5x the planned time"], date(2026, 10, 8))
        self.assertEqual(out, "- Likes to batch errands on Saturdays\n"
                              "- 'ironing' usually takes about 1.5x the planned time (from the statistics, 2026-10-08)")


class InTheProfileTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        db = self.sched.state_db
        for d in range(1, 6):  # ironing planned for 30 minutes, really an hour, five times
            start = at("18:00", -d)
            s = EventSpan(f"ir{d}", "Ironing", start, start + timedelta(minutes=30), SpanCategory.TASK,
                          status=SpanStatus.DONE)
            s.actual_start, s.actual_end, s.time_source = start, start + timedelta(minutes=60), "reported"
            db.upsert_span(s)
        plan, pending = self.sched.build_day_plan(DAY)
        self.pt = PlanTools(plan, pending, self.sched)
        self.pt.assistant = AssistantTools(self.pt, Services.for_db(db))
        self.pt.services = self.pt.assistant.s

    def test_written_once_a_week_and_shown_in_the_prompt(self):
        from thirties_core.brains.frontier import FrontierBrain
        from thirties_core.inference import MockInferenceEngine
        prompt = FrontierBrain(self.pt, MockInferenceEngine(), [], []).build_system_prompt()
        section = self.pt.services.profile.sections()["Observations"]
        self.assertIn("'ironing' usually takes about 2.0x the planned time", section)
        self.assertIn("(from the statistics, 2026-10-15)", section)
        self.assertIn("'ironing' usually takes about 2.0x", prompt.split("USER PROFILE")[1])  # the same turn
        self.pt.services.profile.set_section("Observations", "- Prefers ironing with a podcast")
        self.assertFalse(refresh_if_due(self.pt.assistant, DAY + timedelta(days=3)))  # not again within the week
        self.assertEqual(self.pt.services.profile.sections()["Observations"], "- Prefers ironing with a podcast")
        self.assertTrue(refresh_if_due(self.pt.assistant, DAY + timedelta(days=7)))
        section = self.pt.services.profile.sections()["Observations"]
        self.assertTrue(section.startswith("- Prefers ironing with a podcast\n- 'ironing' usually takes"), section)


if __name__ == "__main__":
    unittest.main()
