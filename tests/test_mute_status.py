"""Muting check-ins quiets what Thirties says by itself, never where things stand: a schedule the user asks for
still shows what's not marked yet. Invented items; DAY is Thu Oct 15."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory


class TestMutedStillShown(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("18:50"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        for sid, title, start, end in (("oil", "Oil the hinges", "16:00", "16:30"),
                                       ("seeds", "Sort the seed packets", "16:30", "17:00"),
                                       ("dinner", "Dinner", "19:00", "20:00")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(),
                                     active_notes=[])
        self.assertIn("No more check-ins until", self.m.execute_tool("mute_checkins", {}))  # mute everything

    def test_the_schedule_still_lists_whats_not_marked(self):
        out = self.m.execute_tool("show_schedule", {})
        self.assertIn("Not marked yet:", out)
        self.assertIn("Oil the hinges", out)
        self.assertIn("Sort the seed packets", out)

    def test_the_prompt_knows_them_but_not_to_ask(self):
        from thirties_core.messages import now_section
        text = now_section(self.m.tools)
        self.assertIn("Not marked yet (their time is over, nothing said): Oil the hinges, Sort the seed packets", text)
        self.assertIn("Check-ins are muted for Oil the hinges, Sort the seed packets: never ask about those", text)

    def test_no_notice_lists_them(self):
        svc = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=MagicMock(),
                                inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today, svc._manager, svc._day = (lambda: DAY), self.m, DAY
        self.assertEqual(svc._with_missed("🔔 7:00 PM: Dinner", at("18:55")), "🔔 7:00 PM: Dinner")


if __name__ == "__main__":
    unittest.main()
