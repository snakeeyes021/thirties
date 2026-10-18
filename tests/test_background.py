"""The background service: check-ins, reminders, start nudges and muting, delivered to desktop and phone."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.memory import CheckinMutes, ReminderStore
from thirties_core.models import EventSpan, SpanCategory


class TestBackground(unittest.TestCase):

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
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", at("17:00"), at("18:00"), SpanCategory.TASK))

    def test_check_in_once_after_an_item_ends_on_desktop_and_phone(self):
        self.svc.check_ins(at("18:15"))
        self.svc.check_ins(at("18:20"))
        self.assertEqual(self.desktop, [("checkin", "👋 Just checking in: did you get to Taxes, or are you still on it?")])
        self.svc.bridge.broadcast.assert_called_once_with("👋 Just checking in: did you get to *Taxes*, or are you still on it?")
        self.assertIn("did you get to Taxes", self.svc.manager().history()[-1]["content"])
        fresh = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=self.svc.joplin_engine,
                                  inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        fresh.today = lambda: DAY
        fresh.check_ins(at("18:30"))  # a restart doesn't ask again
        self.assertEqual(len(self.desktop), 1)

    def test_muted_items_and_muted_days_get_no_check_in(self):
        CheckinMutes(self.sched.state_db).mute(at("23:00"), "tx")
        self.svc.check_ins(at("18:15"))
        self.assertEqual(self.desktop, [])

    def test_reminders_are_delivered_once(self):
        ReminderStore(self.sched.state_db).add(at("15:00"), "Water the alligator")
        self.svc.reminders(at("14:59"))
        self.svc.reminders(at("15:00"))
        self.svc.reminders(at("15:01"))
        self.assertEqual(self.desktop, [("reminder", "⏰ Reminder: Water the alligator")])

    def test_start_nudge_once_per_thirty_without_minute_times(self):
        for title, minute in (("Feed kitties", 0), ("Caffeinate", 5), ("Water bottle", 10)):
            start = at("11:00") + timedelta(minutes=minute)
            self.sched.state_db.upsert_span(EventSpan(f"m{minute}", title, start, start + timedelta(minutes=5),
                                                      SpanCategory.ACTIVITY))
        for minute in (0, 1, 5, 10):  # ticks through the thirty
            self.svc.starts(at("11:00") + timedelta(minutes=minute))
        self.assertEqual(self.desktop, [("start", "🔔 11:00: Feed kitties, Caffeinate, Water bottle")])
        self.svc.starts(at("17:00") + timedelta(seconds=20))
        self.assertEqual(self.desktop[-1], ("start", "🔔 5:00: Taxes"))

    def test_moved_item_is_not_announced_again(self):
        self.svc.starts(at("17:00"))
        span = self.sched.state_db.get_spans_between(at("17:00"), at("18:00"))[0]
        span.start_dt, span.end_dt = at("17:30"), at("18:30")
        self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        self.svc.starts(at("17:30"))
        self.assertEqual(len(self.desktop), 1)

    def test_check_ins_batch_the_items_of_a_thirty(self):
        for title, minute in (("Feed kitties", 0), ("Caffeinate", 5)):
            start = at("11:00") + timedelta(minutes=minute)
            self.sched.state_db.upsert_span(EventSpan(f"c{minute}", title, start, start + timedelta(minutes=5),
                                                      SpanCategory.ACTIVITY))
        self.svc.check_ins(at("11:20"))  # the thirty isn't over yet
        self.svc.check_ins(at("11:41"))
        self.svc.check_ins(at("11:50"))
        self.assertEqual(self.desktop, [("checkin", "👋 How did it go? Did you get to Feed kitties and Caffeinate?")])


class TestReminderTools(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        from thirties_core.conversation import ConversationManager
        sched = make_scheduler()
        plan, pending = sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, sched, inference_engine=MockInferenceEngine(), active_notes=[])

    def test_set_list_cancel_and_mute(self):
        out = self.m.execute_tool("set_reminder", {"text": "Water the alligator", "date": "Saturday", "time": "3:00 PM"})
        self.assertIn("Saturday, October 17 at 3:00 PM: Water the alligator", out)
        self.assertIn("9:00 AM", self.m.execute_tool("set_reminder", {"text": "Call mom", "date": "next week"}))
        self.assertIn("Mon Oct 19 9:00 AM: Call mom", self.m.execute_tool("list_reminders", {}))
        self.assertIn("Cancelled the reminder: Call mom", self.m.execute_tool("cancel_reminder", {"reminder": "mom"}))
        self.assertIn("10:30 AM", self.m.execute_tool("set_reminder", {"text": "Stretch", "time": "in 30 minutes"}))
        self.assertIn("No more check-ins until", self.m.execute_tool("mute_checkins", {}))


if __name__ == "__main__":
    unittest.main()


class TestRollover(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        y = lambda hhmm: at(hhmm, -1)
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", y("17:00"), y("18:00"), SpanCategory.TASK))
        self.sched.state_db.upsert_span(EventSpan("ml", "Email Mom", y("19:00"), y("19:30"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("dn", "Dishes", y("20:00"), y("20:30"), SpanCategory.ACTIVITY))
        from thirties_core.conversation import ConversationManager
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(), active_notes=[])

    def test_proposal_keeps_times_that_are_free_then_applies_in_one_go(self):
        text = self.m.execute_tool("rollover_proposal", {})
        self.assertIn("- 5:00 PM – Taxes", text)  # same time today: it's free
        self.assertIn("- 7:00 PM – Email Mom", text)
        out = self.m.execute_tool("apply_rollover", {"skip": "dishes"})
        self.assertIn("Rolled into today", out)
        self.assertIn("Back to your lists: Dishes.", out)
        today = {s.title: s for s in self.sched.build_day_plan(DAY)[0].spans}
        self.assertEqual(today["Taxes"].start_dt, at("17:00"))
        yesterday = {s.title: s.status.value for s in self.sched.build_day_plan(DAY - timedelta(days=1))[0].spans
                     if s.title in ("Taxes", "Dishes")}
        self.assertEqual(yesterday, {"Taxes": "skipped", "Dishes": "skipped"})
        self.assertIn("already rolled over", self.m.execute_tool("apply_rollover", {}))

    def test_offered_once_after_waking(self):
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        sent = []
        svc = BackgroundService(self.sched.config, lambda t, b, k: sent.append((k, b)), scheduler=self.sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: DAY
        svc.rollover(at("07:10"))  # before waking (7:00) + 30 minutes
        svc.rollover(at("08:00"))
        svc.rollover(at("08:15"))
        self.assertEqual([k for k, _ in sent], ["rollover"])
        self.assertTrue(sent[0][1].startswith("☀️ Good morning! From yesterday, still open:"))
