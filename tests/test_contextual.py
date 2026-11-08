"""Contextual messages ("have you left yet?"): only for items that matter, once each, by message level."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.contextual import ContextualFlags, moments
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


class ContextualCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("12:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([event("dent", "15:00", "16:00", "Dentist")])
        self.sched.config.assistant.message_level = "light"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)), scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=MockInferenceEngine(),
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()

    def add(self, sid, title, start, end):
        self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        self.svc._manager = None

    def plan(self):
        return self.sched.build_day_plan(DAY)[0]


class TestRule(ContextualCase):

    def test_travel_before_a_fixed_item(self):
        self.add("drv", "Drive to dentist", "14:30", "15:00")
        found = moments(self.plan(), lambda s: s.source == "calendar", set())
        self.assertEqual([(m.key, m.at, m.text) for m in found],
                         [("drv", at("14:35"), "🚗 Are you on your way to Dentist? It's at 3:00 PM.")])

    def test_a_leave_by_time(self):
        self.add("vet", "Vet appointment (leave by 4:40)", "17:00", "18:00")
        found = [m for m in moments(self.plan(), lambda s: False, set()) if m.key == "vet"]
        self.assertEqual((found[0].at, found[0].until), (at("16:45"), at("17:00")))
        self.assertIn("Have you left for Vet appointment", found[0].text)

    def test_a_commute_before_work(self):
        self.add("cm", "Commute", "08:00", "08:30")  # the fixture's Work starts at 8:30
        found = moments(self.plan(), lambda s: False, set())
        self.assertEqual([(m.key, m.text) for m in found], [("cm", "🚗 Are you on your way to Work? It's at 8:30 AM.")])

    def test_ordinary_items_and_travel_to_nothing_fixed_get_nothing(self):
        self.add("w", "Walk", "13:00", "13:30")
        self.add("drv", "Drive to the park", "17:00", "17:30")
        self.assertEqual(moments(self.plan(), lambda s: False, set()), [])

    def test_flagged_items(self):
        self.add("tx", "Taxes", "17:00", "18:00")
        ContextualFlags(self.sched.state_db).set("tx")
        found = moments(self.plan(), lambda s: False, ContextualFlags(self.sched.state_db).flagged())
        self.assertEqual([(m.key, m.at, m.text) for m in found], [("tx", at("17:10"), "👀 Have you started Taxes?")])


class TestDelivery(ContextualCase):

    def test_once_and_only_in_its_window(self):
        self.add("drv", "Drive to dentist", "14:30", "15:00")
        self.svc.contextual(at("14:30"))
        self.svc.contextual(at("14:36"))
        self.svc.contextual(at("14:40"))
        self.assertEqual(self.sent, [("contextual", "🚗 Are you on your way to Dentist? It's at 3:00.")])  # AM/PM only where unclear
        self.assertEqual(self.svc.bridge.broadcast.call_args.kwargs["expires"], at("15:00"))

    def test_not_once_the_travel_is_reported(self):
        span = EventSpan("drv", "Drive to dentist", at("14:30"), at("15:00"), SpanCategory.ACTIVITY)
        span.status = SpanStatus.DONE
        self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        self.svc.contextual(at("14:40"))
        self.assertEqual(self.sent, [])

    def test_off_means_off(self):
        self.sched.config.assistant.message_level = "off"
        self.add("drv", "Drive to dentist", "14:30", "15:00")
        self.svc.contextual(at("14:40"))
        self.assertEqual(self.sent, [])

    def test_the_check_on_tool_flags_an_item(self):
        self.add("tx", "Taxes", "17:00", "18:00")
        out = self.svc.manager().execute_tool("check_on", {"item": "Taxes"})
        self.assertEqual(out, "I'll check on Taxes once its time comes (5:00 PM).")
        self.svc.contextual(at("17:12"))
        self.assertEqual(self.sent, [("contextual", "👀 Have you started Taxes?")])


if __name__ == "__main__":
    unittest.main()
