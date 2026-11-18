"""The evening status question asks only about one-off items, and never calls an unmarked item "not done".

Some things never roll: meals, routines and their steps, appointments, and anything tied to a time that has
passed. An unmarked item's outcome is unknown. Invented items for someone who throws pots on a late shift at a
studio and paddles a canoe on days off; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep from 11 PM)."""

import unittest
from datetime import timedelta

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core import catch_up
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


class TestOnlyWhatRolls(ServiceCase):

    def setUp(self):
        super().setUp()
        db = self.sched.state_db
        for sid in ("tx", "din"):
            span = next(s for s in self.svc.manager().day_plan.spans if s.id == sid)
            span.status = SpanStatus.DONE
            db.upsert_span(span)
        # two one-offs with no time of their own: these may go on tonight
        self.add("tile", "Sort the test tiles", "10:00", "10:15")
        self.add("seat", "Patch the canoe seat", "10:30", "10:45")
        # none of these ever rolls
        self.add("brunch", "Brunch", "11:30", "12:00")
        db.upsert_span(EventSpan("step", "Stretch the shoulders", at("11:00"), at("11:15"), SpanCategory.ACTIVITY,
                                 source="routine"))
        db.upsert_span(EventSpan("kiln", "Kiln delivery", at("13:00"), at("13:30"), SpanCategory.CALENDAR,
                                 source="calendar"))
        db.upsert_span(EventSpan("call", "Call the outfitter", at("14:00"), at("14:15"), SpanCategory.ACTIVITY,
                                 given_start=at("14:00")))
        self.svc._manager = None
        set_debug_time(at("19:10"))

    def test_meals_routines_appointments_and_set_times_stay_out(self):
        tools = self.svc.manager().tools
        from thirties_core.messages import missed_for
        carried = [s.id for s in catch_up.behind(tools, missed_for(tools, at("19:10")))]
        self.assertEqual(carried, ["tile", "seat"])

    def test_the_message_carries_only_those(self):
        self.svc.evening_catch_up(at("19:10"))
        text = self.last("checkin")
        self.assertIn("Sort the test tiles", text)
        self.assertIn("Patch the canoe seat", text)
        for title in ("Brunch", "Stretch the shoulders", "Kiln delivery", "Call the outfitter"):
            self.assertNotIn(title, text)
        self.assertNotIn("not done", text.lower())  # unmarked is unknown, never "not done"

    def test_nothing_but_meals_and_routines_behind_says_nothing(self):
        db = self.sched.state_db
        for sid in ("tile", "seat"):
            span = next(s for s in self.svc.manager().day_plan.spans if s.id == sid)
            span.status = SpanStatus.DONE
            db.upsert_span(span)
        self.add("sup", "Supper", "14:30", "15:00")
        self.svc._manager = None
        self.svc.evening_catch_up(at("19:10"))
        self.assertEqual([k for k, _ in self.desktop if k == "checkin"], [])

    def test_nothing_is_marked_by_it(self):
        self.svc.evening_catch_up(at("19:10"))
        by_id = {s.id: s for s in self.svc.manager().day_plan.spans}
        for sid in ("brunch", "step", "call", "tile", "seat"):
            self.assertIn(by_id[sid].status, (SpanStatus.PLANNED, SpanStatus.UNKNOWN))  # unknown: never skipped
            self.assertLess(by_id[sid].start_dt, at("15:00"))  # and never moved


if __name__ == "__main__":
    unittest.main()
