"""Status is asked about once: one question at the end of work covers both of work's times, and the evening
catch-up holds off until every item in a back-to-back run has ended.

Invented items for someone who throws pots on a late shift and paddles a canoe; DAY is Thu Oct 15 (Work 8:30 AM -
4 PM)."""

import unittest

from tests.fixtures import at
from tests.test_schedule_reach import ServiceCase
from thirties_core.background import back_to_back
from thirties_core.models import EventSpan, SpanCategory


class TestRuns(ServiceCase):

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
