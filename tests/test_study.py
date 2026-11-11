"""The study loop's own machinery (tests/study.py): a written question first, a person whose state carries
through the day, frictions with their words and what the app did, a catalogue compared across runs, and a
one-page report.

No model: a scripted person and reviewer, and the mock engine for the app.
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests.day_sim import DayScript
from tests.study import (PHONE_CUT, Friction, Person, Study, catalogue, compare, live_study_day, phone_view,
                         report, run_study)
from thirties_core.astronomy import set_debug_time
from thirties_core.config import ThirtiesConfig
from thirties_core.inference import MockInferenceEngine

SCRIPT = DayScript(
    id="t", day=date(2026, 10, 10), wake="09:00", bed="22:00", persona="Tester", situation="A Saturday.",
    life=["10:00 Walk until 10:30."], truth=[], setup=[["add_event", {"title": "Walk", "start": "10:00 AM",
                                                                      "duration": "30 min"}]])


def scripted(reactions):
    reactions = list(reactions)
    prompts = []

    def actor(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(reactions.pop(0)) if reactions else json.dumps({"felt": {}, "move": {"do": "sleep"}})
    actor.prompts = prompts
    return actor


def felt(p=7, t=6, **extra):
    return {"understood": "yes", "did_what_i_asked": "yes", "energy": 7, "patience": p, "trust": t, **extra}


class TestStudyRules(unittest.TestCase):
    def test_no_question_no_run(self):
        s = Study(id="x", question="", change="", scenarios=[], budget={})
        problems = " ".join(s.problems())
        for word in ("question", "change", "days", "budget"):
            self.assertIn(word, problems)
        ok = Study(id="x", question="Does it work?", change="A change", scenarios=[{"script": "t"}],
                   budget={"days": 1, "calls": 10})
        self.assertEqual(ok.problems(), [])

    def test_the_committed_studies_are_runnable(self):
        for path in Path("tests/studies").glob("*.json"):
            self.assertEqual(Study.load(path).problems(), [], path)

    def test_a_phone_shows_formatting_not_markup_and_cuts_long_messages(self):
        self.assertEqual(phone_view("**Lunch** at 1"), "Lunch at 1")
        long = phone_view("word " * 400)
        self.assertTrue(long.endswith("[Read more]"))
        self.assertLessEqual(len(long), PHONE_CUT + 20)


class TestLivingADay(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_debug_time, None)
        patcher = patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_day(self, reactions):
        from tests.day_sim import start_day
        run = start_day(SCRIPT, "frontier", [], [], MockInferenceEngine())
        actor = scripted(reactions)
        return live_study_day(run, actor, Person()), actor

    def test_state_carries_and_a_drop_is_a_friction_with_words_and_actions(self):
        day, actor = self.run_day([
            {"felt": felt(), "move": {"do": "message", "message": "morning! walk at 10"}},
            {"felt": felt(p=4, t=3, did_what_i_asked="no", annoyance="it ignored my walk", why="no walk on it"),
             "move": {"do": "message", "message": "I SAID walk at 10"}},
            {"felt": felt(p=4, t=3), "move": {"do": "sleep"}},
        ])
        self.assertEqual(day.person.state["patience"], 4)
        self.assertIn("patience 4", actor.prompts[2])  # the state the person carries into the next move
        self.assertEqual(len(day.frictions), 1)
        f = day.frictions[0]
        self.assertEqual((f.label, f.quote), ("it ignored my walk", "I SAID walk at 10"))
        self.assertEqual(f.cause, "no walk on it")

    def test_the_person_can_open_the_agenda_and_tick_there(self):
        day, actor = self.run_day([
            {"felt": felt(), "move": {"do": "open_app"}},
            {"felt": felt(), "move": {"do": "tick", "tick": "Walk"}},
            {"felt": felt(), "move": {"do": "sleep"}},
        ])
        self.assertIn("Agenda, open on your screen", actor.prompts[1])
        self.assertIn("Walk", actor.prompts[1])
        self.assertTrue(any("(in the app) ticked Walk" in x for _, w, x in day.run.log if w == "user"))

    def test_giving_up_ignores_the_app_for_a_while(self):
        day, _ = self.run_day([
            {"felt": felt(p=1, t=1, annoyance="too many pings"), "move": {"do": "give_up", "wait_minutes": 180}},
            {"felt": felt(p=2, t=1), "move": {"do": "sleep"}},
        ])
        self.assertEqual(len(day.frictions), 1)  # giving up is always a friction


class TestCatalogueAndReport(unittest.TestCase):
    def frictions(self):
        return [Friction("a", "09:00", "two morning messages", "why twice??", ["rollover_proposal {}"], "", "dup"),
                Friction("b", "10:00", "sent the morning twice", "again?", [], "", "dup"),
                Friction("b", "13:00", "lunch moved", "I didn't ask", ["move_event {}"], "", "moved")]

    def test_frictions_are_grouped_into_kinds_counted_by_days(self):
        llm = lambda _p: json.dumps([{"kind": "morning double message", "ids": [0, 1], "likely_cause": "x"},
                                     {"kind": "unasked moves", "ids": [2]}])
        groups = catalogue(self.frictions(), llm, [])
        self.assertEqual((groups[0]["kind"], groups[0]["days"], groups[0]["count"]),
                         ("morning double message", ["a", "b"], 2))

    def test_runs_are_compared_by_the_catalogue(self):
        before = [{"kind": "morning double message", "days": ["a", "b", "c", "d"]}]
        now = [{"kind": "unasked moves", "days": ["a"]}]
        self.assertEqual(compare(now, before, 8, 8), ["morning double message: 4 of 8 days → 0 of 8",
                                                      "unasked moves: 0 of 8 days → 1 of 8"])

    def test_a_study_runs_end_to_end_and_writes_its_one_page_report(self):
        study = Study(id="s", question="Does the walk get on the plan?", change="nothing, a smoke test",
                      scenarios=[{"script": "t", "beats": ["10:30 You get back home."]}], budget={"days": 1, "calls": 20},
                      brain="frontier")
        actor = scripted([{"felt": felt(p=3, annoyance="ignored me"), "move": {"do": "message", "message": "walk?"}},
                          {"felt": felt(p=3), "move": {"do": "sleep"}}])
        reviewer = lambda p: json.dumps([{"kind": "ignored requests", "ids": [0]}]) if "Group these" in p else \
            json.dumps({"verdict": "thin", "believable": "flat", "actor_changes": ["more interruptions"]})
        with tempfile.TemporaryDirectory() as out, \
                patch("tests.study.scripts_for", return_value=[DayScript(**{**SCRIPT.__dict__,
                                                                            "life": SCRIPT.life + ["10:30 You get back home."]})]), \
                patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig):
            result = run_study(study, actor, reviewer, MockInferenceEngine(), out_dir=Path(out))
            saved = Path(result["dir"])
            self.assertTrue((saved / "report.md").exists())
            again = run_study(study, scripted([{"felt": felt(), "move": {"do": "sleep"}}]),
                              lambda p: "[]" if "Group these" in p else "{}", MockInferenceEngine(), out_dir=Path(out))
        set_debug_time(None)
        text = result["report"]
        self.assertTrue(text.startswith("# Does the walk get on the plan?"))
        for section in ("**Change tested:**", "## What it found", "## Since the last run", "## Testers", "## Next"):
            self.assertIn(section, text)
        self.assertIn("**ignored requests**: 1 of 1 days", text)
        self.assertIn("Verdict: thin", text)
        self.assertIn("ignored requests: 1 of 1 days → 0 of 1", again["report"])  # compared with the last run

    def test_a_run_stops_at_its_end_time_and_resumes_with_the_days_it_kept(self):
        from datetime import datetime, timedelta
        study = Study(id="s", question="Q?", change="c", scenarios=[{"script": "t"}, {"script": "u"}],
                      budget={"days": 2, "calls": 50}, brain="frontier")
        u = DayScript(**{**SCRIPT.__dict__, "id": "u"})
        quiet = lambda p: "[]" if "Group these" in p else "{}"
        with tempfile.TemporaryDirectory() as out, \
                patch("tests.study.scripts_for", return_value=[SCRIPT, u]), \
                patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig):
            first = run_study(study, scripted([{"felt": felt(), "move": {"do": "sleep"}}]), quiet,
                              MockInferenceEngine(), out_dir=Path(out),
                              until=datetime.now().astimezone() + timedelta(minutes=10))
            self.assertEqual((first["days"], first["stopped"]), (0, "time window"))
            with patch("tests.study.scripts_for", return_value=[SCRIPT]):
                part = run_study(study, scripted([{"felt": felt(), "move": {"do": "sleep"}}]), quiet,
                                 MockInferenceEngine(), out_dir=Path(out))
            whole = run_study(study, scripted([{"felt": felt(), "move": {"do": "sleep"}}]), quiet,
                              MockInferenceEngine(), out_dir=Path(out), resume=Path(part["dir"]))
        set_debug_time(None)
        self.assertEqual([d["script"] for d in whole["per_day"]], ["t", "u"])  # t kept, only u run again

    def test_report_with_nothing_found_says_so(self):
        study = Study(id="s", question="Q?", change="c", scenarios=[{"script": "t"}], budget={"days": 1, "calls": 5})
        self.assertIn("No frictions", report(study, [], [], [], {}, 0))


if __name__ == "__main__":
    unittest.main()
