"""The sun and the moon on request: the next sunrise from now, the moon's phase, the next full and new moon."""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.fixtures import DAY, TZ, at, make_scheduler
from thirties_core.assistant_tools import AssistantTools, Services
from thirties_core.astronomy import moon_phase, next_moon, next_sun_events, set_debug_time
from thirties_core.plan_tools import PlanTools

UTC = timezone.utc


class MoonTest(unittest.TestCase):
    """Against almanac times (UTC) of well-known moons: within a few hours."""

    def assertNear(self, got, want, hours=3):
        self.assertLess(abs((got - want).total_seconds()), hours * 3600, f"{got} vs {want}")

    def test_new_and_full_moons(self):
        self.assertNear(next_moon(datetime(2024, 4, 1, tzinfo=UTC), 0.0), datetime(2024, 4, 8, 18, 21, tzinfo=UTC))
        self.assertNear(next_moon(datetime(2024, 4, 10, tzinfo=UTC), 180.0), datetime(2024, 4, 23, 23, 49, tzinfo=UTC))
        self.assertNear(next_moon(datetime(2025, 1, 20, tzinfo=UTC), 0.0), datetime(2025, 1, 29, 12, 36, tzinfo=UTC))
        self.assertNear(next_moon(datetime(2025, 3, 1, tzinfo=UTC), 180.0), datetime(2025, 3, 14, 6, 55, tzinfo=UTC))

    def test_waxing_and_waning(self):
        after_new = moon_phase(datetime(2025, 2, 2, tzinfo=UTC))
        self.assertTrue(after_new["waxing"])
        self.assertEqual(after_new["name"], "waxing crescent")
        after_full = moon_phase(datetime(2025, 3, 19, tzinfo=UTC))
        self.assertFalse(after_full["waxing"])
        self.assertEqual(after_full["name"], "waning gibbous")
        full = moon_phase(datetime(2025, 3, 14, 7, tzinfo=UTC))
        self.assertEqual(full["name"], "full moon")
        self.assertGreater(full["lit"], 0.99)


class SunTest(unittest.TestCase):
    def test_next_sunrise_is_tomorrows_once_todays_has_passed(self):
        evening = at("20:00")
        nxt = next_sun_events(40.7128, -74.0060, evening, TZ)
        self.assertEqual(nxt["sunrise"].date(), DAY + timedelta(days=1))
        self.assertEqual(nxt["sunset"].date(), DAY + timedelta(days=1))
        before_dawn = at("05:00")
        self.assertEqual(next_sun_events(40.7128, -74.0060, before_dawn, TZ)["sunrise"].date(), DAY)


class ToolTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("20:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        self.pt = PlanTools(plan, pending, sched)
        self.pt.assistant = AssistantTools(self.pt, Services.for_db(sched.state_db))

    def test_the_answer(self):
        out = self.pt.execute("sky", {})
        self.assertIn("It's 8:00 PM now.", out)
        self.assertIn("Next sunrise: tomorrow at 7:1", out)
        self.assertRegex(out, r"Moon: (waxing|waning) \w+ \((growing|shrinking) toward (full|new)\), \d+% lit")
        self.assertIn("Next full moon:", out)
        self.assertIn("Next new moon:", out)
        self.assertNotIn("- ", out)  # plain lines

    def test_another_day(self):
        out = self.pt.execute("sky", {"day": "Saturday"})
        self.assertIn("Saturday, October 17: sunrise 7:", out)
        self.assertNotIn("Next sunrise", out)


if __name__ == "__main__":
    unittest.main()
