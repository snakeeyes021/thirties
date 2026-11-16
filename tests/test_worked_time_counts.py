"""Time worked on an item counts, even when the item isn't finished: a work session (or a line unticked by hand
after it was marked done) leaves the day's item partly done, and that time goes toward the item's budget, its
deadline's remaining work and the month's summary, the same as a finished session. Invented items throughout."""

from datetime import date, timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
import tests.test_period_summary as summary_tests
from thirties_core.budgets import budget_of
from thirties_core.deadlines import _done_minutes
from thirties_core.horizons import HorizonPlanner
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.period_summary import month_summary


class TestBudgetsAndDeadlines(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.services.tasks.append(TaskItem(id="t_shed", source_notebook="1. Tasks", title="Shed clear-out",
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands"))
        self.t.execute("set_item_facts", {"event": "Shed clear-out", "kind": "ongoing"})
        self.hp = HorizonPlanner(self.t)
        self.task = self.hp.find_task("Shed clear-out")

    def session(self, offset, status=None):
        day = (DAY + timedelta(days=offset)).isoformat()
        self.t.execute("add_event", {"title": "Shed clear-out", "start": "6:00 PM", "duration": "1 hour", "day": day})
        if status is not None:
            s = next(s for s in self.t.plan_for(day).spans if s.title == "Shed clear-out")
            s.status = status
            self.sched.state_db.upsert_span(s)

    def test_a_partly_done_session_counts_toward_the_budget(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3})
        self.session(-2, status=SpanStatus.PARTIAL)  # Tuesday: worked on it, not finished
        self.session(-1, status=SpanStatus.DONE)     # Wednesday: done
        self.session(1)                              # Friday: planned
        b = budget_of(self.hp, self.task, DAY)
        self.assertEqual((b.done, b.planned, b.left), (2, 1, 0))

    def test_a_partly_done_session_counts_toward_a_deadlines_work(self):
        self.session(-2, status=SpanStatus.PARTIAL)
        self.session(-1, status=SpanStatus.DONE)
        self.session(-3, status=SpanStatus.SKIPPED)  # skipped: no time on it
        self.assertEqual(_done_minutes(self.t, "Shed clear-out", DAY - timedelta(days=3), DAY), 120)


class TestTheMonthsSummary(summary_tests.Fixture):

    def test_a_partly_done_session_counts_as_a_session_worked(self):
        self.pin("t_shed", "Shed clear-out", "month", DAY, sessions=4, minutes=90)
        self.span("Shed clear-out", date(2026, 10, 3), status=SpanStatus.DONE)
        self.span("Shed clear-out", date(2026, 10, 10), status=SpanStatus.PARTIAL)
        self.span("Shed clear-out", date(2026, 10, 20))
        shed = month_summary(DAY, self.source(), DAY).efforts[0]
        self.assertEqual((shed.done, shed.planned, shed.left), (2, 1, 1))
        self.assertEqual(shed.status, "scheduled")  # time on it isn't finishing it


if __name__ == "__main__":
    import unittest
    unittest.main()
