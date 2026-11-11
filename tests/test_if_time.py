"""Things added for today get a real time; "If there's time" holds only what the user asked to leave untimed, and
overflow (if_time.py). Fixtures are invented."""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.fixtures import DAY, at
from tests.test_today import TodayTestCase
from thirties_core import if_time
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus


class WordingTest(unittest.TestCase):
    def test_only_explicit_words_ask_for_no_time(self):
        for said in ("call the plumber if there's time", "if I get to it, water the ferns", "no rush, sometime today",
                     "don't schedule it", "squeeze it in if you can", "put the gutters on deck", "pencil in a walk",
                     "whenever today is fine"):
            self.assertTrue(if_time.asked_untimed(said), said)
        for said in ("add call the plumber for today", "I need to water the ferns today", "time to go", None, ""):
            self.assertFalse(if_time.asked_untimed(said), said)


class Base(TodayTestCase):
    def say(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def span(self, title):
        return next((s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED), None)


class AddedTodayTest(Base):
    def test_anything_added_for_today_gets_a_time_at_once(self):
        out = self.say("add water the ferns today", "add_to_today", {"items": ["Water the ferns"]})
        ferns = self.span("Water the ferns")
        self.assertIsNotNone(ferns)
        self.assertGreaterEqual(ferns.start_dt, at("10:00"))
        self.assertIn(f"Water the ferns at ", out)
        self.assertEqual(self.body("n_today"), "- [ ] Water the ferns\n")
        others = [s for s in self.t.day_plan.spans if s is not ferns and s.status == SpanStatus.PLANNED
                  and not s.is_envelope and s.start_dt < ferns.end_dt and s.end_dt > ferns.start_dt]
        self.assertEqual(others, [])  # in free time, never on top of something

    def test_asked_untimed_it_goes_under_if_theres_time_with_no_time(self):
        out = self.say("water the ferns if there's time", "add_to_today", {"items": ["Water the ferns"]})
        self.assertIsNone(self.span("Water the ferns"))
        self.assertEqual(self.body("n_today"), "## If there's time\n- [ ] Water the ferns\n")
        self.assertIn("If there's time", out)
        # a later timed thing goes above the section, never under it
        self.say("and fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        self.assertEqual(self.body("n_today"), "- [ ] Fix the gate\n## If there's time\n- [ ] Water the ferns\n")
        self.assertIsNotNone(self.span("Fix the gate"))

    def test_a_full_day_doubles_up_then_overflows_with_one_line(self):
        with patch("thirties_core.if_time.first_slot", return_value=None):
            out = self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        self.assertIn("alongside another item", out)
        with patch("thirties_core.if_time.first_slot", return_value=None), \
                patch("thirties_core.if_time.doubled_slot", return_value=None):
            out = self.say("and oil the hinges today", "add_to_today", {"items": ["Oil the hinges"]})
        self.assertIn("Today is full, even doubled up, so Oil the hinges is under If there's time.", out)
        self.assertIsNone(self.span("Oil the hinges"))
        self.assertEqual(if_time.items(self.api.notes["n_today"]), ["Oil the hinges"])

    def test_something_already_scheduled_keeps_its_time(self):
        self.say("bank at 3", "add_event", {"title": "Call the bank", "start": "3:00 PM", "duration": "30 min",
                                            "task_id": "t_bank"})
        self.say("put the bank on today", "add_to_today", {"items": ["Call the bank"]})
        spans = [s for s in self.t.day_plan.spans if s.title == "Call the bank"]
        self.assertEqual([s.start_dt for s in spans], [at("15:00")])

    def test_the_schedule_says_if_theres_time_after_the_day(self):
        self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        self.say("water the ferns, no rush", "add_to_today", {"items": ["Water the ferns"]})
        text = self.t.execute("show_schedule", {"whole_day": True})
        self.assertIn("Fix the gate", text.split("If there's time:")[0])
        self.assertTrue(text.rstrip().endswith("If there's time:\nWater the ferns"), text)


class ActionsTest(Base):
    def setUp(self):
        super().setUp()
        self.say("call the bank and water the ferns if there's time", "add_to_today",
                 {"items": ["Call the bank", "Water the ferns"]})

    def test_schedule_it_puts_it_in_the_next_gap_and_out_of_the_section(self):
        out = self.t.execute("if_there_is_time", {"item": "Water the ferns", "action": "schedule"})
        self.assertIn("Water the ferns is on the plan at", out)
        self.assertIsNotNone(self.span("Water the ferns"))
        self.assertEqual(if_time.items(self.api.notes["n_today"]), ["Call the bank"])
        self.assertIn("- [ ] Water the ferns", self.body("n_today"))

    def test_schedule_it_at_a_time_the_user_picks(self):
        self.t.execute("if_there_is_time", {"item": "Water the ferns", "action": "schedule", "time": "5:30 PM"})
        self.assertEqual(self.span("Water the ferns").start_dt, at("17:30"))

    def test_done_ticks_it(self):
        self.t.execute("if_there_is_time", {"item": "Water the ferns", "action": "done"})
        self.assertIn("- [x] Water the ferns", self.body("n_today"))

    def test_tomorrow_puts_it_on_tomorrows_list(self):
        from thirties_core.day_lists import AheadLists
        self.t.execute("if_there_is_time", {"item": "Water the ferns", "action": "tomorrow"})
        self.assertNotIn("Water the ferns", self.body("n_today"))
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1))],
                         ["Water the ferns"])

    def test_later_this_week_and_drop_leave_the_home_list_alone(self):
        from thirties_core.carry import OpenPool
        self.t.execute("if_there_is_time", {"item": "Water the ferns", "action": "week"})
        self.t.execute("if_there_is_time", {"item": "Call the bank", "action": "drop"})
        pool = OpenPool(self.sched.state_db).all()
        self.assertEqual(pool["water the ferns"]["horizon"], "week")
        self.assertEqual(pool["call the bank"]["horizon"], "dropped")
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))  # a drop never touches its own list
        self.assertEqual(if_time.items(self.api.notes["n_today"]), [])

    def test_only_items_in_the_section(self):
        self.assertTrue(self.t.execute("if_there_is_time", {"item": "Buy stamps", "action": "done"}).startswith(
            "NOT_DONE"))


class DayEndTest(Base):
    def test_whats_left_goes_to_the_morning_as_this_week(self):
        from thirties_core.carry import settle
        from thirties_core.plan_tools import PlanTools
        self.say("water the ferns if there's time", "add_to_today", {"items": ["Water the ferns"]})
        with self.sched.state_db._get_connection() as conn:  # it went on an earlier day
            conn.execute("UPDATE sync_meta SET value = '[]' WHERE key LIKE 'today_added:%'")
        set_debug_time(at("09:00", 1))
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        t2 = PlanTools(plan, pending, self.sched, services=self.services)
        out = settle(t2.assistant, DAY + timedelta(days=1))
        rec = next(p for p in out["pool_items"] if p["title"] == "Water the ferns")
        self.assertEqual(rec["horizon"], "week")


if __name__ == "__main__":
    unittest.main()
