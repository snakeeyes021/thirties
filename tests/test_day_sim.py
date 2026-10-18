"""The day-long sim's own machinery: the clock runs, the app's messages reach the user, the day is measured.

No model: a scripted user and the mock engine stand in for Gemini.
"""

import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests.day_sim import DayScript, find_scripts, live_day, measure, start_day
from thirties_core.astronomy import set_debug_time
from thirties_core.config import ThirtiesConfig
from thirties_core.inference import MockInferenceEngine

SCRIPT = DayScript(
    id="t", day=date(2026, 10, 10), wake="09:00", bed="22:00", persona="Tester", situation="A Saturday.",
    life=["10:00 Walk until 10:30."], truth=[{"item": "Walk", "match": "walk", "start": "10:05", "end": "10:35"}],
    setup=[["add_event", {"title": "Walk", "start": "10:00 AM", "duration": "30 min"}]])


def scripted(moves):
    moves = list(moves)
    prompts = []

    def sim(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(moves.pop(0)) if moves else json.dumps({"wait_minutes": 0, "message": None,
                                                                  "end_of_day": True})
    sim.prompts = prompts
    return sim


class TestDaySim(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)
        patcher = patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_clock_runs_and_the_apps_messages_interrupt_the_wait(self):
        run = start_day(SCRIPT, "frontier", [], [], MockInferenceEngine())
        run.scheduler.config.assistant.message_level = "heavy"  # a message per item, to test the mechanics
        sim = scripted([
            {"wait_minutes": 0, "message": "morning!"},
            {"wait_minutes": 180, "message": "back from my walk"},  # interrupted at 10:00 by the start message
            {"wait_minutes": 120, "message": None},                  # interrupted by the check-in
            {"wait_minutes": 0, "message": None, "end_of_day": True},
        ])
        live_day(run, sim)
        app = [(t, x) for t, w, x in run.log if w == "app"]
        self.assertEqual(app[0][0], "10:00")
        self.assertIn("Walk", app[0][1])
        self.assertIn("Walk", app[1][1])  # the check-in, after the walk's thirty ended
        self.assertGreaterEqual(app[1][0], "10:30")
        self.assertIn("[10:00] App:", sim.prompts[2])  # the user saw the start message before deciding again
        self.assertNotIn("back from my walk", [x for _, w, x in run.log if w == "user"])  # dropped: interrupted
        self.assertEqual(run.end_reason, "end of day")

    def test_measuring_a_day(self):
        run = start_day(SCRIPT, "frontier", [], [], MockInferenceEngine())
        walk = next(s for s in run.manager.day_plan.spans if s.title == "Walk")
        run.manager.execute_tool("log_outcome", {"event": walk.id, "status": "done", "started": "10:15 AM",
                                                 "finished": "10:35 AM"})
        run.log += [("10:40", "user", "done"), ("10:40", "assistant", "Logged it, 10:15 to 10:35. CALL x {}"),
                    ("10:41", "user", "exact minutes please"), ("10:41", "assistant", "10:15 to 10:35."),
                    ("11:00", "app", "🔔 11:00: Lunch"), ("11:30", "app", "🔔 11:00: Lunch")]
        facts = measure(run)
        self.assertEqual(facts["minute_times_shown"], 2)  # the first reply only: the second was asked for
        self.assertEqual(len(facts["internal_text_in_replies"]), 1)
        self.assertEqual(facts["app_messages_repeated"], 1)
        self.assertEqual(facts["done_items_recorded"], "1/1")
        self.assertEqual(facts["mean_time_error_min"], 5.0)  # started 10:15 against 10:05; ended right on 10:35

    def test_committed_scripts_load(self):
        scripts = find_scripts([Path(__file__).parent / "day_scripts"])
        self.assertTrue(scripts)
        for s in scripts:
            self.assertTrue(s.life and s.truth, s.id)
            s.scenario()


if __name__ == "__main__":
    unittest.main()
