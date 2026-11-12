"""Deadlines that come round again (deadlines.py): a missed one moves to the next, with a prep week before it.
Invented items throughout."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
from thirties_core.deadlines import PREP_SESSIONS, add_months, next_deadline, prep_start, prep_week
from thirties_core.horizons import HorizonPlanner, period_of
from thirties_core.models import TaskItem


class ArithmeticTest(unittest.TestCase):
    def test_months_keep_to_the_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 9, 30), 3), date(2026, 12, 31))  # a quarter's end stays its end
        self.assertEqual(add_months(date(2026, 10, 15), 3), date(2027, 1, 15))

    def test_the_next_deadline_after_a_missed_one(self):
        self.assertEqual(next_deadline(date(2026, 9, 30), "quarterly", DAY), date(2026, 12, 31))
        self.assertEqual(next_deadline(date(2026, 3, 31), "quarterly", DAY), date(2026, 12, 31))  # two missed
        self.assertEqual(next_deadline(date(2026, 10, 1), "monthly", DAY), date(2026, 11, 1))
        self.assertEqual(next_deadline(date(2025, 10, 10), "yearly", DAY), date(2026, 10, 10) + timedelta(days=365))
        self.assertIsNone(next_deadline(date(2026, 9, 30), "", DAY))

    def test_preparing_starts_a_month_before_or_a_third_of_the_period(self):
        self.assertEqual(prep_start(date(2026, 12, 31), "quarterly"), date(2026, 12, 1))  # a month
        self.assertEqual(prep_start(date(2027, 10, 15), "yearly"), date(2027, 9, 15))    # a month
        self.assertEqual(prep_start(date(2026, 11, 30), "monthly"), date(2026, 11, 20))  # a third of 31 days: 10
        week = prep_week(date(2026, 12, 31), DAY)
        self.assertTrue(week.contains(date(2026, 12, 1)))  # the week preparing starts in
        self.assertGreater((week.start - DAY).days, 40)  # about two months after a quarter's missed deadline
        self.assertEqual(prep_week(date(2026, 11, 1), DAY), period_of("week", DAY))  # already begun: this week


class RollTest(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.services.tasks.append(TaskItem(id="t_permit", source_notebook="1. Tasks", title="Renew the parking permit",
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands", due=date(2026, 9, 30).isoformat()))

    def test_a_missed_repeating_deadline_moves_on_with_a_prep_week(self):
        from thirties_core.carry import settle
        from thirties_core.lists import ListCatalog
        out = self.t.execute("set_item_facts", {"event": "Renew the parking permit", "repeats": "quarterly"})
        self.assertIn("its deadline comes round quarterly", out)
        left = settle(self.t.assistant, DAY)
        moved = left["deadlines_moved"]
        self.assertEqual([(m["title"], m["due"]) for m in moved], [("Renew the parking permit", "2026-12-31")])
        self.assertEqual(ListCatalog(self.sched.state_db).urgencies()["t_permit"]["due"], "2026-12-31")  # stored
        hp = HorizonPlanner(self.t)
        week = prep_week(date(2026, 12, 31), DAY)
        rows = [r for r in hp.plans.in_period(week) if r["item_id"] == "t_permit"]
        self.assertEqual(int(rows[0]["sessions"]), PREP_SESSIONS)  # an intense week, before the deadline

    def test_the_morning_says_where_it_went(self):
        self.t.execute("set_item_facts", {"event": "Renew the parking permit", "repeats": "quarterly"})
        said = self.t.execute("rollover_proposal", {})
        self.assertIn("Renew the parking permit: next deadline Dec 31; a prep week from ", said)

    def test_without_a_repeat_a_missed_deadline_is_left_alone(self):
        from thirties_core.carry import settle
        left = settle(self.t.assistant, DAY)
        self.assertEqual(left.get("deadlines_moved"), [])

    def test_a_mark_in_the_items_own_text_counts(self):
        from thirties_core.carry import settle
        from thirties_core.deadlines import marked_repeat
        self.assertEqual(marked_repeat("Renew the parking permit (quarterly)"), "quarterly")
        self.assertEqual(marked_repeat("Quarterly-ish chores"), "")  # only the explicit mark, never the wording
        task = next(t for t in self.services.tasks if t.id == "t_permit")
        task.title = "Renew the parking permit (quarterly)"
        moved = settle(self.t.assistant, DAY)["deadlines_moved"]
        self.assertEqual([m["due"] for m in moved], ["2026-12-31"])

    def test_the_miss_is_recorded_for_the_statistics(self):
        import json
        from thirties_core.carry import settle
        from thirties_core.deadlines import MISSED_KEY
        self.t.execute("set_item_facts", {"event": "Renew the parking permit", "repeats": "quarterly"})
        settle(self.t.assistant, DAY)
        missed = json.loads(self.sched.state_db.get_sync_meta(MISSED_KEY))
        self.assertEqual([(m["item"], m["due"]) for m in missed], [("t_permit", "2026-09-30")])
        self.assertEqual(self.sched.state_db.get_task_history("t_permit").deferred_count, 1)

    def test_repeats_is_only_what_the_user_said(self):
        out = self.t.execute("set_item_facts", {"event": "Renew the parking permit", "repeats": "fortnightly"})
        self.assertTrue(out.startswith("NOT_DONE"), out)


if __name__ == "__main__":
    unittest.main()
