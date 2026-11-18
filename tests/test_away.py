"""Quiet while the user is away (away.py): a bell or a check-in carries the Not marked yet list only when it
changed since it was last sent, and after two check-ins in a row with no reply, no more until the user writes again
(start notices still go out, short). Invented items, on the fake clock; DAY is Thu Oct 15 (Work 8:30 AM - 4 PM,
Sleep from 11 PM)."""

import unittest

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus


class AwayCase(ServiceCase):

    def kinds(self, kind):
        return [b for k, b in self.desktop if k == kind]

    def mark(self, sid, status=SpanStatus.DONE):
        span = next(s for s in self.sched.state_db.get_spans_between(at("07:00"), at("07:00", 1)) if s.id == sid)
        span.status = status
        self.sched.state_db.upsert_span(span)
        self.svc._manager = None

    def user_writes(self, when, text="the walk was lovely"):
        set_debug_time(at(when))
        self.svc.manager().send_user_message(text)  # the scripted model answers in plain text


class TestTheListOnlyWhenItChanged(AwayCase):

    def test_an_unchanged_list_isnt_sent_again_under_the_next_bell(self):
        self.add("old", "Oil the hinges", "16:00", "16:15")
        self.svc.starts(at("17:00"))
        self.assertIn("Not marked yet:\n4:00 PM – Oil the hinges", self.last("start"))
        self.mark("tx")
        self.mark("din")  # the plan changed, so the next bell says what's next again
        self.svc.starts(at("20:00"))
        bell = self.last("start")
        self.assertTrue(bell.startswith("🔔 8:00 PM: Film night"), bell)
        self.assertNotIn("Not marked yet", bell)  # the same one item as last time: not again
        self.assertNotIn("Oil the hinges", bell)

    def test_a_changed_list_is_sent_again_whole(self):
        self.add("old", "Oil the hinges", "16:00", "16:15")
        self.svc.starts(at("17:00"))
        self.mark("tx")
        self.mark("din")
        self.add("lamp", "Fix the lamp", "19:00", "19:15")  # fell behind since: the list changed
        self.svc.starts(at("20:00"))
        self.assertIn("Not marked yet:\n4:00 PM – Oil the hinges\n7:00 PM – Fix the lamp", self.last("start"))

    def test_a_reply_that_listed_it_counts_as_sent(self):
        from thirties_core.away import LastSentList
        self.add("old", "Oil the hinges", "16:00", "16:15")
        old = next(s for s in self.svc.manager().day_plan.spans if s.id == "old")
        LastSentList(self.sched.state_db).mark(at("16:50"), [old])  # the reply's what's next listed it
        self.svc.starts(at("17:00"))
        self.assertNotIn("Not marked yet", self.last("start"))


class TestTwoUnansweredCheckinsThenNone(AwayCase):

    def test_the_third_check_in_waits_until_the_user_writes(self):
        self.add("old", "Oil the hinges", "16:00", "16:15")
        self.svc.check_ins(at("16:45"))  # about Oil the hinges
        self.svc.check_ins(at("19:15"))  # about Sort the receipts and Dinner, back to back: one question
        self.assertEqual(len(self.kinds("checkin")), 2)
        self.svc.check_ins(at("21:45"))  # about Film night: two went unanswered
        self.assertEqual(len(self.kinds("checkin")), 2)
        self.add("tea", "Brew the tea", "22:00", "22:15")
        self.user_writes("21:50")  # back: check-ins start again
        self.svc.check_ins(at("22:40"))
        self.assertEqual(len(self.kinds("checkin")), 3)
        self.assertIn("Brew the tea", self.kinds("checkin")[-1])

    def test_start_notices_still_go_out_short(self):
        self.add("old", "Oil the hinges", "16:00", "16:15")
        self.svc.check_ins(at("16:45"))
        self.svc.check_ins(at("19:15"))
        self.svc.starts(at("20:00"))
        self.assertEqual(self.last("start"), "🔔 8:00 PM: Film night")  # no what's next, no list

    def test_a_reply_in_between_keeps_them_coming(self):
        self.user_writes("16:30")  # the day's first message builds the day: before the items it asks about
        self.svc.check_ins(at("19:15"))
        self.user_writes("19:20")
        self.svc.check_ins(at("21:45"))
        self.add("tea", "Brew the tea", "22:00", "22:15")
        self.user_writes("21:50")
        self.svc.check_ins(at("22:40"))
        self.assertEqual(len(self.kinds("checkin")), 3)  # never two in a row unanswered

    def test_the_rule_in_numbers(self):
        from thirties_core.away import MAX_UNANSWERED, UnansweredCheckins
        gate = UnansweredCheckins(self.sched.state_db)
        for _ in range(MAX_UNANSWERED):
            self.assertFalse(gate.paused())
            gate.sent()
        self.assertTrue(gate.paused())
        self.sched.state_db.set_sync_meta("last_user_message_at", at("20:00").isoformat())
        self.assertFalse(gate.paused())
        self.assertEqual(gate.count(), 0)


if __name__ == "__main__":
    unittest.main()
