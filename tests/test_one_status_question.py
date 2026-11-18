"""Ask about status once, batched: a run of back-to-back items gets one question when it's over, never one per
block per item; work's start and end are asked together, once, at work's end.

Invented items for someone who throws pots on a late shift and paddles a canoe; DAY is Thu Oct 15 (Work 8:30 AM -
4 PM)."""

import unittest

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core.background import back_to_back
from thirties_core.models import EventSpan, SpanCategory


class TestOneQuestionPerRun(ServiceCase):

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.message_level = "heavy"
        for sid, title, start, end in (("trim", "Trim the mugs", "16:00", "16:30"),
                                       ("wedge", "Wedge the clay", "16:30", "17:00")):
            self.add(sid, title, start, end)

    def checkins(self):
        return [b for k, b in self.desktop if k == "checkin"]

    def test_no_question_per_block(self):
        for t in ("16:45", "17:15", "17:45", "18:15", "18:45"):
            self.svc.check_ins(at(t))
        self.assertEqual(self.checkins(), [])  # the run (4:00 to 7:00) isn't over yet
        self.svc.check_ins(at("19:15"))
        self.svc.check_ins(at("19:20"))
        self.assertEqual(len(self.checkins()), 1, self.checkins())
        q = self.checkins()[0]
        for title in ("Trim the mugs", "Wedge the clay", "Sort the receipts", "Dinner"):
            self.assertIn(title, q)
        self.assertNotIn("still on", q)  # one batched question, not a single-item ask

    def test_runs(self):
        spans = [EventSpan("a", "A", at("10:00"), at("10:30"), SpanCategory.ACTIVITY),
                 EventSpan("b", "B", at("10:30"), at("11:15"), SpanCategory.ACTIVITY),
                 EventSpan("c", "C", at("13:00"), at("13:30"), SpanCategory.ACTIVITY)]
        self.assertEqual(back_to_back(spans), {"a": at("11:15"), "b": at("11:15"), "c": at("13:30")})


class TestWorkTimesTogether(ServiceCase):

    def test_one_question_for_start_and_end(self):
        from thirties_core.day_times import due_question
        plan = self.svc.manager().day_plan
        asked = {}
        morning = [due_question(plan, at(t), asked) for t in ("08:45", "09:00", "09:30", "10:00")]
        self.assertEqual(morning, [None] * 4)
        key, text = due_question(plan, at("16:00"), asked)
        self.assertIn("when did you start and finish?", text)
        self.assertIn("Done with work?", text)  # answered by the short-answer path as before


if __name__ == "__main__":
    unittest.main()
