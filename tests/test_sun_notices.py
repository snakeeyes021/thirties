"""A message at sunrise and sunset, when switched on (10-30: the user's lights)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import get_solar_phases, set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.plan_tools import PlanTools


class TestSunNotices(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        gen = self.sched.config.general
        self.phases = get_solar_phases(gen.latitude, gen.longitude, DAY, gen.timezone)
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)), scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=MockInferenceEngine(),
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY

    def test_only_when_switched_on_once_a_day_whatever_the_level(self):
        sunset = self.phases["sunset"]
        self.svc.sun(sunset + timedelta(minutes=1))
        self.assertEqual(self.sent, [])  # off by default
        plan, pending = self.sched.build_day_plan(DAY)
        set_debug_time(at("12:00"))
        out = PlanTools(plan, pending, self.sched).execute("set_sun_notices", {"sunset": True})
        self.assertEqual(out, "You'll get a message at sunset every day.")
        self.sched.config.assistant.message_level = "off"  # asked for: sent anyway
        self.svc.sun(sunset - timedelta(minutes=1))
        self.svc.sun(sunset + timedelta(minutes=1))
        self.svc.sun(sunset + timedelta(minutes=2))
        self.assertEqual(self.sent, [("sun", "🌇 Sunset.")])
        self.svc.sun(self.phases["sunrise"] + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)  # sunrise wasn't switched on


if __name__ == "__main__":
    unittest.main()
