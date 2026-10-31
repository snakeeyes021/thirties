"""The day-long sim's own machinery: the clock runs, the app's messages reach the user, the day is measured.

No model: a scripted user and the mock engine stand in for Gemini.
"""

import json
import unittest
from datetime import date, datetime
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
        asked = [(t, x) for t, w, x in run.log if w == "app" and "For the record" in x]
        self.assertEqual(asked, [])  # waking isn't asked (10-31), and a day off has no work to ask about
        morning = [(t, x) for t, w, x in run.log if w == "app" and x.startswith("☀️ Good morning")]
        self.assertIn("Already planned for today", morning[0][1])  # what was planned ahead is said (O2)
        app = [(t, x) for t, w, x in run.log if w == "app" and "For the record" not in x
               and not x.startswith("☀️ Good morning")]
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

    def test_only_minute_times_thirties_picked_are_counted(self):
        script = DayScript(id="t2", day=date(2026, 10, 10), wake="09:00", bed="22:00", persona="Tester",
                           situation="A Saturday.", life=["09:00 Wake."], truth=[], work=["08:45", "16:00"],
                           events=[{"summary": "Dentist", "start": "14:15", "end": "15:00"}],
                           notes=[{"title": "Cats", "role": "routine", "body": "- [ ] Dinner at 5:15"}])
        run = start_day(script, "frontier", [], [], MockInferenceEngine())
        run.log += [("09:47", "user", "I'm leaving at 12:15, don't book me for 12:00"),
                    ("09:47", "assistant", "Got it: leaving at 12:15. The dentist is at 2:15 PM, cats' dinner at 5:15, "
                                           "work from 8:45."),
                    ("09:50", "user", "fed the cats"), ("09:50", "assistant", "Marked them fed at 9:50."),
                    ("09:55", "user", "what's next?"), ("09:55", "assistant", "Laundry at 10:20.")]
        facts = measure(run)
        self.assertEqual(facts["minute_times_shown"], 1)  # 10:20 is the only time Thirties picked itself
        self.assertEqual(facts["minute_time_examples"], ["[09:55] 10:20"])
        self.assertEqual(facts["minute_times_given"], 5)  # 12:15, 2:15 (calendar), 5:15 (lists), 8:45 (work), 9:50

    def test_someone_else_with_their_own_lists(self):
        from tests.day_sim import validate
        nurse = DayScript(id="nurse", day=date(2026, 10, 10), wake="07:00", bed="22:00", persona="Pat", situation="Off.",
                          life=["07:00 Wake."], truth=[{"item": "Laundry", "start": "09:00", "end": "10:00"}],
                          notes=[{"notebook": "Home", "title": "Chores", "role": "list",
                                  "body": "- [ ] Laundry\n- [ ] Pay the water bill\n## Someday\n- [ ] Paint the hall\n"},
                                 {"notebook": "Home", "title": "Today", "role": "today", "body": ""}])
        run = start_day(nurse, "frontier", [], [], MockInferenceEngine())
        tasks = {t.title: t for t in run.manager.tools.services.tasks}
        self.assertEqual(set(tasks), {"Laundry", "Pay the water bill", "Paint the hall"})
        self.assertTrue(tasks["Paint the hall"].someday)
        self.assertEqual(tasks["Laundry"].list_role, "list")
        self.assertIsNotNone(run.manager.tools.assistant._today_note())  # their Today note works too
        self.assertEqual(validate({"id": "x", "date": "2026-13-01", "wake": "7:00", "bed": "22:00", "persona": "p",
                                   "situation": "s", "life": ["morning: wake"], "truth": [{"item": "a"}]}),
                         ["date '2026-13-01' isn't YYYY-MM-DD", "wake '7:00' isn't HH:MM",
                          "life beat without a time first: 'morning: wake'"])

    def test_joplin_writes_are_scored(self):
        nurse = DayScript(id="nurse", day=date(2026, 10, 10), wake="07:00", bed="22:00", persona="Pat", situation="Off.",
                          life=["07:00 Wake."], truth=[{"item": "Laundry", "start": "09:00", "end": "10:00"}],
                          notes=[{"notebook": "Home", "title": "Chores", "role": "list", "body": "- [ ] Laundry\n"},
                                 {"notebook": "Home", "title": "Morning", "role": "routine", "body": "- [ ] Coffee\n"},
                                 {"notebook": "Home", "title": "Today", "role": "today", "body": ""}])
        run = start_day(nurse, "frontier", [], [], MockInferenceEngine())
        set_debug_time(datetime(2026, 10, 10, 8, 30, tzinfo=run.manager.day_plan.day_start.tzinfo))  # after sunrise
        tasks = {t.title: t for t in run.manager.tools.services.tasks}
        run.manager.execute_tool("add_event", {"title": "Laundry", "start": "9:00 AM", "duration": "1 hour",
                                               "task_id": tasks["Laundry"].id})
        run.manager.execute_tool("put_routine", {"routine": "Morning", "start": "8:00 AM"})
        laundry = next(s for s in run.manager.day_plan.spans if s.title == "Laundry")
        run.manager.execute_tool("log_outcome", {"event": laundry.id, "status": "done"})
        j = measure(run)["joplin"]
        self.assertEqual((j["into_routine"], j["plan_not_on_today"], j["done_but_open"], j["today_duplicates"]),
                         ([], [], [], []))
        self.assertGreaterEqual(j["writes"], 4)  # Laundry and Morning on Today, the steps, Laundry ticked twice

    def test_committed_scripts_load(self):
        scripts = find_scripts([Path(__file__).parent / "day_scripts"])
        self.assertTrue(scripts)
        for s in scripts:
            self.assertTrue(s.life and s.truth, s.id)
            s.scenario()


if __name__ == "__main__":
    unittest.main()


class TestMessageLevel(unittest.TestCase):

    def test_each_person_picks_their_level_once(self):
        import dataclasses
        import tempfile
        import tests.day_sim as ds
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds, "LEVELS_FILE", Path(tmp) / "levels.json"):
            asked = []

            def sim(prompt):
                asked.append(prompt)
                return "High, I like to hear about everything."
            self.assertEqual(ds.message_level(SCRIPT, sim), "heavy")
            self.assertEqual(ds.message_level(SCRIPT, sim), "heavy")  # kept: paired runs use the same level
            self.assertEqual(len(asked), 1)
            self.assertEqual(ds.message_level(dataclasses.replace(SCRIPT, message_level="light"), sim), "light")
