"""House rules (tests/house_rules.py): how Thirties works by design is told to every simulated person and judge, and
a complaint only against it is dropped after judging and counted, never scored as a fault.

No model: scripted people and judges, the mock engine for the app. Invented people, items and times.
"""

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from tests import house_rules
from tests.day_sim import DAY_JUDGE_PROMPT, DayScript, judge_day, live_day, start_day
from tests.house_rules import (HOUSE_RULES, JUDGE_HOUSE_SECTION, PERSON_HOUSE_NOTE, by_house_rules,
                               drop_house_rule_text, house_rule_of, with_house_rules)
from tests.sim_eval import JUDGE_PROMPT as SHORT_JUDGE_PROMPT
from tests.sim_eval import drop_house_rule_issues
from tests.study import Person, live_study_day
from thirties_core.astronomy import set_debug_time
from thirties_core.config import ThirtiesConfig
from thirties_core.inference import MockInferenceEngine

SCRIPT = DayScript(
    id="t", day=date(2026, 10, 10), wake="09:00", bed="22:00", persona="A retired luthier", situation="A Saturday.",
    life=["10:00 Glazing until 10:30."], truth=[], setup=[["add_event", {"title": "Glazing", "start": "10:00 AM",
                                                                         "duration": "30 min"}]])
UNDER_WAY = "Glazing is under way now (started at 10:20 AM in the 10:00 block)."


def rule(evidence, quote=""):
    return house_rule_of({"kind": "request_ignored", "cause": "app", "quote": quote, "evidence": evidence})


class TestTheRules(unittest.TestCase):
    def test_each_rule_is_named_and_told_to_both(self):
        names = [r.name for r in HOUSE_RULES]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"blocks_not_clocks", "skip_means_not_today", "done_on_their_word", "routines_on_their_own",
                         "changes_are_live"} <= set(names))
        self.assertNotIn("late_is_recorded", names)
        for r in HOUSE_RULES:
            self.assertIn(r.person, PERSON_HOUSE_NOTE)
            self.assertIn(r.judge, JUDGE_HOUSE_SECTION)
        self.assertTrue(with_house_rules("A retired luthier").startswith("A retired luthier\n"))
        self.assertIn("costs it no trust or patience", with_house_rules("x"))

    def test_wanting_minutes_is_dropped(self):
        self.assertEqual(rule("Person asked to fix the glazing line showing 10:00; the app left it and the person "
                              "asked again at 11:07.", UNDER_WAY), "blocks_not_clocks")
        self.assertEqual(rule("The reply gave no exact times for the porch sweep."), "blocks_not_clocks")
        claim = {"claim": "Repotting moved to start right now at 4:40", "true": False,
                 "why": "The plan view after the turn still shows 4:30 PM – Repotting."}
        self.assertEqual(house_rule_of(claim), "blocks_not_clocks")

    def test_a_given_time_another_block_or_another_fault_stays(self):
        self.assertIsNone(rule("The person asked for the kayak trip at 11:15 and it stayed in the 11:00 block."))
        self.assertIsNone(rule("The person said it was 3:30 and leaving now; the plan lists it at 2:30 (started at "
                               "2:36).", "Trip to the framer started at 2:36 PM"))
        self.assertIsNone(rule("The calendar's 2:45 dentist was shown without its exact time."))
        self.assertIsNone(rule("The reply shows the 9:00 block for the glazing started at 10:20."))
        self.assertIsNone(rule("Line showing 10:00 for the glazing begun at 10:20, and it overlaps the call.",
                               UNDER_WAY))

    def test_skip_and_done(self):
        self.assertEqual(rule("The person skipped the bike tune-up and it wasn't put on tomorrow's plan."),
                         "skip_means_not_today")
        self.assertIsNone(rule("The person skipped the bike tune-up and asked to move it to tomorrow; it stayed "
                               "off tomorrow's plan."))
        self.assertIsNone(rule("The person skipped the bike tune-up and the app moved it to tomorrow unasked."))
        self.assertEqual(rule("The kiln was started at 2 but not marked as done."), "done_on_their_word")
        self.assertIsNone(rule("The person said the kiln was done, but it was only started and never marked done."))
        self.assertIsNone(rule("The app assumed the shower was done and marked it."))

    def test_rules_told_only_drop_nothing(self):
        self.assertIsNone(rule("At 9:10 it added a Daily Routine task list without being asked."))
        self.assertIsNone(rule("Told the call ran long, it left the rest of the plan where it was."))

    def test_counted_by_rule(self):
        kept, dropped = by_house_rules([{"evidence": "The reply gave no exact times."},
                                        {"evidence": "The porch sweep was moved though nobody asked."}])
        self.assertEqual((len(kept), dropped), (1, {"blocks_not_clocks": 1}))
        self.assertEqual(drop_house_rule_text(["Gave no exact times for the sweep.", "Nagged twice in a meeting.", 3]),
                         (["Nagged twice in a meeting.", 3], 1))


