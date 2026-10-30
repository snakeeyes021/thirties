"""Actual wake, sleep and work times (B6) and the context stored with each outcome (B7)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.day_times import day_time_facts, due_question, morning_sleep, wake_time, work_span
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.outcome_context import OutcomeContextStore
from thirties_core.plan_tools import NOT_DONE, PlanTools


class DayTimesCase(unittest.TestCase):
    """Thursday Oct 15: slept 11 PM - 7 AM (the night before), work 8:30 AM - 4 PM, bed at 11 PM."""

    def setUp(self):
        self.now("09:45")
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db))

    @staticmethod
    def now(hhmm, offset=0):
        set_debug_time(at(hhmm, offset))

    def history(self):
        plan = self.t.day_plan
        return self.sched.state_db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_end)

    def fresh(self):
        return self.sched.build_day_plan(DAY)[0]


class TestRecording(DayTimesCase):
    def test_work_start_and_wake_move_the_envelopes_and_keep_the_plan(self):
        result = self.t.execute("log_day_times", {"woke": "7:40 AM", "work_started": "9:10 AM"})
        self.assertEqual(result, "Recorded: woke 7:40 AM, started work 9:10 AM.")
        plan = self.fresh()
        work = work_span(plan)
        self.assertEqual((work.start_dt, work.actual_start, work.planned_start), (at("09:10"), at("09:10"), at("08:30")))
        self.assertIsNone(work.actual_end)  # not over yet: nothing said about the end
        self.assertEqual(work.time_source, "reported")
        sleep = morning_sleep(plan, self.history())
        self.assertEqual((sleep.end_dt, sleep.actual_end, sleep.planned_end), (at("07:40"), at("07:40"), at("07:00")))
        self.assertEqual(wake_time(plan, self.history()), at("07:40"))

    def test_a_wake_before_the_canvas_starts_is_this_morning(self):
        self.t.execute("log_day_times", {"woke": "6:15 AM"})
        sleep = morning_sleep(self.fresh(), self.history())
        self.assertEqual(sleep.actual_end, at("06:15"))  # not tomorrow morning's 6:15

    def test_done_with_work_now_and_bedtime(self):
        self.now("17:20")
        self.t.execute("log_day_times", {"work_ended": "now"})
        self.assertEqual(work_span(self.fresh()).end_dt, at("17:20"))  # ran long; the evening shrinks
        self.now("23:50")
        self.t.execute("log_day_times", {"slept": "11:45 PM"})
        night = next(s for s in self.fresh().spans if s.category == SpanCategory.SLEEP and s.start_dt > at("12:00"))
        self.assertEqual((night.actual_start, night.planned_start), (at("23:45"), at("23:00")))

    def test_last_nights_bedtime_said_in_the_morning(self):
        self.t.execute("log_day_times", {"slept": "1:00 AM"})
        sleep = morning_sleep(self.fresh(), self.history())
        self.assertEqual(sleep.actual_start, at("01:00"))  # last night, 1 AM Thursday

    def test_nothing_changes_when_a_time_is_impossible_or_missing(self):
        self.assertTrue(self.t.execute("log_day_times", {"work_started": "5:00 PM"}).startswith(NOT_DONE))
        self.assertEqual(work_span(self.fresh()).start_dt, at("08:30"))
        self.assertIn("which times", self.t.execute("log_day_times", {}))
        self.t.execute("remove_event", {"event": "Work"})
        self.assertIn("no Work", self.t.execute("log_day_times", {"work_started": "9:00 AM"}))

    def test_the_rollover_places_nothing_before_a_late_wake(self):
        self.now("11:30")
        self.t.execute("log_day_times", {"woke": "11:00 AM"})
        windows = self.t.open_windows()
        self.assertTrue(all(a >= at("11:00") or b <= at("07:00") for a, b in windows if a < at("12:00")))

    def test_statistics_from_recorded_times(self):
        spans = []
        for d, (woke, started, ended) in enumerate([("07:30", "08:45", "16:30"), ("07:40", "09:00", "17:00"),
                                                    ("07:20", "08:50", "16:45")]):
            sleep = EventSpan(f"s{d}", "Sleep", at("23:00", d - 1), at("07:00", d), SpanCategory.SLEEP)
            sleep.planned_start, sleep.planned_end = sleep.start_dt, sleep.end_dt
            sleep.actual_end, sleep.time_source = at(woke, d), "reported"
            work = EventSpan(f"w{d}", "Work", at(started, d), at(ended, d), SpanCategory.WORK,
                             planned_start=at("08:30", d), planned_end=at("16:00", d), actual_start=at(started, d),
                             actual_end=at(ended, d), time_source="reported")
            spans += [sleep, work]
        facts = day_time_facts(spans)
        self.assertIn("You usually wake around 7:30 AM, about 30 minutes later than planned (3 mornings recorded).",
                      facts)
        self.assertIn("Work usually starts about 15 minutes later than planned (3 days recorded).", facts)
        self.assertIn("A workday usually runs about 8h (planned 7h 30m; 3 days recorded).", facts)
        self.assertEqual(day_time_facts(spans[:4]), [])  # two days: too few to say anything


class TestAsking(DayTimesCase):
    def test_the_morning_question_waits_for_work_to_start(self):
        plan, history = self.fresh(), self.history()
        self.assertIsNone(due_question(plan, at("08:30"), {}, history))
        key, text = due_question(plan, at("09:00"), {}, history)
        self.assertEqual(text, "☀️ For the record: when did you wake up, and when did you start work?")
        self.assertIsNone(due_question(plan, at("09:05"), {key: "x"}, history))  # once a day

    def test_what_is_known_isnt_asked(self):
        self.t.execute("log_day_times", {"woke": "7:10 AM"})
        _, text = due_question(self.fresh(), at("09:00"), {}, self.history())
        self.assertEqual(text, "💼 For the record: when did you start work?")
        self.t.execute("log_day_times", {"work_started": "8:30 AM"})
        self.assertIsNone(due_question(self.fresh(), at("09:00"), {}, self.history()))

    def test_done_with_work_at_its_planned_end(self):
        plan = self.fresh()
        self.assertIsNone(due_question(plan, at("15:55"), {"morning:2026-10-15": "x"}, self.history()))
        key, text = due_question(plan, at("16:00"), {"morning:2026-10-15": "x"}, self.history())
        self.assertEqual(key, "work_end:2026-10-15")
        self.assertIn("Done with work?", text)

    def test_a_day_off_only_asks_about_waking(self):
        self.t.execute("remove_event", {"event": "Work"})
        plan = self.fresh()
        self.assertEqual(due_question(plan, at("08:00"), {}, self.history())[1],
                         "☀️ For the record: when did you wake up?")
        self.assertIsNone(due_question(plan, at("16:00"), {"morning:2026-10-15": "x"}, self.history()))

    def test_the_background_service_asks_once_at_medium(self):
        self.sched.config.assistant.message_level = "medium"
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        sent = []
        svc = BackgroundService(self.sched.config, lambda t, b, k: sent.append(b), scheduler=self.sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.today = lambda: DAY
        svc.day_times(at("09:00"))
        svc.day_times(at("09:15"))
        self.assertEqual(len(sent), 1)
        self.sched.config.assistant.message_level = "light"
        svc.day_times(at("16:00"))
        self.assertEqual(len(sent), 1)  # light is only heads-ups before calendar events


class TestOutcomeContext(DayTimesCase):
    def test_context_is_captured_when_the_outcome_is_logged(self):
        self.now("19:30")
        self.t.execute("log_day_times", {"woke": "7:30 AM"})
        self.t.execute("add_event", {"title": "Groceries", "start": "5:00 PM", "duration": "30 min"})
        self.t.execute("add_event", {"title": "Gym", "start": "6:00 PM", "duration": "1 hour"})
        self.t.execute("add_event", {"title": "Dinner", "start": "7:00 PM", "duration": "1 hour"})
        gym = next(s for s in self.t.day_plan.spans if s.title == "Gym")
        self.t.execute("log_outcome", {"event": gym.id, "status": "skipped"})
        ctx = OutcomeContextStore(self.sched.state_db).get(gym.id)
        self.assertEqual(ctx["status"], "skipped")
        self.assertEqual((ctx["gap_before_min"], ctx["gap_after_min"]), (30, 0))
        self.assertEqual(ctx["minutes_since_wake"], 630)  # 7:30 AM to 6:00 PM, from the recorded wake
        self.assertEqual([n["title"] for n in ctx["nearby"]], ["Groceries", "Dinner"])
        self.assertEqual(ctx["nearby"][0]["kind"], "activity")
        self.assertFalse(ctx["during_work"])
        fullness = ctx["day_fullness"]
        self.assertEqual(fullness["items"], 3)
        self.assertEqual(fullness["planned_minutes"], 150)
        self.assertEqual(fullness["awake_minutes"], 930)  # 7:30 AM to 11 PM

        # the plan changes later; the record keeps what it was when the outcome was logged
        self.t.execute("remove_event", {"event": "Dinner"})
        self.assertEqual(OutcomeContextStore(self.sched.state_db).get(gym.id)["gap_after_min"], 0)

    def test_it_syncs(self):
        from thirties_core.sync_engine import SyncEngine
        self.assertIn("outcome_context", SyncEngine(config=self.sched.config, state_db=self.sched.state_db,
                                                    joplin_engine=MagicMock())._stores())
        self.t.execute("add_event", {"title": "Walk", "start": "10:00 AM", "duration": "30 min"})
        walk = next(s for s in self.t.day_plan.spans if s.title == "Walk")
        self.now("10:35")
        self.t.execute("log_outcome", {"event": walk.id, "status": "done"})
        store, other = OutcomeContextStore(self.sched.state_db), OutcomeContextStore(make_scheduler().state_db)
        for record in store.all_records():
            self.assertTrue(other.merge_record(record))
        self.assertTrue(other.get(walk.id)["during_work"])


if __name__ == "__main__":
    unittest.main()
