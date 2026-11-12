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
        self.svc.bridge.broadcast.assert_called_once()
        self.assertEqual(self.svc.bridge.broadcast.call_args[0][0],
                         "👋 Just checking in: did you get to *Taxes*, or are you still on it?")
        self.assertIn("did you get to Taxes", self.svc.manager().history()[-1]["content"])
        fresh = BackgroundService(self.sched.config, None, scheduler=self.sched, joplin_engine=self.svc.joplin_engine,
                                  inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        fresh.today = lambda: DAY
        fresh.check_ins(at("18:30"))  # a restart doesn't ask again
        self.assertEqual(len(self.desktop), 1)

    def test_the_morning_message_says_what_was_planned_ahead(self):
        # O2 (10-24): something set for tomorrow should be seen the next morning
        from tests.fixtures import mark_up
        mark_up(self.sched.state_db, at("08:30"))  # the user's first message of the day
        self.svc.rollover(at("09:00"))
        self.assertEqual(self.desktop, [("rollover", "☀️ Good morning! Already planned for today:\n5:00 PM Taxes")])

    def test_the_drop_box_is_offered_in_the_afternoon_twice_an_hour_at_most(self):
        from unittest.mock import patch
        with patch("thirties_core.list_tools.ListTools.dropbox_offer", return_value="🗃️ 3 things are waiting") as offer:
            self.svc.dropbox(at("09:30"))  # not in the morning
            self.assertEqual(offer.call_count, 0)
            self.svc.dropbox(at("14:00"))
            self.svc.dropbox(at("14:01"))
        self.assertEqual(offer.call_count, 1)
        self.assertEqual(self.desktop, [("dropbox", "🗃️ 3 things are waiting")])

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
        for title, minute in (("Feed the fish", 0), ("Make coffee", 5), ("Pack lunch", 10)):
            start = at("11:00") + timedelta(minutes=minute)
            self.sched.state_db.upsert_span(EventSpan(f"m{minute}", title, start, start + timedelta(minutes=5),
                                                      SpanCategory.ACTIVITY))
        for minute in (0, 1, 5, 10):  # ticks through the thirty
            self.svc.starts(at("11:00") + timedelta(minutes=minute))
        self.assertEqual(self.desktop, [("start", "🔔 11:00: Feed the fish, Make coffee, Pack lunch")])
        self.svc.starts(at("17:00") + timedelta(seconds=20))
        self.assertEqual(self.desktop[-1][1].split("\n")[0], "🔔 5:00: Taxes")

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
        self.assertEqual(self.desktop[-1][1].split("\n")[0], "🔔 10:00: Brush teeth")  # the first was done: new

    def test_whats_behind_is_listed_under_the_status_when_something_new_falls_behind(self):
        for sid, title, start in (("k", "Feed the fish", "11:00"), ("c", "Make coffee", "11:10"), ("s", "Shower", "11:30")):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(start) + timedelta(minutes=15),
                                                      SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("s2", "Shower", at("11:45"), at("11:55"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("w", "Walk", at("12:00"), at("12:30"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("l", "Lunch", at("12:30"), at("13:00"), SpanCategory.ACTIVITY))
        self.svc._manager = None
        self.svc.starts(at("12:00"))
        self.assertEqual(self.desktop[-1], ("start", "🔔 12:00 PM: Walk\nNot marked yet:\n- Feed the fish\n"
                                                      "- Make coffee\n- Shower"))  # one per line, once each, no times
        self.svc.starts(at("12:30"))
        self.assertEqual(self.desktop[-1][1].split(": ", 1)[1], "Lunch\nNot marked yet:\n- Feed the fish\n"
                                                               "- Make coffee\n- Shower\n- Walk")  # Walk fell behind
        self.svc.starts(at("17:00"))
        self.assertEqual(self.desktop[-1][1].split("\n")[0], "🔔 5:00: Taxes")
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
        for title, minute in (("Feed the fish", 0), ("Make coffee", 5)):
            start = at("11:00") + timedelta(minutes=minute)
            self.sched.state_db.upsert_span(EventSpan(f"c{minute}", title, start, start + timedelta(minutes=5),
                                                      SpanCategory.ACTIVITY))
        self.svc.check_ins(at("11:20"))  # the thirty isn't over yet
        self.svc.check_ins(at("11:41"))
        self.svc.check_ins(at("11:50"))
        self.assertEqual(self.desktop, [("checkin", "👋 How did it go? Did you get to Feed the fish and Make coffee?")])


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
                      "alligator", out)  # a thing to do goes on the plan, not only a ping (10-23)
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
        from tests.fixtures import mark_up
        mark_up(self.sched.state_db, at("07:00"))  # up and talking early; these tests are about the day after

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
        self.svc.check_ins(at("18:12"))  # Food's (until 6) is due at 6:10, its own time: the talk is over
        self.assertEqual([(k, "Food" in t, "Shower" in t) for k, t in self.sent], [("checkin", True, False)])  # 11-06:
        # Shower's moment passed while the user was talking: never sent late
        self.sched.state_db.upsert_span(EventSpan("x", "Errand", at("15:00"), at("15:20"), SpanCategory.ACTIVITY))
        before = len(self.sent)
        self.svc.starts(at("15:00"))  # during the dentist: quiet
        self.assertEqual(len(self.sent), before)


class TestCheckInsAtTheirOwnTime(unittest.TestCase):
    """11-06: the day was built at 9:25 (the first message), the user wrote again at 9:27, and the 9:00 block's
    check-in came at 9:57. Each check-in goes out at its own time (its thirty's end + 10 minutes) or not at all;
    none for a block that was half over when the day was built; a late build never shifts or batches them."""

    def run_morning(self, built, talked, placed_9_00=True):
        set_debug_time(at("09:30"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        sent = []
        svc = BackgroundService(sched.config, lambda t, b, k: sent.append((svc.now().strftime("%H:%M"), b)),
                                scheduler=sched, joplin_engine=joplin, inference_engine=MockInferenceEngine(),
                                sync_engine=MagicMock())
        svc.today = lambda: DAY
        sched.config.assistant.message_level = "heavy"
        if placed_9_00:
            sched.state_db.upsert_span(EventSpan("fk", "Feed the fish", at("09:00"), at("09:06"), SpanCategory.TASK))
        sched.state_db.upsert_span(EventSpan("th", "Make bed", at("09:30"), at("09:37"), SpanCategory.TASK))
        sched.state_db.set_sync_meta(f"day_built_at:{DAY.isoformat()}", at(built).isoformat())
        sched.state_db.set_sync_meta("last_user_message_at", at(talked).isoformat())
        t = at("09:30")
        while t <= at("10:40"):
            set_debug_time(t)
            svc.check_ins(t)
            t += timedelta(minutes=1)
        return sent

    def test_this_morning_built_at_9_25(self):
        sent = self.run_morning("09:25", "09:27")
        self.assertEqual([(when, "Make bed" in text, "Feed the fish" in text) for when, text in sent],
                         [("10:10", True, False)])  # the 9:30 block on time; the 9:00 block never

    def test_built_at_9_02_the_9_00_block_gets_its_check_in(self):
        sent = self.run_morning("09:02", "09:02")
        self.assertEqual([when for when, _ in sent], ["09:40", "10:10"])

    def test_talking_at_its_time_means_it_isnt_sent_later(self):
        sent = self.run_morning("09:02", "09:35")  # still talking at 9:40: the 9:00 block's moment passes
        self.assertEqual([when for when, _ in sent], ["10:10"])


class TestTurnsInTheBackground(unittest.TestCase):
    """T1: a lost turn is answered later and the reply delivered; T2: desktop turns go to the phone; C3: notices
    carry when they go stale."""

    def setUp(self):
        from thirties_core import turns
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        waits = turns.RETRY_WAITS
        turns.RETRY_WAITS = (0.0, 0.0)
        self.addCleanup(setattr, turns, "RETRY_WAITS", waits)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.engine = MockInferenceEngine()
        self.desktop = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.svc.run_in_threads = False

    def test_a_lost_turn_is_answered_and_sent_once_the_model_is_back(self):
        from tests.test_conversation import reply
        timeout = {"role": "assistant", "content": "The Antigravity assistant timed out waiting.", "tool_calls": []}
        self.engine.responses = [timeout] * 3
        self.svc.manager().send_user_message("fish are fed", via="telegram")
        self.engine.responses = [reply("Logged: fish fed.")]
        self.svc._fixed_now = True
        self.svc.answer_lost_turns(at("09:01"))  # not yet: the first retry waits two minutes
        self.assertEqual(self.desktop, [])
        self.svc.answer_lost_turns(at("09:03"))
        self.assertEqual(self.desktop, [("reply", "Logged: fish fed.")])
        self.svc.bridge.broadcast.assert_called_once_with("Logged: fish fed.")
        self.svc.answer_lost_turns(at("09:10"))
        self.assertEqual(len(self.desktop), 1)

    def test_the_desktop_chat_is_mirrored_to_the_phone(self):
        self.svc._mirror(["plan my evening"], "Sure: dinner at 7.", False)
        sent = [c.args[0] for c in self.svc.bridge.broadcast.call_args_list]
        self.assertEqual(sent, ["🖥 You, on the desktop:\nplan my evening", "Sure: dinner at 7."])
        self.svc.bridge.broadcast.reset_mock()
        self.sched.config.telegram.mirror_desktop = False
        self.svc._mirror(["x"], "y", False)
        self.svc.bridge.broadcast.assert_not_called()

    def test_a_start_notice_goes_stale_at_the_end_of_its_block(self):
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", at("17:00"), at("18:00"), SpanCategory.TASK))
        self.svc._manager = None
        self.svc.starts(at("17:00"))
        self.assertEqual(self.svc.bridge.broadcast.call_args.kwargs["expires"], at("17:30"))

    def test_a_check_in_counts_as_asked_so_replies_dont_ask_again(self):
        from thirties_core.messages import MissedTracker
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", at("17:00"), at("18:00"), SpanCategory.TASK))
        self.svc._manager = None
        self.svc.check_ins(at("18:12"))  # its own time: 10 minutes after its thirty ends
        self.assertIn("taxes", MissedTracker(self.sched.state_db).titles_shown(at("18:12")))


if __name__ == "__main__":
    unittest.main()


class TestBeforeTheUserIsUp(unittest.TestCase):
    """11-04, 11-05: a proposal at 7:00, a block notice, "when did you start work?" at 7:30 and a check-in at 7:40,
    all before the user woke at 9. Until their first message nothing is sent, except reminders they asked for and
    one heads-up about a fixed calendar event."""

    def setUp(self):
        set_debug_time(at("07:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler([event("dentist", "09:00", "09:30", "Dentist"),
                                     event("call", "10:00", "10:30", "Call")])
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.sort_lists_enabled = False
        self.sched.config.assistant.message_level = "heavy"
        y = lambda hhmm: at(hhmm, -1)
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", y("17:00"), y("18:00"), SpanCategory.TASK))
        self.sched.state_db.upsert_span(EventSpan("jr", "Journal", at("07:00"), at("07:30"), SpanCategory.ACTIVITY))

    def run_until(self, end, start="07:00"):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            self.svc.tick(t)
            t += timedelta(minutes=5)

    def test_one_first_message_then_nothing_until_the_reply(self):
        from thirties_core.memory import ReminderStore
        ReminderStore(self.sched.state_db).add(at("08:00"), "Take the bins out")
        self.run_until("10:20", start="07:30")  # after the test day's sunrise: before it, it's still yesterday
        kinds = [k for k, _ in self.sent]
        self.assertEqual(kinds, ["morning", "reminder"], self.sent)  # no heads-up, check-in or block notice
        first = self.sent[0][1]
        from thirties_core.wake import FALLBACK_LATE
        line, items = first.split("\n\n", 1)
        # sent at 7:30 for a 7:00 wake-up (the computer was asleep): the time said as past, never as now
        self.assertEqual(line, FALLBACK_LATE["wake"].format(clock="7:00"))  # the mock model: the plain fallback
        self.assertEqual(items, "On the plate today:\nJournal\nDentist\nCall")  # one per line, no bullets, no times
        self.assertNotIn("Taxes", first)  # yesterday's leftover isn't today's by default (carry.py)
        self.assertFalse(any(s.title == "Taxes" for s in self.svc.manager().day_plan.spans))  # nothing built
        from tests.fixtures import mark_up
        mark_up(self.sched.state_db, at("10:22"))  # the reply
        self.run_until("10:30", start="10:25")
        from tests.fixtures import DAY as _DAY
        self.assertTrue(self.sched.state_db.get_sync_meta(f"rollover_offered:{_DAY.isoformat()}"))  # built after it
        self.assertEqual([k for k, _ in self.sent].count("morning"), 1)

    def test_at_the_wake_up_time_even_before_sunrise(self):
        # 11-05: "at your wake-up time" is its own choice, not "sunrise or later"
        from datetime import timedelta as _td
        from thirties_core.day_times import morning_sleep
        plan = self.svc.manager().day_plan
        sleep = morning_sleep(plan, self.sched.state_db.get_spans_between(plan.day_start - _td(hours=18),
                                                                          plan.day_start))
        wake = sleep.end_dt
        early = plan.sunrise - _td(minutes=40)  # a winter morning: the alarm before the sun
        sleep.end_dt = early
        self.sched.state_db.upsert_span(sleep)
        self.svc.today = lambda: DAY - timedelta(days=1)  # before sunrise it's still yesterday's day
        self.svc._manager = None
        set_debug_time(early - _td(minutes=5))
        self.svc.morning_hello(early - _td(minutes=5))
        self.assertEqual(self.sent, [])
        set_debug_time(early)
        self.svc.morning_hello(early)
        self.assertEqual([k for k, _ in self.sent], ["morning"])
        from tests.fixtures import mark_up
        from thirties_core.wake import user_is_up
        mark_up(self.sched.state_db, early + _td(minutes=10))  # the reply, still before sunrise, counts for today
        self.assertTrue(user_is_up(self.sched.state_db, self.sched.build_day_plan(DAY)[0], plan.sunrise + _td(minutes=5)))
        self.assertNotEqual(wake, early)

    def test_the_first_message_at_sunrise_and_without_the_items(self):
        self.sched.config.assistant.morning_message_at = "sunrise"
        self.sched.config.assistant.morning_message_items = False
        plan = self.svc.manager().day_plan
        before = plan.sunrise - timedelta(minutes=1)
        set_debug_time(before)
        self.svc.morning_hello(before)
        self.assertEqual(self.sent, [])
        set_debug_time(plan.sunrise)
        self.svc.morning_hello(plan.sunrise)
        self.svc.morning_hello(plan.sunrise + timedelta(minutes=5))
        from thirties_core.clock import format_clock
        from thirties_core.wake import FALLBACK
        self.assertEqual(self.sent, [("morning", FALLBACK["sunrise"].format(clock=format_clock(plan.sunrise).replace(" AM", "")))])


class TestRollover(unittest.TestCase):
    """Yesterday's unmarked items (carry.py; 11-05): a one-off on a list is asked about once, in one
    question; nothing is moved into today; things on no list simply end."""

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        y = lambda hhmm: at(hhmm, -1)
        from thirties_core.models import TaskItem
        self.tasks = [TaskItem(id="t_tax", source_notebook="1. Tasks", title="Taxes", is_checkbox_item=True,
                               parent_note_title="Errands/Admin", parent_note_id="n_errands")]
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", y("17:00"), y("18:00"), SpanCategory.TASK,
                                                  task_id="t_tax"))
        self.sched.state_db.upsert_span(EventSpan("ml", "Email Mom", y("19:00"), y("19:30"), SpanCategory.ACTIVITY))
        self.sched.state_db.upsert_span(EventSpan("dn", "Dishes", y("20:00"), y("20:30"), SpanCategory.ACTIVITY))
        from thirties_core.conversation import ConversationManager
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, list(self.tasks), pending, self.sched, inference_engine=MockInferenceEngine(),
                                     active_notes=[])

    def test_a_notice_and_nothing_moved_into_today(self):
        text = self.m.execute_tool("rollover_proposal", {})
        # 11-06: in effect by itself, said as a notice; Email Mom and Dishes are tasks too (no item is "on no list")
        self.assertEqual(text.split("\n")[0], "Kept 3 from before (0 on today's plan): see Before today.")
        self.assertIn("Keep: Taxes, tomorrow.", text)  # each placement said, so it can be corrected
        keep = {r["title"]: r["when"] for r in self.m.tools.assistant.before.rows()["keep"]}
        self.assertEqual(keep, {"Taxes": "tomorrow", "Email Mom": "tomorrow", "Dishes": "tomorrow"})
        out = self.m.execute_tool("apply_rollover", {})
        self.assertIn("The rest wait where proposed.", out)
        self.assertNotIn("Taxes", [s.title for s in self.sched.build_day_plan(DAY)[0].spans])

    def test_offered_once_after_waking(self):
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = list(self.tasks), []
        sent = []
        svc = BackgroundService(self.sched.config, lambda t, b, k: sent.append((k, b)), scheduler=self.sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: DAY
        svc.rollover(at("07:10"))  # the planned wake (7:00) has passed, but the user hasn't said anything yet
        svc.rollover(at("08:00"))
        self.assertEqual(sent, [])  # 11-04, 11-05: nothing before the user is up
        from tests.fixtures import mark_up
        mark_up(self.sched.state_db, at("08:10"))  # their first message of the day
        svc.rollover(at("08:15"))
        svc.rollover(at("08:30"))
        self.assertEqual([k for k, _ in sent], ["rollover"])
        self.assertTrue(sent[0][1].strip(), sent[0][1])  # one message (the model writes the notice)
