"""The background service: check-ins, reminders, start nudges and muting, delivered to desktop and phone."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, event, make_scheduler
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService, day_stretches
from thirties_core.inference import MockInferenceEngine
from thirties_core.memory import CheckinMutes, ReminderStore
from thirties_core.models import EventSpan, SpanCategory


class TestBackground(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"  # every item; the levels have their own tests
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
        self.assertEqual(self.desktop[-1][1].split("\n\n")[0], "🔔 5:00: Taxes")

    def test_moved_item_is_not_announced_again(self):
        self.svc.starts(at("17:00"))
        span = self.sched.state_db.get_spans_between(at("17:00"), at("18:00"))[0]
        span.start_dt, span.end_dt = at("17:30"), at("18:30")
        self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        self.svc.starts(at("17:30"))
        self.assertEqual(len(self.desktop), 1)

    def test_a_replanned_copy_is_not_announced_again_but_a_second_one_is(self):
        self.svc.starts(at("17:00"))
        self.sched.state_db.delete_span("tx")  # a redraw: the same item, a new id, later
        self.sched.state_db.upsert_span(EventSpan("tx2", "Taxes", at("18:30"), at("19:30"), SpanCategory.TASK))
        self.svc._manager = None
        self.svc.starts(at("18:30"))
        self.assertEqual(len(self.desktop), 1)  # 10-21: announced at 6:00, 6:30, 8:30 and 9:30
        teeth = EventSpan("t1", "Brush teeth", at("08:00"), at("08:30"), SpanCategory.ACTIVITY)
        self.sched.state_db.upsert_span(teeth)
        self.svc._manager = None
        self.svc.starts(at("08:00"))
        from thirties_core.models import SpanStatus
        teeth.status = SpanStatus.DONE
        self.sched.state_db.upsert_span(teeth)
        self.sched.state_db.upsert_span(EventSpan("t2", "Brush teeth", at("22:00"), at("22:30"), SpanCategory.ACTIVITY))
        self.svc._manager = None
        self.svc.starts(at("22:00"))
        self.assertEqual(self.desktop[-1][1].split("\n\n")[0], "🔔 10:00: Brush teeth")  # the first was done: new

    def test_whats_behind_is_listed_at_the_bottom_when_something_new_falls_behind(self):
        for sid, title, start in (("k", "Feed kitties", "11:00"), ("c", "Caffeinate", "11:10"), ("s", "Shower", "11:30")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(start) + timedelta(minutes=15),
                                                      SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("w", "Walk", at("12:00"), at("12:30"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("l", "Lunch", at("12:30"), at("13:00"), SpanCategory.ACTIVITY))
        self.svc._manager = None
        self.svc.starts(at("12:00"))
        self.assertEqual(self.desktop[-1], ("start", "🔔 12:00 PM: Walk\n\nNot marked yet:\n- Feed kitties, "
                                                      "Caffeinate\n- Shower"))  # by block, no times
        self.svc.starts(at("12:30"))
        self.assertEqual(self.desktop[-1][1].split(": ", 1)[1], "Lunch\n\nNot marked yet:\n- Feed kitties, "
                                                               "Caffeinate\n- Shower\n- Walk")  # Walk fell behind
        self.svc.starts(at("17:00"))
        self.assertEqual(self.desktop[-1][1].split("\n\n")[0], "🔔 5:00: Taxes")
        self.assertIn("- Lunch", self.desktop[-1][1])
        self.sched.state_db.upsert_span(EventSpan("x", "Stretch", at("17:30"), at("18:00"), SpanCategory.ACTIVITY))
        self.svc._manager = None
        self.svc.starts(at("17:30"))
        self.assertEqual(self.desktop[-1], ("start", "🔔 5:30: Stretch"))  # nothing new behind: not repeated

    def test_start_notices_are_in_the_conversation(self):
        self.svc.starts(at("17:00"))
        last = self.svc.manager().messages[-1]
        self.assertEqual((last["content"], last.get("kind")), ("🔔 5:00: Taxes", "notice"))
        self.assertIn("🔔 5:00: Taxes", [m["content"] for m in self.svc.manager().history()])

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
        self.assertIn("On the plan for Saturday, October 17 at 3:00 PM, with a ping when it's time: Water the "
                      "alligator", out)  # a thing to do goes on the plan (10-23: "not a disembodied reminder")
        sat = [s.title for s in self.m.tools.plan_for("Saturday").spans]
        self.assertIn("Water the alligator", sat)
        self.assertIn("9:00 AM", self.m.execute_tool("set_reminder", {"text": "Call mom", "date": "next week"}))
        out = self.m.execute_tool("set_reminder", {"text": "the laundry's done", "time": "in 20 minutes",
                                                   "ping_only": True})
        self.assertTrue(out.startswith("Ping set for"), out)
        self.assertIn("Mon Oct 19 9:00 AM: Call mom", self.m.execute_tool("list_reminders", {}))
        self.assertIn("Cancelled the reminder: Call mom", self.m.execute_tool("cancel_reminder", {"reminder": "mom"}))
        self.assertIn("10:30 AM", self.m.execute_tool("set_reminder", {"text": "Stretch", "time": "in 30 minutes"}))
        self.assertIn("No more check-ins until", self.m.execute_tool("mute_checkins", {}))


class TestMessageLevels(unittest.TestCase):
    """How often Thirties speaks up: light, medium (the default), heavy; never while asleep or in an event."""

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([event("dentist", "15:00", "16:00", "Dentist")])
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.sched.state_db.upsert_span(EventSpan("sh", "Shower", at("16:30"), at("17:00"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("fd", "Food", at("17:00"), at("18:00"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("lu", "Lunch", at("14:00"), at("14:30"), SpanCategory.ACTIVITY))

    def run_day(self, start, end):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            for duty in (self.svc.heads_up, self.svc.stretches, self.svc.wrap_up, self.svc.check_ins, self.svc.starts):
                duty(t)
            t += timedelta(minutes=5)

    def test_stretches_cut_at_fixed_things_sunset_and_sleep(self):
        plan = self.svc.manager().day_plan
        cuts = [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in day_stretches(plan)]
        self.assertIn(("16:00", "18:30"), cuts)  # after work (until 4) and the dentist, until sunset (6:31: nearest thirty)
        self.assertTrue(all(b > a for a, b in day_stretches(plan)))
        self.assertTrue(all(b - a <= timedelta(hours=3) for a, b in day_stretches(plan)))
        self.assertFalse(any(a < at("16:00") and b > at("15:00") for a, b in day_stretches(plan)))  # not in the event

    def test_medium_is_a_few_messages_a_day(self):
        self.run_day("14:45", "20:30")
        kinds = [k for k, _ in self.sent]
        self.assertEqual(kinds.count("heads_up"), 1)
        self.assertIn(("heads_up", "⏰ Dentist at 3:00. (Lunch isn't reported yet.)"), self.sent)
        self.assertNotRegex(" ".join(b for _, b in self.sent), r":(?!00|30)\d\d")  # only :00 and :30
        starts = [b for k, b in self.sent if k == "start"]
        self.assertTrue(starts[0].startswith("🗓 Until ") and "Shower, then Food" in starts[0], starts)
        checkins = [b for k, b in self.sent if k == "checkin"]
        self.assertEqual(len(checkins), 1)
        self.assertIn("did you get to Shower and Food?", checkins[0])
        self.assertLessEqual(len(self.sent), 4)

    def test_ticks_made_elsewhere_are_seen_before_any_message(self):
        # 10-25: items ticked in the app's Day page at 10:53 were listed as "not marked yet" at 11:00
        self.svc.manager()  # the service's own copy of the day, loaded before the tick
        from thirties_core.plan_tools import PlanTools
        plan, pending = self.sched.build_day_plan(DAY)
        set_debug_time(at("14:40"))
        PlanTools(plan, pending, self.sched).execute("log_outcome", {"event": "lu", "status": "done"})  # the app
        self.svc.tick(at("14:45"))
        self.assertNotIn("Lunch", " ".join(b for _, b in self.sent))

    def test_light_is_only_the_heads_up(self):
        self.sched.config.assistant.message_level = "light"
        self.run_day("14:45", "20:30")
        self.assertEqual([k for k, _ in self.sent], ["heads_up"])

    def test_a_calendar_event_is_told_by_its_block(self):
        self.sched.state_db.set_event_decision("dentist", "attend")
        from thirties_core.calendar_engine import MockCalendarEngine
        self.sched.calendar_engine = MockCalendarEngine([event("vet", "16:45", "17:15", "Vet")])
        self.svc._manager = None
        self.run_day("16:30", "16:40")
        self.assertIn(("heads_up", "⏰ Vet at 4:45, in the 4:30 block."), self.sent)  # its real time, and its block

    def test_the_old_start_message_switch_doesnt_silence_everything(self):
        self.sched.config.telegram.notify_block_transitions = False  # someone who'd turned start messages off
        self.run_day("14:45", "15:00")
        self.assertEqual([k for k, _ in self.sent], ["heads_up"])

    def test_the_last_stretch_is_asked_about_before_bed(self):
        self.sched.state_db.upsert_span(EventSpan("rd", "Reading", at("21:00"), at("22:00"), SpanCategory.ACTIVITY))
        self.run_day("20:30", "23:30")
        checkins = [b for k, b in self.sent if k == "checkin"]
        self.assertTrue(any("Reading" in b for b in checkins), self.sent)

    def test_no_stretch_check_in_after_talking_with_the_assistant(self):
        self.sched.state_db.set_sync_meta("last_user_message_at", at("17:15").isoformat())  # mid-stretch
        self.run_day("15:55", "20:30")
        self.assertEqual([k for k, _ in self.sent if k == "checkin"], [])

    def test_one_wrap_up_before_bed_for_what_isnt_reported(self):
        self.run_day("21:30", "23:30")
        wrap = [b for k, b in self.sent if b.startswith("🌙 Before bed")]
        self.assertEqual(wrap, ["🌙 Before bed: did Lunch, Shower and Food happen? Just say which (or none)."])

    def test_off_and_no_notify(self):
        self.sched.config.assistant.message_level = "off"
        self.run_day("14:45", "20:30")
        self.assertEqual(self.sent, [])

    def test_no_check_in_while_the_user_is_talking_or_in_an_event(self):
        self.sched.config.assistant.message_level = "heavy"
        self.sched.state_db.set_sync_meta("last_user_message_at", at("17:30").isoformat())
        self.svc.check_ins(at("17:45"))  # Shower's thirty ended at 5:00: but the user just wrote
        self.assertEqual(self.sent, [])
        self.svc.check_ins(at("18:20"))
        self.assertTrue(self.sent and self.sent[0][0] == "checkin")
        self.sched.state_db.upsert_span(EventSpan("x", "Errand", at("15:00"), at("15:20"), SpanCategory.ACTIVITY))
        before = len(self.sent)
        self.svc.starts(at("15:00"))  # during the dentist: quiet
        self.assertEqual(len(self.sent), before)


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
