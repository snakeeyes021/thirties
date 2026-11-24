"""A check-in for each item as the thirty it ends in is over; one catch-up only for what was really missed. A check-in
is counted as asked only once it went out: a recent message never swallows it, an answer under way delays it, and
one held back is left for the evening.

Invented items at a map maker's desk; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM in the fixture)."""

import threading
import unittest
from datetime import timedelta

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core import holds
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory

ITEMS = (("sh", "Plot the shoal", "09:10", "09:20"),
         ("ct", "Letter the cartouche", "09:20", "09:50"),
         ("cn", "Shade the contours", "09:50", "10:20"))


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


class MapCase(ServiceCase):

    def setUp(self):
        super().setUp()
        for sid, title, start, end in ITEMS:
            self.add(sid, title, start, end)

    def tick(self, hhmm):
        set_debug_time(at(hhmm))
        self.svc.check_ins(at(hhmm))

    def answers(self, hhmm):  # the user writes back
        self.sched.state_db.set_sync_meta("last_user_message_at", at(hhmm).isoformat())

    def checkins(self):
        return [b.split("\n\n")[0] for k, b in self.desktop if k == "checkin"]

    def asked(self):
        return self.svc._remembered("checkins_asked")


class TestEachItemAsItEnds(MapCase):

    def test_three_short_items_one_after_another_each_get_their_own(self):
        self.tick("09:40")
        self.answers("09:40")
        self.tick("10:10")
        self.answers("10:10")
        self.tick("10:40")
        asked = self.checkins()
        self.assertEqual(len(asked), 3, asked)
        for question, (_, title, _, _) in zip(asked, ITEMS):
            self.assertIn(f"still on {title}" if title != ITEMS[-1][1] else f"did you get to {title}", question)
            self.assertNotIn("How did it go", question)

    def test_a_message_twenty_minutes_before_doesnt_swallow_it(self):
        self.answers("09:20")
        self.tick("09:40")
        self.assertEqual(len(self.checkins()), 1, self.desktop)
        self.assertIn("Plot the shoal", self.checkins()[0])


class TestHeldCheckIns(MapCase):

    def test_an_answer_under_way_delays_it_to_the_answers_end(self):
        turn = Turn(self.svc.manager().turn_lock)
        for t in ("09:40", "09:45", "09:50", "09:55"):
            self.tick(t)
        self.assertEqual(self.checkins(), [])
        self.assertNotIn("sh", self.asked())  # waiting: not asked yet
        turn.end()
        self.tick("10:00")  # past its ten minutes, but it waited for the answer
        self.assertEqual(len(self.checkins()), 1, self.desktop)
        self.assertIn("Plot the shoal", self.checkins()[0])
        self.assertIn("sh", self.asked())

    def test_still_waiting_half_an_hour_on_it_is_left_for_the_evening(self):
        turn = Turn(self.svc.manager().turn_lock)
        for t in ("09:40", "09:50", "10:00"):
            self.tick(t)
        turn.end()
        self.tick("10:10")  # the next item's own time: the first one's half hour is over
        self.assertEqual(len(self.checkins()), 1, self.desktop)
        self.assertNotIn("Plot the shoal", self.checkins()[0])
        self.assertNotIn("sh", self.asked())  # never asked: the evening catch-up may carry it

    def test_a_quiet_the_user_asked_for_holds_it_without_counting_it(self):
        holds.start(self.sched.state_db, at("09:20"), at("09:45"), said="quiet please")
        self.tick("09:40")
        self.assertEqual(self.checkins(), [])
        self.assertNotIn("sh", self.asked())
        self.tick("09:45")  # the quiet is over, within its ten minutes
        self.assertEqual(len(self.checkins()), 1, self.desktop)
        self.assertIn("sh", self.asked())

    def test_asleep_or_in_a_firm_event_holds_it_without_counting_it(self):
        self.sched.state_db.upsert_span(EventSpan("ev", "Inlet tide talk", at("09:30"), at("10:00"),
                                                  SpanCategory.CALENDAR, is_firm=True))
        self.svc._manager = None
        self.tick("09:40")
        self.assertEqual(self.checkins(), [])
        self.assertNotIn("sh", self.asked())


class TestOneCatchUpForWhatWasMissed(MapCase):

    def test_a_long_stretch_with_no_check_ins_gets_one_catch_up(self):
        self.tick("11:20")  # back after the whole morning closed: every item's moment has passed
        self.assertEqual(self.checkins(), [])  # never sent late, one by one
        set_debug_time(at("18:40"))
        self.svc.evening_catch_up(at("18:40"))
        asked = self.checkins()
        self.assertEqual(len(asked), 1, asked)
        self.assertTrue(asked[0].startswith("👋 How did it go?"), asked[0])
        for _, title, _, _ in ITEMS:
            self.assertIn(title, asked[0])


class TestMediumAsksOnlyOnceItWentOut(MapCase):
    """Medium: one question at the end of each stretch and one before bed, each counted only once it went out."""

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.message_level = "medium"
        from thirties_core.background import day_stretches
        plan = self.svc.manager().day_plan
        self.a, self.b = next((a, b) for a, b in day_stretches(plan) if a <= at("17:10") < b)
        self.add("pl", "Plot the graticule", "17:10", "17:40")
        self.ask_at = self.b + timedelta(minutes=self.sched.config.assistant.checkin_after_minutes)

    def stretch(self, after_min):
        t = self.ask_at + timedelta(minutes=after_min)
        set_debug_time(t)
        self.svc.stretches(t)

    def stretch_questions(self):
        return [b for b in self.checkins() if b.startswith("👋 Since")]

    def test_talking_during_the_stretch_doesnt_count_as_asked(self):
        self.sched.state_db.set_sync_meta("last_user_message_at", (self.a + timedelta(minutes=40)).isoformat())
        self.stretch(0)
        self.assertEqual(len(self.stretch_questions()), 1, self.desktop)
        self.assertIn("Plot the graticule", self.stretch_questions()[0])

    def test_an_answer_under_way_delays_it(self):
        turn = Turn(self.svc.manager().turn_lock)
        self.stretch(0)
        self.assertEqual(self.stretch_questions(), [])
        self.assertNotIn(self.a.isoformat(), self.svc._remembered("stretch_asked"))
        turn.end()
        self.stretch(10)
        self.assertEqual(len(self.stretch_questions()), 1, self.desktop)

    def test_before_bed_waits_out_a_quiet_and_an_answer(self):
        plan = self.svc.manager().day_plan
        bed = min(s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start)
        key = f"wrap_up:{plan.target_date.isoformat()}"
        first = bed - timedelta(minutes=40)
        holds.start(self.sched.state_db, first - timedelta(minutes=10), first + timedelta(minutes=5), said="quiet please")
        set_debug_time(first)
        self.svc.wrap_up(first)
        turn = Turn(self.svc.manager().turn_lock)
        set_debug_time(first + timedelta(minutes=10))
        self.svc.wrap_up(first + timedelta(minutes=10))
        self.assertEqual([b for b in self.checkins() if b.startswith("🌙 Before bed")], [])
        self.assertFalse(self.sched.state_db.get_sync_meta(key))
        turn.end()
        set_debug_time(first + timedelta(minutes=20))
        self.svc.wrap_up(first + timedelta(minutes=20))
        wrap = [b for b in self.checkins() if b.startswith("🌙 Before bed")]
        self.assertEqual(len(wrap), 1, self.desktop)
        self.assertIn("Plot the graticule", wrap[0])
        self.assertTrue(self.sched.state_db.get_sync_meta(key))


if __name__ == "__main__":
    unittest.main()
