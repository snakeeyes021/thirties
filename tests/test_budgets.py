"""Effort budgets (budgets.py): sessions per period, what's left, a level's share for the next one down, and
missed time re-spread before the end of its budgeted period. Invented items throughout."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY
from tests.test_today import TodayTestCase
from thirties_core.budgets import budget_of, periods_left, share_for, share_this
from thirties_core.horizons import HorizonPlanner, HorizonPlans, period_of
from thirties_core.models import SpanStatus, TaskItem

WEEK = period_of("week", DAY)     # Mon Oct 12 - Sun Oct 18 2026; DAY is Thursday
MONTH = period_of("month", DAY)


class ArithmeticTest(unittest.TestCase):
    def test_a_share_is_whats_left_spread_evenly_rounded_up(self):
        self.assertEqual(share_for(6, 2), 3)
        self.assertEqual(share_for(5, 2), 3)   # front-loaded, never left to the last period
        self.assertEqual(share_for(0, 3), 0)
        self.assertEqual(share_for(4, 0), 0)

    def test_the_weeks_left_in_a_month(self):
        weeks = periods_left(MONTH, "week", DAY)
        self.assertEqual(weeks[0], WEEK)  # this week counts: it isn't over
        self.assertTrue(all(w.end >= DAY for w in weeks))
        self.assertEqual(weeks[-1].start, date(2026, 10, 26))  # the last week that starts in October


class BudgetTest(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.services.tasks.append(TaskItem(id="t_shed", source_notebook="1. Tasks", title="Shed clear-out",
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands"))
        self.t.execute("set_item_facts", {"event": "Shed clear-out", "kind": "ongoing"})
        self.hp = HorizonPlanner(self.t)
        self.task = self.hp.find_task("Shed clear-out")

    def session(self, offset, start="6:00 PM", status=None):
        day = (DAY + timedelta(days=offset)).isoformat()
        self.t.execute("add_event", {"title": "Shed clear-out", "start": start, "duration": "1 hour", "day": day})
        if status is not None:
            plan = self.t.plan_for(day)
            s = next(s for s in plan.spans if s.title == "Shed clear-out")
            s.status = status
            self.sched.state_db.upsert_span(s)

    def test_a_pin_carries_a_budget(self):
        out = self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": "3",
                                               "length": "1 hour"})
        self.assertIn("3 sessions of 60 min", out)
        b = budget_of(self.hp, self.task, DAY)
        self.assertEqual((b.period, b.sessions, b.minutes, b.left), (WEEK, 3, 60, 3))

    def test_whats_left_counts_done_and_planned_sessions(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3})
        self.session(-2, status=SpanStatus.DONE)   # Tuesday: done
        self.session(1)                             # Friday: planned
        b = budget_of(self.hp, self.task, DAY)
        self.assertEqual((b.done, b.planned, b.left), (1, 1, 1))

    def test_a_day_takes_no_budget(self):
        out = self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "tomorrow", "sessions": 2})
        self.assertTrue(out.startswith("NOT_DONE") or "not a day" in out, out)

    def test_a_months_budget_gives_this_week_its_share(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this month", "sessions": 6})
        n = len(periods_left(MONTH, "week", DAY))
        self.assertEqual(share_this(self.hp, self.task, WEEK), share_for(6, n))

    def test_a_budget_syncs_and_follows_old_databases(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 2})
        plans = HorizonPlans(self.sched.state_db)
        rec = next(r for r in plans.all_records() if r["item_id"] == "t_shed")
        rec = {**rec, "sessions": 4, "updated_at": "2099-01-01T00:00:00+00:00"}
        self.assertTrue(plans.merge_record(rec))
        self.assertEqual(budget_of(self.hp, self.task, DAY).sessions, 4)
        HorizonPlans(self.sched.state_db)  # opening again leaves the columns alone

    def test_missed_budgeted_time_is_respread_within_the_week(self):
        from thirties_core.carry import settle
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3})
        self.session(-1, start="3:00 PM")   # yesterday: missed (no outcome)
        self.session(1)                     # Friday's session
        out = settle(self.t.assistant, DAY)
        spread = {r["title"]: r for r in out["respread"]}
        self.assertIn("Shed clear-out", spread, out)
        self.assertNotIn("let", spread["Shed clear-out"]["why"])

    def test_with_no_room_left_this_week_its_let_go_to_the_month(self):
        from thirties_core.carry import settle
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3})
        self.session(-1, start="3:00 PM")
        for offset in range(1, 4):  # Friday to Sunday, full
            day = (DAY + timedelta(days=offset)).isoformat()
            for s in [s for s in self.t.plan_for(day).spans if s.title == "Work"]:
                self.t.execute("remove_event", {"event": s.id, "day": day})
            self.t.execute("add_event", {"title": f"Busy {offset}", "start": "7:00 AM", "end": "11:30 PM",
                                         "day": day, "fixed": True})
        out = settle(self.t.assistant, DAY)
        spread = {r["title"]: r for r in out["respread"]}
        self.assertEqual(spread["Shed clear-out"]["why"], "no room left this week, so it counts toward this "
                                                          "month's plan")
        self.assertEqual(spread["Shed clear-out"]["day"], "")

    def test_the_month_trickles_into_the_week(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this month", "sessions": 6,
                                         "length": "2 hours"})
        share = share_this(self.hp, self.task, WEEK)
        cands = {c.title: c for c in self.hp.candidates(WEEK, limit=None)}
        c = cands["Shed clear-out"]
        self.assertEqual((c.sessions, c.minutes), (share, 120))
        self.assertIn(f"{share} sessions of 120 min", c.line())
        self.assertIn("6 of 6 sessions still to place", c.why)
        self.hp.propose(WEEK)
        out = self.hp.apply(WEEK)
        self.assertIn(f"Shed clear-out ({share} sessions)", out)
        week = budget_of(self.hp, self.task, DAY, level="week")
        self.assertEqual((week.sessions, week.minutes), (share, 120))  # the week now holds its share

    def test_a_session_on_the_schedule_doesnt_take_a_budget_off_the_plan(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this month", "sessions": 6})
        self.session(1)  # one session already on Friday
        cands = {c.title for c in self.hp.candidates(WEEK, limit=None)}
        self.assertIn("Shed clear-out", cands)

    def test_a_spent_budget_offers_nothing(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this month", "sessions": 2})
        self.session(-3, status=SpanStatus.DONE)
        self.session(-2, status=SpanStatus.DONE)
        cands = {c.title for c in self.hp.candidates(WEEK, limit=None)}
        self.assertNotIn("Shed clear-out", cands)

    def test_a_quarter_trickles_into_the_month(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "Q4", "sessions": 6})
        q = budget_of(self.hp, self.task, DAY)
        self.assertEqual((q.period.level, q.period.start, q.sessions), ("quarter", date(2026, 10, 1), 6))
        self.assertEqual(share_this(self.hp, self.task, MONTH), 2)  # 6 over October, November, December
        c = {c.title: c for c in self.hp.candidates(MONTH, limit=None)}["Shed clear-out"]
        self.assertEqual(c.sessions, 2)
        self.assertIn("6 of 6 sessions still to place", c.why)

    def test_a_year_with_no_quarter_trickles_into_the_month(self):
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this year", "sessions": 12})
        self.assertEqual(share_this(self.hp, self.task, MONTH), 4)  # 12 over the three months left

    def test_a_deadline_pulls_the_pieces_earlier(self):
        self.task.due = date(2026, 10, 25).isoformat()  # this Sunday week: two weeks of October left before it
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this month", "sessions": 6})
        self.assertEqual(share_this(self.hp, self.task, WEEK), 3)  # 6 over 2 weeks, not over all 3
        after = period_of("week", date(2026, 10, 26))
        self.assertEqual(share_this(self.hp, self.task, after), 0)  # nothing after it's due

    def test_the_week_trickles_into_the_day(self):
        from thirties_core.day_proposal import _proposal_candidates
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3,
                                         "length": "90 min"})
        day = self.hp.period("today")
        c = {c.title: c for c in self.hp.candidates(day, limit=None)}["Shed clear-out"]
        self.assertEqual((c.sessions, c.minutes), (1, 90))  # 3 left over the 4 days left: one today
        self.assertIn("3 of 3 sessions still to place", c.why)
        offered = {o["title"]: o for o in _proposal_candidates(self.t.assistant.proposals, self.t.day_plan)}
        self.assertEqual(offered["Shed clear-out"]["minutes"], 90)

    def test_the_week_plan_spreads_its_sessions_over_days_with_room(self):
        from thirties_core.budgets import session_days
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 3})
        days = session_days(self.hp, self.task, WEEK)
        self.assertEqual(days, [DAY, DAY + timedelta(days=2), DAY + timedelta(days=3)])  # Thu, Sat, Sun: spread out
        friday = self.hp.period((DAY + timedelta(days=1)).isoformat())
        self.assertNotIn("Shed clear-out", {c.title for c in self.hp.candidates(friday, limit=None)})
        self.assertIn("Shed clear-out (3 sessions of 60 min: 0 done, 0 planned, 3 left: Thu, Sat, Sun)",
                      self.hp.describe_plan(WEEK, with_context=False))

    def test_the_week_plan_bends_as_the_week_fills(self):
        from thirties_core.budgets import session_days
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 2})
        self.session(-2, status=SpanStatus.DONE)  # Tuesday's is done: one left
        saturday = (DAY + timedelta(days=2)).isoformat()
        for s in [s for s in self.t.plan_for(saturday).spans if s.title == "Work"]:
            self.t.execute("remove_event", {"event": s.id, "day": saturday})
        self.t.execute("add_event", {"title": "Busy", "start": "7:00 AM", "end": "11:30 PM", "day": saturday,
                                     "fixed": True})
        self.assertEqual(session_days(self.hp, self.task, WEEK), [DAY])  # one left, front-loaded
        self.session(0, start="7:00 PM")  # put on today's schedule: that's the week's last one
        self.assertEqual(session_days(self.hp, self.task, WEEK), [DAY])

    def test_the_trickle_reads_from_the_quarter_down_to_the_days(self):
        from thirties_core.budgets import trickle_line
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "Q4", "sessions": 6})
        self.assertEqual(trickle_line(self.hp, self.task),
                         "6 sessions in Q4 2026 → 2 sessions this month (October) → 1 session this week")
        self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "this week", "sessions": 1})
        self.assertEqual(trickle_line(self.hp, self.task),
                         "6 sessions in Q4 2026 → 2 sessions this month (October) → 1 session this week → today")
        shown = self.hp.describe_plan(MONTH, with_context=False)
        self.assertIn("  Shed clear-out: 6 sessions in Q4 2026 → ", shown)  # a budget from above reaches the month
        self.assertTrue(shown.startswith("This month (October)"), shown)  # the month's name keeps its capital

    def test_a_span_of_years_reads_and_trickles_into_each_year(self):
        from thirties_core.budgets import trickle_line
        from thirties_core.horizons import parse_period, years_period
        for said, span in (("2026 to 2028", (2026, 2028)), ("the next 3 years", (2026, 2028)),
                           ("by 2029", (2026, 2029)), ("2027-2030", (2027, 2030))):
            self.assertEqual(parse_period(said, DAY), years_period(*span), said)
        self.assertEqual(parse_period("2027", DAY).level, "year")  # one year is still a year
        out = self.t.execute("pin_to_period", {"task": "Shed clear-out", "when": "the next 3 years", "sessions": 90})
        self.assertIn("2026–2028: 90 sessions", out)
        year = period_of("year", DAY)
        self.assertEqual(share_this(self.hp, self.task, year), 30)  # 90 over the three years
        self.assertTrue(trickle_line(self.hp, self.task).startswith("90 sessions in 2026–2028 → 30 sessions this "
                                                                    "year → "), trickle_line(self.hp, self.task))
        c = {c.title: c for c in self.hp.candidates(year, limit=None)}["Shed clear-out"]
        self.assertEqual(c.sessions, 30)  # planning the year draws its share from the span
        shown = self.hp.describe_plan(years_period(2026, 2028), with_context=True)
        self.assertIn("Shed clear-out", shown)
        self.assertIn("2027: nothing planned yet", shown)

    def test_no_budget_no_trickle(self):
        from thirties_core.budgets import trickle_line
        self.assertEqual(trickle_line(self.hp, self.task), "")

    def pin_week(self, title, sessions, length):
        self.services.tasks.append(TaskItem(id="t_" + title[:6].lower(), source_notebook="1. Tasks", title=title,
                                            is_checkbox_item=True, parent_note_title="Errands/Admin",
                                            parent_note_id="n_errands"))
        self.t.execute("pin_to_period", {"task": title, "when": "this week", "sessions": sessions, "length": length})
        hp = HorizonPlanner(self.t)
        return hp, hp.find_task(title)

    def test_long_sessions_go_on_days_off_first(self):
        from thirties_core.budgets import session_days
        hp, task = self.pin_week("Shed clear-out", 2, "2 hours")
        self.assertEqual(session_days(hp, task, WEEK), [DAY + timedelta(days=2), DAY + timedelta(days=3)])  # Sat, Sun
        hp, task = self.pin_week("Sort the photo boxes", 2, "1 hour")
        self.assertEqual(session_days(hp, task, WEEK)[0], DAY)  # a short one still starts today

    def test_something_outdoors_needs_its_stretch_in_daylight(self):
        from thirties_core.budgets import session_days
        self.t.execute("add_event", {"title": "School pickup", "start": "4:00 PM", "end": "5:30 PM", "fixed": True})
        hp, task = self.pin_week("Weed the garden beds", 1, "90 min")
        indoor_hp, indoor = self.pin_week("Sort the photo boxes", 1, "90 min")
        self.assertEqual(session_days(indoor_hp, indoor, WEEK), [DAY])  # this evening has room
        first = session_days(hp, task, WEEK)[0]
        self.assertNotEqual(first, DAY)  # but only an hour of daylight is left today (5:30 to sunset)
        plan = self.t.plan_for(first.isoformat())
        self.assertGreaterEqual((plan.sunset - plan.sunrise).total_seconds(), 90 * 60)

    def test_no_budget_and_no_deadline_simply_ends(self):
        from thirties_core.carry import settle
        self.session(-1, start="3:00 PM")
        out = settle(self.t.assistant, DAY)
        self.assertIn("Shed clear-out", out["ended"])
        self.assertEqual(out["respread"], [])


if __name__ == "__main__":
    unittest.main()
