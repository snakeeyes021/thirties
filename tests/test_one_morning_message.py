"""One morning message: a new week's plan is made while the day is built and said in the morning reply, in one
line, never as a message of its own a minute later.

Invented items for someone who throws pots on a late shift at a studio and paddles a canoe on days off. Monday
Oct 19 is a new week (DAY is Thu Oct 15)."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler, mark_up
from tests.test_conversation import reply
from tests.test_horizons import item
from thirties_core.astronomy import set_debug_time
from thirties_core.horizons import HorizonCatalog, HorizonPlans, period_of

MONDAY = 4  # days after DAY


class MorningCase(unittest.TestCase):

    def setUp(self):
        self.when = at("09:30", MONDAY)
        set_debug_time(self.when)
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        self.tasks = [item("seat", "Patch the canoe seat"), item("glaze", "Mix a new celadon glaze", "n_studio",
                                                                   "Studio")]
        for tid in ("seat", "glaze"):
            HorizonCatalog(self.sched.state_db).set_item(tid, "week")
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = list(self.tasks), []
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        self.engine = MockInferenceEngine()
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: self.when.date()
        self.svc.bridge = MagicMock()
        self.m = self.svc.manager()

    def week_titles(self):
        week = period_of("week", self.when.date())
        return {r["title"] for r in HorizonPlans(self.sched.state_db).in_period(week)}


class TestTheWeekIsInTheMorning(MorningCase):

    def test_the_week_line_is_in_the_morning_reply(self):
        self.engine.set_next_response(reply("☀️ Good morning! The studio shift is later; the morning is open."))
        out = self.m.send_user_message("morning, I'm up")
        self.assertIn("This week: ", out)
        self.assertIn("Patch the canoe seat", out)
        self.assertIn("Mix a new celadon glaze", out)
        self.assertNotIn("Want this week", out)
        self.assertEqual({"Patch the canoe seat", "Mix a new celadon glaze"}, self.week_titles())  # applied, no yes
        self.assertEqual(out.count("This week: "), 1)

    def test_not_said_twice_when_the_reply_already_names_it(self):
        for _ in range(3):  # the same draft, however often it's asked for
            self.engine.set_next_response(reply("☀️ Good morning! This week holds Patch the canoe seat and Mix a new "
                                                "celadon glaze."))
        out = self.m.send_user_message("morning")
        self.assertIn("This week holds", out)
        self.assertNotIn("This week: ", out)

    def test_no_separate_message_after_the_morning(self):
        self.engine.set_next_response(reply("☀️ Good morning! Nothing fixed until the shift."))
        self.m.send_user_message("morning")
        mark_up(self.sched.state_db, self.when)
        for minute in ("09:31", "09:35", "10:00", "13:00"):
            self.svc.tick(at(minute, MONDAY))
        self.assertFalse([k for k, _ in self.sent if k == "horizon"])
        self.assertFalse(hasattr(self.svc, "horizon_session"))  # there's no duty that sends one


if __name__ == "__main__":
    unittest.main()
