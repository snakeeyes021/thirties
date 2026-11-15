"""Backing off from what keeps being missed (backoff.py): rests of 1, 2 and 4 whole days, a due date that
overrides them, one question after the third miss, and a run that ends when it's done or the user places it.
Invented items; DAY is Thu Oct 15."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY, at, make_scheduler
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.backoff import MissStreaks, signature
from thirties_core.lists import ListCatalog

D = date(2026, 3, 2)


def miss(streaks, title, day, task_id="", sig=""):
    streaks.record_day(day, [{"title": title, "task_id": task_id, "sig": sig}])


class TestTheRests(unittest.TestCase):

    def setUp(self):
        self.s = MissStreaks(make_scheduler().state_db)

    def first_free(self, title, after, **kw):
        d = after
        while self.s.held(d, title, **kw):
            d += timedelta(days=1)
        return d

    def test_one_then_two_then_four_whole_days(self):
        miss(self.s, "Re-pot the fern", D)
        self.assertEqual(self.first_free("Re-pot the fern", D + timedelta(days=1)), D + timedelta(days=2))  # 1 day
        miss(self.s, "Re-pot the fern", D + timedelta(days=2))
        self.assertEqual(self.first_free("Re-pot the fern", D + timedelta(days=3)), D + timedelta(days=5))  # 2 days
        miss(self.s, "Re-pot the fern", D + timedelta(days=5))
        self.assertEqual(self.first_free("Re-pot the fern", D + timedelta(days=6)), D + timedelta(days=10))  # 4 days
        miss(self.s, "Re-pot the fern", D + timedelta(days=10))
        self.assertEqual(self.first_free("Re-pot the fern", D + timedelta(days=11)), D + timedelta(days=19))  # 8 days

    def test_said_it_cant_wait_counts_only_before_the_third_miss(self):
        miss(self.s, "Mail the tax form", D)
        self.assertFalse(self.s.held(D + timedelta(days=1), "Mail the tax form", urgent=True))
        for n in (1, 2):
            miss(self.s, "Mail the tax form", D + timedelta(days=n))
        self.assertTrue(self.s.held(D + timedelta(days=3), "Mail the tax form", urgent=True))  # rests like the rest

    def test_the_same_day_counts_once(self):
        for _ in range(3):
            miss(self.s, "Re-pot the fern", D)
        self.assertEqual(self.s.get("Re-pot the fern")["misses"], 1)

    def test_a_due_date_near_overrides_it(self):
        for n in range(2):
            miss(self.s, "Renew the fishing licence", D + timedelta(days=n))
        later = D + timedelta(days=2)
        self.assertTrue(self.s.held(later, "Renew the fishing licence", due=(later + timedelta(days=5)).isoformat()))
        self.assertFalse(self.s.held(later, "Renew the fishing licence", due=(later + timedelta(days=2)).isoformat()))
        self.assertFalse(self.s.held(later, "Renew the fishing licence", session=True))  # its prep session today

    def test_done_ends_the_run(self):
        miss(self.s, "Re-pot the fern", D)
        self.s.record_day(D + timedelta(days=1), [], [{"title": "Re-pot the fern"}])
        self.assertFalse(self.s.held(D + timedelta(days=1), "Re-pot the fern"))
        self.assertEqual(self.s.get("Re-pot the fern")["misses"], 0)

    def test_a_changed_item_starts_again(self):
        miss(self.s, "Re-pot the fern", D, task_id="t_fern", sig="a")
        self.assertTrue(self.s.held(D + timedelta(days=1), "Re-pot the fern", "t_fern", sig="a"))
        self.assertTrue(self.s.held(D + timedelta(days=1), "Re-pot the fern", "t_copy", sig="b"))  # a copy of it
        self.assertFalse(self.s.held(D + timedelta(days=1), "Re-pot the fern", "t_fern", sig="b"))  # edited

    def test_the_question_once_after_the_third(self):
        for n in range(2):
            miss(self.s, "Re-pot the fern", D + timedelta(days=n))
        self.assertEqual(self.s.to_ask(), [])
        miss(self.s, "Re-pot the fern", D + timedelta(days=2))
        self.assertEqual([r["title"] for r in self.s.to_ask()], ["Re-pot the fern"])
        self.s.mark_asked(["Re-pot the fern"], D + timedelta(days=3))
        self.assertEqual(self.s.to_ask(), [])
        miss(self.s, "Re-pot the fern", D + timedelta(days=8))  # a fourth: still never asked again
        self.assertEqual(self.s.to_ask(), [])

    def test_the_user_bringing_it_up_lets_a_later_run_ask_again(self):
        for n in range(3):
            miss(self.s, "Re-pot the fern", D + timedelta(days=n))
        self.s.mark_asked(["Re-pot the fern"], D + timedelta(days=3))
        self.s.reset("Re-pot the fern")
        self.assertFalse(self.s.held(D + timedelta(days=3), "Re-pot the fern"))
        for n in range(3):
            miss(self.s, "Re-pot the fern", D + timedelta(days=4 + n))
        self.assertEqual([r["title"] for r in self.s.to_ask()], ["Re-pot the fern"])


class BackoffDays(TodayTestCase):
    """The mornings themselves: what's missed isn't offered or placed again the next morning."""

    def setUp(self):
        super().setUp()
        self.services.tasks.append(item("t_fern", "Re-pot the fern"))
        catalog = ListCatalog(self.sched.state_db)
        for tid in ("t_bank", "t_fern"):
            catalog.set_urgency(tid, "soon")
        self.services.tasks[:] = catalog.apply(self.services.tasks)
        self.streaks = MissStreaks(self.sched.state_db)

    def on(self, offset, title, task_id, outcome=None):
        day = (DAY + timedelta(days=offset)).isoformat()
        self.t.execute("add_event", {"title": title, "start": "2:00 PM", "duration": "30 min", "day": day,
                                     "task_id": task_id, "category": "task"})
        if outcome:
            span = next(s for s in self.t.plan_for(day).spans if s.title == title)
            self.t.execute("log_outcome", {"event": span.id, "status": outcome, "day": day})

    def morning(self, offset):
        from thirties_core.carry import settle
        set_debug_time(at("09:00", offset))
        return settle(self.t.assistant, DAY + timedelta(days=offset))

    def offered(self, offset):
        set_debug_time(at("09:00", offset))
        plan = self.t.plan_for((DAY + timedelta(days=offset)).isoformat())
        return [c["title"] for c in self.t.assistant.proposals.candidates(plan)]

    def test_a_skipped_item_isnt_offered_the_next_morning(self):
        self.assertIn("Call the bank", self.offered(0))  # without a miss, it's an option
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.assertNotIn("Call the bank", self.offered(0))  # the next morning: resting
        self.assertIn("Call the bank", self.offered(1))  # one whole day later, an option again

    def test_an_unmarked_one_isnt_placed_the_next_morning_either(self):
        self.on(-1, "Re-pot the fern", "t_fern")  # left unmarked
        self.morning(0)
        said = self.t.execute("rollover_proposal", {})
        self.assertNotIn("Re-pot the fern", [s.title for s in self.t.plan_for(DAY.isoformat()).spans
                                             if s.status.value == "planned" and s.start_dt.date() == DAY], said)
        self.assertNotIn("Re-pot the fern", self.offered(0))

    def test_a_due_date_near_still_offers_it(self):
        task = next(t for t in self.services.tasks if t.id == "t_bank")
        task.due = (DAY + timedelta(days=1)).isoformat()
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.assertIn("Call the bank", self.offered(0))

    def test_a_guessed_now_doesnt_override_the_rest(self):
        ListCatalog(self.sched.state_db).set_urgency("t_bank", "now", source="guess")
        self.services.tasks[:] = ListCatalog(self.sched.state_db).apply(self.services.tasks)
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.assertNotIn("Call the bank", self.offered(0))

    def test_said_it_cant_wait_skipped_three_days_running_gets_the_question_and_rests(self):
        ListCatalog(self.sched.state_db).set_urgency("t_bank", "now")  # the user's own word
        self.services.tasks[:] = ListCatalog(self.sched.state_db).apply(self.services.tasks)
        for offset in (-3, -2, -1):
            self.morning(offset)
            self.assertIn("Call the bank", self.offered(offset))  # it can't wait: back the next morning
            self.on(offset, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        said = self.t.execute("rollover_proposal", {})
        self.assertIn("Still want Call the bank? Keep for this week / Park for later / Drop", said)  # golden: exact text the user reads
        for offset in range(0, 4):  # no longer re-placed daily: four whole days of rest
            self.morning(offset)
            self.assertNotIn("Call the bank", self.offered(offset))
        self.morning(4)
        self.assertIn("Call the bank", self.offered(4))

    def three_misses(self):
        for offset in (-3, -2, -1):
            self.on(offset, "Re-pot the fern", "t_fern")
        for offset in (-2, -1, 0):
            self.morning(offset)
        self.assertEqual(self.streaks.get("Re-pot the fern")["misses"], 3)

    def test_after_three_misses_one_question_and_then_never_again(self):
        self.three_misses()
        said = self.t.execute("rollover_proposal", {})
        line = "Still want Re-pot the fern? Keep for this week / Park for later / Drop"
        self.assertEqual(said.count(line), 1, said)
        self.assertEqual([r["title"] for r in self.t.assistant.before.rows()["ask"]], ["Re-pot the fern"])
        self.morning(1)
        self.assertNotIn("Still want", self.t.execute("rollover_proposal", {}))  # asked once

    def test_missed_three_days_running_is_on_no_later_day_and_no_notice_lists_it(self):
        from thirties_core.messages import missed_items
        self.three_misses()
        self.t.execute("rollover_proposal", {})
        for offset in range(0, 4):  # missed on DAY-1, the fourth: four whole days of rest
            self.morning(offset)
            self.assertNotIn("Re-pot the fern", self.offered(offset))
            plan = self.t.plan_for((DAY + timedelta(days=offset)).isoformat())
            self.assertNotIn("Re-pot the fern", [s.title for s in plan.spans])
            self.assertEqual(missed_items(plan, at("21:00", offset)), [])  # nothing for a notice to list

    def test_the_answers(self):
        self.three_misses()
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "park"})
        self.assertIn("parked for later", out)
        self.assertTrue(self.streaks.held(DAY + timedelta(days=9), "Re-pot the fern", "t_fern"))
        fern = next(t for t in self.services.tasks if t.id == "t_fern")
        self.assertEqual(fern.horizon, "month")
        self.assertEqual(self.t.assistant.before.rows()["ask"], [])  # answered: off Before today
        self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "keep"})
        self.assertFalse(self.streaks.held(DAY, "Re-pot the fern", "t_fern"))
        lists = self.api.notes["n_errands"]["body"]
        self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "drop"})
        self.assertTrue(self.streaks.held(DAY + timedelta(days=30), "Re-pot the fern", "t_fern"))
        self.assertEqual(self.api.notes["n_errands"]["body"], lists)  # its list in Joplin untouched

    def test_the_user_placing_it_ends_the_run(self):
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.t.user_message = "sounds good"  # the model placing it, the user not naming it: still resting
        self.t.execute("add_event", {"title": "Call the bank", "start": "4:00 PM", "duration": "30 min"})
        self.assertTrue(self.streaks.held(DAY, "Call the bank", "t_bank"))
        span = next(s for s in self.t.plan_for(DAY.isoformat()).spans if s.title == "Call the bank")
        self.t.user_message = "move call the bank to 5"  # the user names it: their placement
        self.t.execute("update_event", {"event": span.id, "start": "5:00 PM"})
        self.assertFalse(self.streaks.held(DAY, "Call the bank", "t_bank"))

    def test_pinning_it_by_its_id_when_the_user_named_it_ends_it(self):
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.t.user_message = "pin call the bank to saturday"
        out = self.t.execute("pin_to_period", {"task": "t_bank", "when": "Saturday"})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertFalse(self.streaks.held(DAY, "Call the bank", "t_bank"))

    def test_a_drag_in_the_app_ends_it_too(self):
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.on(0, "Call the bank", "t_bank")
        span = next(s for s in self.t.plan_for(DAY.isoformat()).spans if s.title == "Call the bank")
        self.t.user_message, self.t.by_hand = None, True
        self.t.execute("update_event", {"event": span.id, "start": "5:00 PM"})
        self.t.by_hand = False
        self.assertFalse(self.streaks.held(DAY, "Call the bank", "t_bank"))

    def test_a_resting_item_the_model_proposes_is_refused(self):
        self.on(-1, "Call the bank", "t_bank", "skipped")
        self.morning(0)
        self.t.user_message = None
        out = self.t.execute("propose_day", {"items": [{"item": "t_bank", "start": "5:00 PM"}]})
        self.assertIn("rests until Friday", out)

    def test_the_signature_is_the_items_own_words(self):
        fern = next(t for t in self.services.tasks if t.id == "t_fern")
        before = signature(fern)
        fern.due = "2026-11-01"
        self.assertNotEqual(signature(fern), before)


if __name__ == "__main__":
    unittest.main()
