"""Deadlines that come round again (deadlines.py): a missed one moves to the next, and its work is planned back
from there, sized to how much it takes. Invented items throughout."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
from thirties_core.deadlines import add_months, buffer_days, next_deadline, parse_lead_days
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

    def test_a_lead_in_days(self):
        self.assertEqual(parse_lead_days("a month"), 30)
        self.assertEqual(parse_lead_days("2 weeks"), 14)
        self.assertEqual(parse_lead_days("10 days before"), 10)
        with self.assertRaises(ValueError):
            parse_lead_days("soonish")


class RollTest(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.services.tasks.append(TaskItem(id="t_permit", source_notebook="1. Tasks", title="Renew the parking permit",
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands", due=date(2026, 9, 30).isoformat()))

    def facts(self, **kw):
        return self.t.execute("set_item_facts", {"event": "Renew the parking permit", "repeats": "quarterly", **kw})

    def pinned(self):
        from thirties_core.budgets import budget_period
        hp = HorizonPlanner(self.t)
        return [(budget_period(r, hp.week_start).level, int(r["sessions"]), int(r["minutes"]))
                for r in hp.plans.rows(item_id="t_permit")]

    def test_a_missed_repeating_deadline_moves_on_and_one_session_holds_its_place(self):
        """No word yet on how much work it needs: one session, and the model (only) is asked for an estimate."""
        from thirties_core.carry import settle
        from thirties_core.lists import ListCatalog
        self.assertIn("its deadline comes round quarterly", self.facts())
        moved = settle(self.t.assistant, DAY)["deadlines_moved"]
        self.assertEqual([(m["title"], m["due"]) for m in moved], [("Renew the parking permit", "2026-12-31")])
        self.assertEqual(ListCatalog(self.sched.state_db).urgencies()["t_permit"]["due"], "2026-12-31")  # stored
        self.assertEqual(self.pinned(), [("quarter", 1, 60)])  # the period that holds the time before it
        self.assertEqual(moved[0]["plan"]["by"], "2026-12-26")  # a few days' buffer, more for the miss
        brief = self.t.assistant.leftover_brief()
        self.assertIn("WORK TO ESTIMATE", brief)
        self.assertIn("Renew the parking permit", brief)

    def test_the_work_is_planned_back_sized_to_what_it_takes(self):
        from thirties_core.carry import settle
        self.facts(effort="12 hours")
        settle(self.t.assistant, DAY)
        self.assertEqual(self.pinned(), [("quarter", 12, 60)])
        self.assertNotIn("WORK TO ESTIMATE", self.t.assistant.leftover_brief())  # the user said

    def test_a_big_effort_close_to_its_deadline_gets_denser_sessions(self):
        from thirties_core.carry import settle
        self.facts(effort="20 hours", lead="1 week")  # a week before the buffer: Dec 19 to Dec 26
        plan = settle(self.t.assistant, DAY)["deadlines_moved"][0]["plan"]
        self.assertEqual((plan["from"], plan["by"]), ("2026-12-19", "2026-12-26"))
        self.assertEqual(self.pinned(), [("month", 8, 150)])  # 20 hours over 8 days: sessions of 2.5 hours

    def test_a_small_effort_is_one_or_two_sessions(self):
        from thirties_core.carry import settle
        self.facts(effort="2 sessions")
        settle(self.t.assistant, DAY)
        self.assertEqual(self.pinned(), [("quarter", 2, 60)])

    def test_the_models_estimate_is_shown_as_one_and_plans_the_work_again(self):
        from thirties_core.carry import settle
        self.facts()
        settle(self.t.assistant, DAY)
        out = self.t.execute("set_item_facts", {"event": "Renew the parking permit", "effort": "6 hours",
                                                "effort_is_estimate": True})
        self.assertIn("about 6h of work for each deadline (an estimate)", out)
        self.assertIn("6 sessions of 1h before Dec 26 (from an estimate).", out)
        self.assertEqual(self.pinned(), [("quarter", 6, 60)])
        out = self.t.execute("set_item_facts", {"event": "Renew the parking permit", "effort": "4 hours"})
        self.assertNotIn("an estimate", out)  # the user's word replaces it

    def test_the_buffer_grows_with_each_miss(self):
        from thirties_core.carry import settle
        self.assertEqual(buffer_days(self.sched.state_db, "t_permit"), 3)
        self.facts()
        settle(self.t.assistant, DAY)
        self.assertEqual(buffer_days(self.sched.state_db, "t_permit"), 5)

    def test_the_budget_stops_at_the_buffer(self):
        from thirties_core.budgets import _due
        from thirties_core.carry import settle
        self.facts()
        settle(self.t.assistant, DAY)
        task = next(t for t in self.services.tasks if t.id == "t_permit")
        self.assertEqual(_due(task, HorizonPlanner(self.t)), date(2026, 12, 26))
        self.assertEqual(_due(task), date(2026, 12, 31))  # without the planner: the deadline itself

    def test_the_morning_says_where_it_went(self):
        self.facts(effort="3 hours")
        said = self.t.execute("rollover_proposal", {})
        self.assertIn("Renew the parking permit: next deadline Dec 31; 3 sessions of 1h before Dec 26.", said)
        self.assertNotIn("[", said)  # nothing meant for the model reaches this text

    def test_a_series_keeps_its_own_day_through_a_short_month(self):
        """Aug 30 quarterly: Nov 30 (November's last day), then Feb 28, then May 30 (not May 31)."""
        from thirties_core.carry import settle
        task = next(t for t in self.services.tasks if t.id == "t_permit")
        task.due = date(2026, 8, 30).isoformat()
        self.facts()
        rolled = []
        for day in (DAY, date(2026, 12, 1), date(2027, 3, 1)):
            moved = settle(self.t.assistant, day)["deadlines_moved"]
            rolled += [m["due"] for m in moved]
        self.assertEqual(rolled, ["2026-11-30", "2027-02-28", "2027-05-30"])

    def test_a_new_deadline_from_the_user_restarts_the_series_on_its_day(self):
        from thirties_core.carry import settle
        task = next(t for t in self.services.tasks if t.id == "t_permit")
        task.due = date(2026, 8, 30).isoformat()
        self.facts()
        settle(self.t.assistant, DAY)  # rolled once: the series keeps the 30th
        out = self.t.execute("set_urgency", {"task": task.title, "due": "2027-01-15"})
        self.assertNotIn("NOT_DONE", out)
        moved = settle(self.t.assistant, date(2027, 1, 16))["deadlines_moved"]
        self.assertEqual([m["due"] for m in moved], ["2027-04-15"])  # the user's new day, not the old 30th

    def test_a_month_end_series_stays_on_month_ends(self):
        from thirties_core.deadlines import next_deadline, series_day
        self.assertEqual(next_deadline(date(2026, 9, 30), "quarterly", DAY, series_day(date(2026, 9, 30))),
                         date(2026, 12, 31))
        self.assertEqual(next_deadline(date(2027, 2, 28), "monthly", date(2027, 3, 1), 30), date(2027, 3, 30))

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
