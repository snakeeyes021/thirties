"""The Frontier bar's machinery: the raw-backlog prompt, reading the plan back, and grading through the day sim's judge.

No model: scripted callables and the mock engine stand in for Gemini.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests.day_sim import DAY_JUDGE_PROMPT, DayScript
from tests.frontier_bar import (BAR_PROMPT, FALLBACK_OPENING, ask_engine, parse_plan, run_bar, run_script, split_reply,
                                write_report)
from thirties_core.astronomy import set_debug_time
from thirties_core.config import ThirtiesConfig
from thirties_core.inference import MockInferenceEngine

SCRIPT = DayScript(
    id="t", day=date(2026, 10, 10), wake="09:00", bed="22:00", persona="Tester", situation="A Saturday, no work.",
    life=["09:00 Ask for a plan: laundry and guitar."], truth=[{"item": "Laundry", "match": "laundry"}],
    events=[{"summary": "Maya's birthday dinner", "start": "19:00", "end": "21:30"}],
    setup=[["add_event", {"title": "Guitar", "start": "4:00 PM", "duration": "1 hour"}]],
    profile={"Preferences": "Slow mornings."},
    notes=[{"notebook": "Home", "title": "Chores", "role": "list", "body": "- [ ] Laundry\n- [ ] Pay the water bill\n"},
           {"notebook": "Home", "title": "Ideas", "body": "Paint the hall green, maybe."}])

PLAN_REPLY = ("Here's a gentle day: breakfast, laundry, then guitar later.\n\nPLAN\n"
              "09:30-10:00 | Breakfast\n10:00-11:30 | Laundry\n17:00-18:00 | Guitar\n19:00-21:30 | Maya's birthday dinner\n"
              "23:30-00:15 | Read")
JUDGE_REPLY = json.dumps({"requests": [{"request": "plan laundry", "met": 1}, {"request": "guitar", "met": 0.5}],
                          "accuracy": 4, "understanding": 4, "plan_respect": 5, "message_load": 5,
                          "record_keeping": 3, "clarity": 5, "tone": 5, "worst_moments": [], "issues": []})


def recorder(*replies):
    """A prompt -> text callable that answers in order and keeps what it was asked."""
    replies = list(replies)

    def ask(prompt: str) -> str:
        ask.prompts.append(prompt)
        return replies.pop(0) if replies else ""
    ask.prompts = []
    return ask


class TestFrontierBar(unittest.TestCase):

    def setUp(self):
        self.addCleanup(set_debug_time, None)
        patcher = patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_prompt_is_the_raw_backlog_and_the_situation_without_tools(self):
        planner, judge = recorder(PLAN_REPLY), recorder(JUDGE_REPLY)
        sim = recorder(json.dumps({"rating": 7}))
        run_bar(SCRIPT, [], [], "plan my day? laundry and guitar", planner, judge, sim, MockInferenceEngine())
        prompt = planner.prompts[0]
        self.assertIn("### Home / Chores\n- [ ] Laundry\n- [ ] Pay the water bill", prompt)  # notes as they are
        self.assertIn("Paint the hall green, maybe.", prompt)                                  # not just checklists
        self.assertIn('"plan my day? laundry and guitar"', prompt)
        self.assertIn("Maya's birthday dinner", prompt)
        self.assertIn("Guitar", prompt.split("Already planned today:")[1].split("Planned yesterday")[0])
        self.assertIn("Slow mornings.", prompt)
        self.assertIn("none (day off)", prompt)  # a script with no work
        for scaffolding in ("add_event", "tool", "thirty", "block", "task_id", "CALL"):
            self.assertNotIn(scaffolding, prompt)

    def test_reading_the_plan(self):
        shown, block = split_reply(PLAN_REPLY)
        self.assertEqual(shown, "Here's a gentle day: breakfast, laundry, then guitar later.")
        items = parse_plan(block, date(2026, 10, 10), "09:00")
        self.assertEqual([t for _, _, t in items], ["Breakfast", "Laundry", "Guitar", "Maya's birthday dinner", "Read"])
        read = items[-1]
        self.assertEqual((read[0].day, read[0].hour, read[1].day, read[1].hour, read[1].minute), (10, 23, 11, 0, 15))
        # twelve-hour times and dashes, without a PLAN line: the whole reply is read
        loose = parse_plan("- 9:30 AM – 10:15 AM: Coffee\n- 1:00 – 2:00 pm | Groceries\nnot a plan line",
                           date(2026, 10, 10), "09:00")
        self.assertEqual([(s.hour, s.minute, e.hour, e.minute, t) for s, e, t in loose],
                         [(9, 30, 10, 15, "Coffee"), (13, 0, 14, 0, "Groceries")])
        self.assertEqual(parse_plan("02:00-03:00 | Night owl", date(2026, 10, 10), "09:00")[0][0].day, 11)

    def test_the_plan_is_graded_by_the_day_sims_judge(self):
        planner, judge = recorder(PLAN_REPLY), recorder(JUDGE_REPLY)
        sim = recorder(json.dumps({"rating": 7, "annoyances": [], "liked": []}))
        run = run_bar(SCRIPT, [], [], "plan my day?", planner, judge, sim, MockInferenceEngine())
        # the judge got the day sim's own prompt, with the bar's reply and its plan as the record
        verdict_prompt = judge.prompts[0]
        self.assertTrue(verdict_prompt.startswith(DAY_JUDGE_PROMPT.split("{")[0]))
        self.assertIn("Assistant: Here's a gentle day", verdict_prompt)
        self.assertNotIn("PLAN\n", verdict_prompt.split("The final record")[0])  # the user never sees the block
        self.assertIn("Laundry", verdict_prompt.split("The final record")[1])
        self.assertEqual(run.judge["accuracy"], 4)
        self.assertEqual(run.rating["rating"], 7)
        record = run.record_text()
        self.assertEqual(record.count("Guitar"), 1)          # the planned guitar moved to the bar's time, not copied
        self.assertIn("5:00 PM – 6:00 PM: Guitar", record)
        self.assertEqual(record.count("Maya's birthday dinner"), 1)  # a calendar event isn't added again
        self.assertEqual(run.facts["plan_items"], 5)
        self.assertEqual(run.facts["plan_items_skipped"], ["Maya's birthday dinner"])
        self.assertEqual(run.facts["minute_times_shown"], 0)

    def test_paired_with_thirties_on_the_same_message_and_reported(self):
        sim = recorder(json.dumps({"wait_minutes": 0, "message": "plan my day pls"}),
                       json.dumps({"rating": 6}), json.dumps({"rating": 5}))
        planner, judge = recorder(PLAN_REPLY), recorder(JUDGE_REPLY, JUDGE_REPLY)
        engine = MockInferenceEngine([{"role": "assistant", "content": "Laundry at 10, guitar at 4.", "tool_calls": []}])
        results = run_script(SCRIPT, [], [], sim, judge, planner, engine)
        self.assertEqual([r["who"] for r in results], ["bar", "thirties"])
        self.assertTrue(all(r["opening"] == "plan my day pls" and not r["error"] for r in results))
        self.assertEqual(results[0]["requests_met"], 0.75)
        thirties_log = [x for _, w, x in results[1]["log"] if w == "assistant"]
        self.assertEqual(thirties_log, ["Laundry at 10, guitar at 4."])
        self.assertIn("plan my day pls", json.dumps(engine.call_history[0]))
        with tempfile.TemporaryDirectory() as tmp:
            out = write_report(results, Path(tmp), "unit")
            data = json.loads(out.read_text())
        self.assertTrue(out.name.endswith("_unit.json"))
        self.assertEqual(len(data["results"]), 2)
        self.assertTrue(data["summary"][0].startswith("bar: 1 scripts | requests met 0.75"))

    def test_a_silent_opening_falls_back_and_a_mock_engine_can_plan(self):
        sim = recorder(json.dumps({"wait_minutes": 30, "message": None}), json.dumps({"rating": 5}))
        planner = ask_engine(MockInferenceEngine([{"role": "assistant", "content": PLAN_REPLY}]))
        results = run_script(SCRIPT, [], [], sim, recorder(JUDGE_REPLY), planner, MockInferenceEngine(), pair=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["opening"], FALLBACK_OPENING)
        self.assertEqual(results[0]["facts"]["plan_items"], 5)

    def test_the_prompt_template_names_every_field(self):
        self.assertNotIn("tool", BAR_PROMPT.lower())
        self.assertIn("PLAN", BAR_PROMPT)


if __name__ == "__main__":
    unittest.main()
