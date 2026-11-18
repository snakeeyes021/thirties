"""Quiet while the user is away (away.py), for the evening catch-up and for check-ins that never reached the phone.

- The evening catch-up is a question too: after two unanswered check-ins it waits, like a check-in, until the
  user writes again. When it does go out, the list it carries counts as sent, so the next bell doesn't repeat it.
- A check-in counts as unanswered only once it reached the user: one that waited for the phone and was dropped
  (its moment passed) was never seen, so it never counts.

Invented items, on the fake clock; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest
from datetime import timedelta

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core.astronomy import set_debug_time
from thirties_core.away import UnansweredCheckins
from thirties_core.models import SpanStatus


class CatchUpCase(ServiceCase):
    def setUp(self):
        super().setUp()
        for sid, title, start in (("fern", "Water the ferns", "10:00"), ("mail", "Post the parcel", "10:30"),
                                  ("bike", "Pump the bike tyres", "16:00")):
            self.add(sid, title, start, (at(start) + timedelta(minutes=15)).strftime("%H:%M"))
        for sid in ("tx", "din"):
            span = next(s for s in self.svc.manager().day_plan.spans if s.id == sid)
            span.status = SpanStatus.DONE
            self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        set_debug_time(at("19:10"))

    def kinds(self, kind):
        return [b for k, b in self.desktop if k == kind]


class TestTheCatchUpWhileAway(CatchUpCase):

    def test_after_two_unanswered_check_ins_it_waits(self):
        gate = UnansweredCheckins(self.sched.state_db)
        gate.sent()
        gate.sent()
        self.assertTrue(gate.paused())
        self.svc.evening_catch_up(at("19:10"))
        self.assertEqual(self.kinds("catch_up"), [])
        from thirties_core import catch_up
        self.assertIsNone(catch_up.pending(self.svc.manager().tools, at("19:10")))  # never offered unseen

    def test_what_it_carried_isnt_listed_as_behind_under_the_next_bell(self):
        self.svc.evening_catch_up(at("19:10"))
        self.assertIn("Water the ferns", self.kinds("catch_up")[-1])
        self.svc.starts(at("19:30"))
        bell = self.kinds("start")[-1]
        self.assertNotIn("Not marked yet", bell)  # they're on tonight now, not behind


class TestACheckInThatNeverArrived(ServiceCase):

    def test_dropped_undelivered_it_doesnt_count(self):
        self.svc.bridge.broadcast.return_value = False  # the phone is out of reach
        self.svc.check_ins(at("18:15"))
        gate = UnansweredCheckins(self.sched.state_db)
        self.assertEqual(gate.count(), 0)  # waiting for the phone: not seen yet
        self.assertTrue(self.svc.dropped_stale("Just checking in"))  # its moment passed: dropped
        self.assertEqual(gate.count(), 0)

    def test_delivered_late_it_counts(self):
        self.svc.bridge.broadcast.return_value = False
        self.svc.check_ins(at("18:15"))
        self.assertTrue(self.svc.delivered_late("Just checking in"))
        self.assertEqual(UnansweredCheckins(self.sched.state_db).count(), 1)

    def test_delivered_at_once_it_counts(self):
        self.svc.check_ins(at("18:15"))
        self.assertEqual(UnansweredCheckins(self.sched.state_db).count(), 1)


if __name__ == "__main__":
    unittest.main()
