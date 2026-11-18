"""Unmarked is unknown: the evening asks once, in the batched status question, and moves nothing by itself. The
user's answer is their report: what happened is recorded; what didn't goes back to its list, fitted where it makes
sense (tonight only when it can't wait and there's room, otherwise a day later this week), said as a notice.

A mock model plays the assistant. Invented items for someone who throws pots on a late shift and paddles a canoe;
DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_conversation import reply
from tests.test_schedule_reach import ServiceCase
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import WEEK, OpenPool
from thirties_core.models import SpanStatus


class TestTheEveningAnswer(ServiceCase):

    def setUp(self):
        super().setUp()
        for sid, title, start in (("tile", "Sort the test tiles", "10:00"), ("seat", "Patch the canoe seat", "10:30"),
                                  ("rent", "Return the rental paddles asap", "11:00")):
            self.add(sid, title, start, (at(start) + timedelta(minutes=15)).strftime("%H:%M"))
        for sid in ("tx", "din"):
            span = next(s for s in self.svc.manager().day_plan.spans if s.id == sid)
            span.status = SpanStatus.DONE
            self.sched.state_db.upsert_span(span)
        self.svc._manager = None
        from thirties_core import morning
        morning.mark_done(self.sched.state_db, DAY, at("08:00"))
        set_debug_time(at("19:10"))
        self.engine = self.svc.inference_engine

    def by_id(self):
        return {s.id: s for s in self.svc.manager().day_plan.spans}

    def test_the_evening_asks_and_moves_nothing(self):
        before = {k: (s.start_dt, s.status) for k, s in self.by_id().items()}
        self.svc.evening_catch_up(at("19:10"))
        asked = [b for k, b in self.desktop if k == "checkin"]
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0].startswith("👋 How did it go? Did you get to Sort the test tiles, Patch the canoe "
                                            "seat and Return the rental paddles asap?"), asked[0])
        after = {k: (s.start_dt, s.status) for k, s in self.by_id().items()}
        self.assertEqual(after, before)  # nothing moved, nothing marked

    def test_the_answer_is_acted_on(self):
        self.svc.evening_catch_up(at("19:10"))
        for r in ('CALL log_outcome {"event": "tile", "status": "skipped"}',
                  'CALL log_outcome {"event": "seat", "status": "done"}',
                  'CALL log_outcome {"event": "rent", "status": "skipped"}'):
            self.engine.set_next_response(reply(r))
        for _ in range(3):
            self.engine.set_next_response(reply("The seat's recorded; the tiles wait for later this week, and the "
                                                "paddles are on tonight."))
        self.svc.manager().send_user_message("the test tiles didn't happen, the canoe seat did, the rental paddles "
                                             "didn't", via="telegram")
        spans = self.by_id()
        self.assertEqual(spans["seat"].status, SpanStatus.DONE)  # what happened is recorded
        self.assertEqual(spans["tile"].status, SpanStatus.SKIPPED)
        planned = [s for s in spans.values() if s.status == SpanStatus.PLANNED]
        self.assertFalse([s for s in planned if s.title == "Sort the test tiles"])  # not pushed onto tonight
        pool = OpenPool(self.sched.state_db).all(DAY)
        self.assertEqual(pool["sort the test tiles"]["horizon"], WEEK)  # back to its list, for this week
        tonight = [s for s in planned if s.title == "Return the rental paddles asap"]
        self.assertEqual(len(tonight), 1)  # it can't wait and there's room tonight
        self.assertGreaterEqual(tonight[0].start_dt, at("19:10"))
        results = [str(m.get("content")) for m in self.svc.manager().brain.messages if m.get("role") == "tool"]
        # a later day this week, never tomorrow by itself (not_today.py)
        self.assertTrue(any("goes back to its list, for Saturday, later this week" in r for r in results), results)
        self.assertTrue(any("can't wait, so it's on tonight at" in r for r in results), results)


if __name__ == "__main__":
    unittest.main()
