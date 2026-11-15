"""The background service's extra duties: people and tickets duties run on every tick (once the
user is up), and one of them failing never breaks the tick. Invented items; DAY is Thu Oct 15."""

import unittest
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory


class HookCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("17:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.desktop = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.run_in_threads = False
        self.svc.sort_lists_enabled = False
        mark_up(self.sched.state_db, at("08:30"))
        self.sched.state_db.upsert_span(EventSpan("tx", "Sort the receipts", at("17:00"), at("18:00"),
                                                  SpanCategory.ACTIVITY))


class TestLaneMDuties(HookCase):

    def test_people_nudge_is_called_on_a_tick(self):
        with patch("thirties_core.people.background_duty") as duty:
            self.svc.tick(at("17:00"))
        duty.assert_called_once_with(self.svc, at("17:00"))

    def test_rush_tickets_is_called_on_a_tick(self):
        with patch("thirties_core.tickets.background_duty") as duty:
            self.svc.tick(at("17:00"))
        duty.assert_called_once_with(self.svc, at("17:00"))

    def test_a_failing_duty_is_logged_and_the_tick_goes_on(self):
        last = MagicMock(__name__="joplin_presence")  # the tick's last duty, after both hooks
        self.svc.joplin_presence = last
        with patch("thirties_core.people.background_duty", side_effect=RuntimeError("boom")), \
                patch("thirties_core.tickets.background_duty", side_effect=RuntimeError("boom")), \
                self.assertLogs("thirties_core.background", level="ERROR") as logs:
            self.svc.tick(at("17:00"))
        self.assertEqual(len([r for r in logs.output if "boom" in r]), 2)
        last.assert_called_once_with(at("17:00"))  # the rest of the tick ran
        self.assertTrue(self.desktop)  # and said what it had to (the morning, here)

    def test_they_wait_until_the_user_is_up(self):
        with patch("thirties_core.people.background_duty") as duty, \
                patch.object(BackgroundService, "_up", return_value=False):
            self.svc.tick(at("17:00"))
        duty.assert_not_called()


if __name__ == "__main__":
    unittest.main()
