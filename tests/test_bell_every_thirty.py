"""At heavy, with start notices on, a bell rings at every thirty where a planned item starts (background.starts).

An invented afternoon at a map maker's desk: four items back to back from 3:10 to 5:20 PM, so each of the thirties
from 3:00 to 4:30 PM has one item starting in it. The service is asked every five minutes, as its loop would."""

import threading
import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core import holds
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory

ITEMS = (
    ("co", "Plot the coastline", "15:10", "15:40"),
    ("ha", "Hatch the hachures", "15:40", "16:10"),
    ("ca", "Letter the cartouche", "16:10", "16:50"),
    ("cn", "Shade the contours", "16:50", "17:20"),
)


class Turn:
    """An answer under way: another thread holds the turn lock until ``end``."""

    def __init__(self, lock):
        self.lock, self.held, self.done = lock, threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.held.wait(5)

    def _run(self):
        with self.lock:
            self.held.set()
            self.done.wait(10)

    def end(self):
        self.done.set()
        self.thread.join(5)


class Day(unittest.TestCase):
    """The afternoon at heavy, with start notices on."""

    def setUp(self):
        set_debug_time(at("07:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        cfg = self.sched.config
        cfg.assistant.message_level = "heavy"
        cfg.telegram.notify_block_transitions = True
        cfg.general.work_start_thirty, cfg.general.work_end_thirty = 18, 22  # 9:00 - 11:00 AM, out of the way
        for sid, title, start, end in ITEMS:
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.out = []
        self.svc = BackgroundService(cfg, lambda title, body, kind: self.out.append((kind, body)), scheduler=self.sched,
                                     joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.run_in_threads = False
        self.svc.sort_lists_enabled = False
        mark_up(self.sched.state_db, at("07:40"))  # up since the morning

    def add(self, span):
        self.sched.state_db.upsert_span(span)
        self.svc._manager = None

    def ask(self, start, end, between=None):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            if between:
                between(t)
            self.svc.starts(t)
            t += timedelta(minutes=5)

    def bells(self):
        return [body.split("\n")[0] for kind, body in self.out if kind == "start"]

    def announced(self):
        return self.svc._remembered("starts_announced")


class BellEveryThirty(Day):

    def test_a_back_to_back_run_rings_at_each_thirty_where_an_item_starts(self):
        self.ask("14:50", "17:40")
        self.assertEqual(self.bells(), ["🔔 3:00 PM: Plot the coastline", "🔔 3:30 PM: Hatch the hachures",
                                        "🔔 4:00 PM: Letter the cartouche", "🔔 4:30 PM: Shade the contours"])

    def test_a_routine_step_alone_gets_its_bell(self):
        self.add(EventSpan("rs", "Plot the graticule", at("17:40"), at("17:50"), SpanCategory.ACTIVITY,
                           source="routine"))
        self.ask("17:30", "17:35")
        self.assertEqual(self.bells(), ["🔔 5:30 PM: Plot the graticule"])

    def test_a_bell_isnt_skipped_because_the_user_wrote_twenty_minutes_before(self):
        mark_up(self.sched.state_db, at("15:40"))
        self.ask("16:00", "16:05")
        self.assertEqual(self.bells(), ["🔔 4:00 PM: Letter the cartouche"])

    def test_a_running_answer_delays_the_bell_to_its_end_within_the_thirty(self):
        turn = Turn(self.svc.manager().turn_lock)
        self.ask("16:00", "16:15")  # the answer is still being written
        self.assertEqual(self.bells(), [])
        turn.end()
        self.ask("16:20", "16:25")
        self.assertEqual(self.bells(), ["🔔 4:00 PM: Letter the cartouche"])

    def test_a_bell_still_waiting_when_its_thirty_ends_is_dropped(self):
        turn = Turn(self.svc.manager().turn_lock)
        self.ask("16:00", "16:25")
        turn.end()
        self.ask("16:30", "16:35")
        self.assertEqual(self.bells(), ["🔔 4:30 PM: Shade the contours"])  # the 4:00 PM one is over

    def test_an_item_whose_bell_was_held_isnt_marked_announced(self):
        turn = Turn(self.svc.manager().turn_lock)
        self.ask("16:00", "16:05")
        self.assertNotIn("ca", self.announced())
        turn.end()
        self.ask("16:10", "16:10")
        self.assertIn("ca", self.announced())  # told once it went out
        holds.start(self.sched.state_db, at("16:20"), at("16:33"), said="quiet please")
        self.ask("16:30", "16:30")
        self.assertNotIn("cn", self.announced())
        self.ask("16:35", "16:35")  # the quiet is over, still at the start of the thirty
        self.assertEqual(self.bells(), ["🔔 4:00 PM: Letter the cartouche", "🔔 4:30 PM: Shade the contours"])
        self.assertIn("cn", self.announced())

    def test_asleep_holds_the_bell(self):
        self.add(EventSpan("nap", "Sleep", at("14:50"), at("15:20"), SpanCategory.SLEEP))
        self.ask("15:00", "15:05")
        self.assertEqual([b for b in self.bells() if b.startswith("🔔")], [])
        self.assertNotIn("co", self.announced())

    def test_a_firm_calendar_event_holds_the_bell(self):
        self.add(EventSpan("ev", "Inlet tide talk", at("15:50"), at("16:20"), SpanCategory.CALENDAR, is_firm=True))
        self.ask("16:00", "16:05")
        self.assertEqual(self.bells(), [])
        self.assertNotIn("ca", self.announced())

    def test_a_quiet_the_user_asked_for_holds_the_bell(self):
        holds.start(self.sched.state_db, at("15:20"), at("16:50"), said="hold my messages")
        self.ask("15:30", "16:40")
        self.assertEqual(self.bells(), [])
        self.assertNotIn("ha", self.announced())
        self.assertNotIn("ca", self.announced())

    def test_start_notices_off_means_no_bells(self):
        self.sched.config.telegram.notify_block_transitions = False
        self.ask("14:50", "17:40")
        self.assertEqual(self.bells(), [])


class ReminderOnAnItem(Day):
    """At heavy, a reminder on a planned item rings as its bell; when the bell can't go out, as itself."""

    def remind(self, due, sid, title):
        from thirties_core.memory import ReminderStore
        ReminderStore(self.sched.state_db).add(at(due), title, span_id=sid)

    def both(self, t):
        self.svc.reminders(t)

    def kinds(self):
        return [(kind, body.split("\n")[0]) for kind, body in self.out]

    def test_a_reminder_waits_for_its_items_bell(self):
        self.remind("15:50", "ca", "Letter the cartouche")
        self.ask("15:50", "16:20", between=self.both)
        self.assertEqual(self.kinds(), [("start", "🔔 4:00 PM: Letter the cartouche")])

    def test_a_reminder_rings_itself_when_a_firm_event_holds_the_bell(self):
        self.add(EventSpan("ev", "Inlet tide talk", at("15:50"), at("16:20"), SpanCategory.CALENDAR, is_firm=True))
        self.remind("16:00", "ca", "Letter the cartouche")
        self.ask("16:00", "16:05", between=self.both)
        self.assertEqual(self.kinds(), [("reminder", "⏰ Reminder: Letter the cartouche")])

    def test_a_reminder_rings_itself_when_its_bell_waited_past_the_thirty(self):
        self.remind("16:00", "ca", "Letter the cartouche")
        turn = Turn(self.svc.manager().turn_lock)
        self.ask("16:00", "16:25", between=self.both)
        self.assertEqual(self.out, [])
        turn.end()
        self.ask("16:30", "16:30", between=self.both)
        self.assertEqual(self.kinds(), [("reminder", "⏰ Reminder: Letter the cartouche"),
                                        ("start", "🔔 4:30 PM: Shade the contours")])


class MediumStretchNotice(Day):
    """At medium, the start of each stretch of the day says what's planned in it."""

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.message_level = "medium"
        from thirties_core.background import day_stretches
        plan = self.svc.manager().day_plan
        self.a, self.b = next((a, b) for a, b in day_stretches(plan) if a <= at("16:10") < b)
        self.key = self.a.isoformat()

    def stretch(self, start_min, end_min):
        t = self.a + timedelta(minutes=start_min)
        while t <= self.a + timedelta(minutes=end_min):
            set_debug_time(t)
            self.svc.stretches(t)
            t += timedelta(minutes=5)

    def notices(self):
        return [body for kind, body in self.out if kind == "start"]

    def test_a_message_twenty_minutes_before_doesnt_swallow_it(self):
        mark_up(self.sched.state_db, self.a - timedelta(minutes=20))
        self.stretch(0, 5)
        self.assertEqual(len(self.notices()), 1, self.out)
        self.assertTrue(self.notices()[0].startswith("🗓 Until"), self.out)

    def test_it_waits_for_a_running_answer_and_isnt_counted_till_sent(self):
        turn = Turn(self.svc.manager().turn_lock)
        self.stretch(0, 10)
        self.assertEqual(self.notices(), [])
        self.assertNotIn(self.key, self.svc._remembered("stretch_started"))
        turn.end()
        self.stretch(15, 20)
        self.assertEqual(len(self.notices()), 1, self.out)
        self.assertIn(self.key, self.svc._remembered("stretch_started"))

    def test_a_quiet_the_user_asked_for_holds_it_without_counting_it(self):
        holds.start(self.sched.state_db, self.a - timedelta(minutes=10), self.a + timedelta(minutes=3),
                    said="quiet please")
        self.stretch(0, 0)
        self.assertEqual(self.notices(), [])
        self.assertNotIn(self.key, self.svc._remembered("stretch_started"))
        self.stretch(5, 5)  # the quiet ended, still at the stretch's start
        self.assertEqual(len(self.notices()), 1, self.out)


if __name__ == "__main__":
    unittest.main()