class TestTheJudges(unittest.TestCase):
    def test_both_judges_are_told_the_house_rules(self):
        for prompt in (DAY_JUDGE_PROMPT, SHORT_JUDGE_PROMPT):
            self.assertIn(JUDGE_HOUSE_SECTION, prompt)
            self.assertNotIn("unless exact minutes were asked for", prompt)

    def test_the_day_judges_house_rule_issues_are_dropped_and_counted(self):
        run = SimpleNamespace(script=SCRIPT, facts={}, transcript_text=lambda for_user=False: "User: hi",
                              record_text=lambda: "(nothing)", judge={}, rating={})
        asked = []

        def judge(prompt):
            asked.append(prompt)
            return json.dumps({"clarity": 4, "issues": ["The reply gave no exact times for the glazing.",
                                                        "Nagged twice during the call."],
                               "worst_moments": ["Shows only the block for the glazing."]})

        def person(prompt):
            asked.append(prompt)
            return json.dumps({"rating": 7})
        judge_day(run, judge, person)
        self.assertIn("House rules", asked[0])
        self.assertIn(PERSON_HOUSE_NOTE, asked[1])  # the person's own rating of the day
        self.assertEqual(run.judge["issues"], ["Nagged twice during the call."])
        self.assertEqual((run.judge["worst_moments"], run.judge["dropped_house_rule"]), ([], 2))

    def test_a_short_sims_verdict_too(self):
        verdict = {"issues": ["Skipped the gym, and it wasn't put on tomorrow."]}
        drop_house_rule_issues(verdict)
        self.assertEqual((verdict["issues"], verdict["dropped_house_rule"]), ([], 1))


class TestThePeople(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_debug_time, None)
        patcher = patch("tests.sim_eval.load_config", side_effect=ThirtiesConfig)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_day_sims_person_is_told(self):
        run = start_day(SCRIPT, "frontier", [], [], MockInferenceEngine())
        prompts = []

        def sim(prompt):
            prompts.append(prompt)
            return json.dumps({"wait_minutes": 0, "message": None, "end_of_day": True})
        live_day(run, sim)
        self.assertIn(PERSON_HOUSE_NOTE, prompts[0])

    def test_a_studys_person_is_told_and_a_complaint_against_the_design_is_no_friction(self):
        run = start_day(SCRIPT, "frontier", [], [], MockInferenceEngine())
        reactions = [
            {"felt": {"patience": 4, "trust": 4, "annoyance": "line shows 10:00 not my start",
                      "why": "It still shows the 10:00 block though the glazing started at 10:20."},
             "move": {"do": "wait", "wait_minutes": 20}},
            {"felt": {"patience": 2, "trust": 2, "annoyance": "it ignored the glazing", "why": "no glazing on it"},
             "move": {"do": "sleep"}}]
        prompts = []

        def actor(prompt):
            prompts.append(prompt)
            return json.dumps(reactions.pop(0)) if reactions else json.dumps({"felt": {}, "move": {"do": "sleep"}})
        day = live_study_day(run, actor, Person())
        self.assertIn(PERSON_HOUSE_NOTE, prompts[0])
        self.assertEqual([f.label for f in day.frictions], ["it ignored the glazing"])
        self.assertEqual(day.house_rule_drops, {"blocks_not_clocks": 1})


class TestTheHarnessCopy(unittest.TestCase):
    def test_the_rules_are_listed_in_one_place_here(self):
        self.assertTrue(callable(house_rules.house_rule_of))


if __name__ == "__main__":
    unittest.main()
