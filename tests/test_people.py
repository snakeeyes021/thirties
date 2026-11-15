"""People to keep in touch with: last seen, texted, called, quality time; a nudge when it's been a while.
Invented people throughout."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import AssistantTools, Services
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.people import People, background_duty, kind_of, parse_every
from thirties_core.plan_tools import NOT_DONE, PlanTools


class WordsTest(unittest.TestCase):
    def test_kinds_from_the_users_words(self):
        self.assertEqual(kind_of("texted"), "texted")
        self.assertEqual(kind_of("had coffee"), "seen")
        self.assertEqual(kind_of("a long phone call"), "called")
        self.assertEqual(kind_of("quality time"), "quality")
        self.assertIsNone(kind_of("thought about"))

    def test_how_often(self):
        self.assertEqual(parse_every("every 2 weeks"), 14)
        self.assertEqual(parse_every("monthly"), 30)
        self.assertEqual(parse_every("every 10 days"), 10)
        self.assertIsNone(parse_every("now and then"))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("12:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.pt = PlanTools(plan, pending, self.sched)
        self.pt.assistant = AssistantTools(self.pt, Services.for_db(self.sched.state_db))
        self.pt.services = self.pt.assistant.s

    def test_logging_starts_keeping_a_person_and_the_overview_says_when(self):
        out = self.pt.execute("log_contact", {"person": "Robin Hale", "how": "had coffee", "when": "Oct 3"})
        self.assertIn("Robin Hale is now among the people you keep in touch with", out)
        self.pt.execute("log_contact", {"person": "Robin", "how": "texted", "when": "yesterday"})
        self.pt.execute("log_contact", {"person": "robin hale", "how": "called"})
        self.assertEqual(len(People(self.sched.state_db).all()), 1)  # one person, found by first name and case
        out = self.pt.execute("people_overview", {"person": "Robin"})
        self.assertEqual(out, "Robin Hale: seen in person 12 days ago, texted yesterday, on the phone today")

    def test_a_future_day_or_unknown_way_changes_nothing(self):
        self.assertTrue(self.pt.execute("log_contact", {"person": "Robin", "how": "texted", "when": "tomorrow"})
                        .startswith(NOT_DONE))
        self.assertTrue(self.pt.execute("log_contact", {"person": "Robin", "how": "waved"}).startswith(NOT_DONE))
        self.assertEqual(People(self.sched.state_db).all(), [])

    def test_two_people_with_the_name_asks_which(self):
        store = People(self.sched.state_db)
        store.add("Sam Ortiz"), store.add("Sam Bell")
        out = self.pt.execute("log_contact", {"person": "Sam", "how": "texted"})
        self.assertTrue(out.startswith(NOT_DONE))
        self.assertIn("Sam Ortiz", out)

    def test_who_is_due_and_stopping(self):
        self.pt.execute("log_contact", {"person": "Robin", "how": "seen", "when": "Sep 1"})
        self.pt.execute("log_contact", {"person": "Ines", "how": "texted", "when": "Oct 10"})
        self.pt.execute("keep_in_touch", {"person": "Ines", "every": "every 3 days"})
        due = [p.name for p, _ in People(self.sched.state_db).due(DAY)]
        self.assertEqual(due, ["Robin", "Ines"])  # longest overdue first (Robin: 44 days, a month wanted)
        self.assertIn("Robin: seen in person", self.pt.execute("people_overview", {}))
        self.pt.execute("keep_in_touch", {"person": "Robin", "stop": True})
        self.assertEqual([p.name for p, _ in People(self.sched.state_db).due(DAY)], ["Ines"])
        self.assertIn("not tracked", self.pt.execute("people_overview", {"person": "Robin"}))  # history stays

    def test_in_the_prompt_only_when_someone_is_due(self):
        from thirties_core.extras import prompt_section
        self.assertNotIn("PEOPLE DUE", prompt_section(self.pt))
        self.pt.execute("log_contact", {"person": "Robin", "how": "seen", "when": "Sep 1"})
        self.assertIn("PEOPLE DUE A MESSAGE", prompt_section(self.pt))
        self.assertIn("- Robin: seen in person 6 weeks ago", prompt_section(self.pt))

    def test_nothing_goes_to_joplin(self):
        joplin = MagicMock()
        self.pt.assistant.s.joplin = joplin
        self.pt.execute("log_contact", {"person": "Robin", "how": "seen"})
        self.assertEqual(joplin.mock_calls, [])


class NudgeTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.desktop = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        store = People(self.sched.state_db)
        robin = store.add("Robin")
        store.log(robin, "texted", DAY - timedelta(days=40))
        ines = store.add("Ines")
        store.log(ines, "seen", DAY - timedelta(days=3))

    def test_once_a_day_after_mid_morning_and_not_again_for_a_week(self):
        self.assertIsNone(background_duty(self.svc, at("08:00")))  # too early
        text = background_duty(self.svc, at("17:00"))
        self.assertEqual(text, "🤝 It's been a while since you were in touch with Robin (texted 6 weeks ago). "
                               "Maybe send a text?")
        self.assertEqual([k for k, _ in self.desktop], ["people"])
        self.assertIsNone(background_duty(self.svc, at("19:00")))  # once a day
        self.assertIsNone(background_duty(self.svc, at("17:00", 3)))  # not about Robin again within a week
        self.assertIsNotNone(background_duty(self.svc, at("17:00", 8)))
        self.assertNotIn("- ", text)  # plain lines

    def test_not_while_asleep_or_when_turned_off(self):
        self.assertIsNone(background_duty(self.svc, at("23:30")))  # asleep
        from thirties_core.settings import Settings
        Settings(self.sched.state_db).set("people.nudges", False)
        self.assertIsNone(background_duty(self.svc, at("17:00")))


if __name__ == "__main__":
    unittest.main()
