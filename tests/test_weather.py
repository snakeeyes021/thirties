"""Weather: facts from a forecast, 'bad for outdoors', and how both brains use it (with a fake forecast)."""

import json
import unittest
from unittest.mock import patch

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.weather import Weather


def forecast(rain_from=18, rain_to=21):
    """Hourly for the fixture day: mild and clear, rain from 6 to 9 PM."""
    times, temp, rain, code, wind = [], [], [], [], []
    for h in range(24):
        times.append(f"{DAY.isoformat()}T{h:02d}:00")
        temp.append(60 + h % 10)
        wet = rain_from <= h < rain_to
        rain.append(80 if wet else 5)
        code.append(63 if wet else 1)
        wind.append(5)
    return {"hourly": {"time": times, "temperature_2m": temp, "precipitation_probability": rain,
                       "weather_code": code, "wind_speed_10m": wind}}


class TestWeather(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("16:40"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.calls = []

        def fetch(url):
            self.calls.append(url)
            return forecast()
        self.weather = Weather(self.sched.state_db, 38.88, -77.1, "America/New_York", "fahrenheit", fetcher=fetch)

    def test_facts_and_bad_times(self):
        facts = self.weather.facts(at("16:40"), at("23:00"))
        self.assertTrue(facts[0].startswith("Around 4:40 PM: 66°F"), facts)
        self.assertIn("Poor for being outside 6:00 PM–9:00 PM: rain (80% chance of rain).", facts)
        self.assertEqual(self.weather.bad_for_outdoors(at("19:00"), at("19:30")), "rain around 7:00 PM")
        self.assertIsNone(self.weather.bad_for_outdoors(at("16:00"), at("17:00")))
        self.weather.hours()
        self.assertEqual(len(self.calls), 1)  # cached for an hour

    def test_offline_uses_the_last_forecast(self):
        self.weather.hours()
        def down(url):
            raise OSError("no network")
        stale = Weather(self.sched.state_db, 38.88, -77.1, "America/New_York", "celsius", fetcher=down)
        self.assertEqual(stale.hours(), [])  # a different request (units): nothing cached for it
        self.assertTrue(self.weather.hours())

    def test_outdoor_plans_in_rain_get_a_heads_up_and_gemma_avoids_suggesting_them(self):
        from tests.test_slm_brain import FakeRuntime, TASKS
        from thirties_core.brains.slm.brain import SlmBrain
        from thirties_core.conversation import ConversationManager
        from thirties_core.models import TaskItem
        plan, pending = self.sched.build_day_plan(DAY)
        tasks = list(TASKS) + [TaskItem(id="t_walk", source_notebook="1. Tasks", title="Go for a long walk",
                                        parent_note_title="Health", parent_note_id="n9", deferred_count=4)]
        m = ConversationManager(plan, tasks, pending, self.sched, active_notes=[])
        rt = FakeRuntime()
        m.brain = SlmBrain(m.tools, tasks=tasks, runtime=rt)
        with patch("thirties_core.weather.weather_for", return_value=self.weather):
            out = m.execute_tool("add_event", {"title": "Walk", "start": "7:00 PM", "duration": "30 min"})
            self.assertIn("Weather heads-up: rain around 7:00 PM.", out)
            rt.parse_replies.append(json.dumps({"actions": [{"type": "suggest", "topic": "evening"}]}))
            rt.pick_replies.append('{"picks": [1, 2, 3]}')
            reply = m.send_user_message("what should I do this evening?")
            self.assertNotIn("walk", reply.lower())
            rt.parse_replies.append(json.dumps({"actions": [{"type": "chat"}]}))
            self.assertIn("Poor for being outside", m.send_user_message("is it going to rain later?"))


if __name__ == "__main__":
    unittest.main()
