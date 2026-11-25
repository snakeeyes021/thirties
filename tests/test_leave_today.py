"""An item leaving today takes its copy off the Today note: skipped for today, moved to another day, or taken off,
and the If there's time actions work on any line of today's list (leave_today.py). Data guard: the home list is
never written to and nothing is ticked. Fixtures are invented."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_if_time import Base
from thirties_core import day_lists
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import OpenPool
from thirties_core.day_lists import AheadLists, pencilled_in
from thirties_core.leave_today import asks_to_skip
from thirties_core.models import SpanStatus
from thirties_core.today import TodayLinks

HOME = "- [ ] Call the bank\n- [ ] Buy stamps\n"


class LeaveBase(Base):
    def setUp(self):
        super().setUp()
        self.a = self.t.assistant

    def assert_guarded(self):
        """The home list is as it was, nothing anywhere is ticked, and only the Today note (or a Dropped note) was
        written to."""
        self.assertEqual(self.body("n_errands"), HOME)
        self.assertNotIn("[x]", self.body("n_today"))
        self.assertEqual([u for u in self.api.updates if u[0] == "n_errands"], [])

    def planned(self, title, plan=None):
        plan = plan or self.t.day_plan
        return [s for s in plan.spans if s.title == title and s.status == SpanStatus.PLANNED]


class SkipWordsTest(unittest.TestCase):
    def test_skip_words(self):
        for said in ("skip the bank today", "not today for the stamps", "I'm not doing the gate", "forget the gate",
                     "I won't get to the gate", "leave the gate for tomorrow"):
            self.assertTrue(asks_to_skip(said), said)
        for said in ("the bank is done", "move the gate to 4", None, ""):
            self.assertFalse(asks_to_skip(said), said)


class SkippedTodayTest(LeaveBase):
    def test_a_list_item_skipped_for_today_leaves_today_but_not_its_list(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        span = self.planned("Call the bank")[0]
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        out = self.say("skip the bank today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("off today's list", out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assertIsNone(TodayLinks(self.sched.state_db).home_of("Call the bank"))
        self.assertEqual(pencilled_in(self.a), [])
        self.assert_guarded()

    def test_an_item_only_on_today_skipped_waits_for_later_this_week(self):
        self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        span = self.planned("Fix the gate")[0]
        out = self.say("skip the gate today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("later this week", out)
        self.assertNotIn("Fix the gate", self.body("n_today"))
        rec = OpenPool(self.sched.state_db).all().get("fix the gate")
        self.assertIsNotNone(rec)  # not lost: it's in Before today's pool
        self.assertNotIn("dropped", str(rec.get("horizon", "")))
        self.assertEqual(pencilled_in(self.a), [])
        self.assert_guarded()

    def test_skipped_isnt_shown_as_no_time_yet_even_before_joplin_has_the_change(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        span = self.planned("Call the bank")[0]
        self.api.up = False  # Joplin closed: the removal waits in the outbox
        self.say("skip the bank today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        self.assertEqual(pencilled_in(self.a), [])
        self.assertNotIn("no time yet", day_lists.planned_ahead_text(self.a))

    def test_another_copy_still_planned_today_keeps_the_line(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        first = self.planned("Call the bank")[0]
        self.say("and again at 4", "add_event", {"title": "Call the bank", "start": "4:00 PM", "duration": "30 min",
                                                 "task_id": "t_bank", "again": True})
        self.say("skip the first bank call", "log_outcome", {"event": first.id, "status": "skipped"})
        self.assertIn("- [ ] Call the bank", self.body("n_today"))

    def test_the_background_skipping_on_its_own_leaves_the_list_alone(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        span = self.planned("Call the bank")[0]
        self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})  # nobody talking
        self.assertIn("- [ ] Call the bank", self.body("n_today"))


class MovedToAnotherDayTest(LeaveBase):
    def test_a_list_item_moved_to_tomorrow_leaves_today_and_comes_back_that_morning(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        span = self.planned("Call the bank")[0]
        out = self.say("move the bank to tomorrow", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertIn("off today's list", out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        tomorrow = self.t.plan_for(DAY + timedelta(days=1))
        self.assertEqual(len(self.planned("Call the bank", tomorrow)), 1)
        self.assertEqual(pencilled_in(self.a), [])
        self.assert_guarded()

    def test_an_item_only_on_today_moved_to_tomorrow_goes_on_tomorrows_today_list(self):
        self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        span = self.planned("Fix the gate")[0]
        self.say("move the gate to tomorrow", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertNotIn("Fix the gate", self.body("n_today"))
        moved = self.planned("Fix the gate", self.t.plan_for(DAY + timedelta(days=1)))[0]
        self.assertFalse(moved.task_id)  # its old line's id is gone with the line
        set_debug_time(at("07:30", 1))  # tomorrow's morning reset puts it back on Today
        day_lists.onto_today(self.a, DAY + timedelta(days=1))
        self.assertIn("- [ ] Fix the gate", self.body("n_today"))
        self.assert_guarded()

    def test_on_a_later_days_list_with_a_move_word_it_leaves_today(self):
        self.say("stamps today", "add_to_today", {"items": ["Buy stamps"]})
        out = self.say("push the stamps to tomorrow's list", "add_to_today",
                       {"items": ["Buy stamps"], "day": "tomorrow"})
        self.assertIn("off today's list", out)
        self.assertNotIn("Buy stamps", self.body("n_today"))
        self.assertEqual(self.planned("Buy stamps"), [])
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1))],
                         ["Buy stamps"])
        self.assert_guarded()

    def test_on_a_later_days_list_too_it_stays_today(self):
        self.say("stamps today", "add_to_today", {"items": ["Buy stamps"]})
        self.say("put stamps on tomorrow's list too", "add_to_today", {"items": ["Buy stamps"], "day": "tomorrow"})
        self.assertIn("- [ ] Buy stamps", self.body("n_today"))


class AnyLineOnTodayTest(LeaveBase):
    def untimed_line(self, title):
        """A line on Today with no time and not under If there's time (put there by hand)."""
        self.api.notes["n_today"]["body"] += f"- [ ] {title}\n"
        self.a.reload_tasks() if hasattr(self.a, "reload_tasks") else None

    def test_drop_works_on_a_line_outside_if_theres_time(self):
        self.untimed_line("Oil the hinges")
        self.assertEqual(pencilled_in(self.a), ["Oil the hinges"])
        out = self.say("drop the hinges", "if_there_is_time", {"item": "Oil the hinges", "action": "drop"})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertNotIn("Oil the hinges", self.body("n_today"))
        self.assertEqual(pencilled_in(self.a), [])
        self.assert_guarded()

    def test_drop_of_a_list_item_takes_its_copy_and_its_time_today(self):
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        self.assertTrue(self.planned("Call the bank"))
        out = self.say("drop the bank", "if_there_is_time", {"item": "Call the bank", "action": "drop"})
        self.assertIn("stays on its own list", out)
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assertEqual(self.planned("Call the bank"), [])
        self.assert_guarded()

    def test_tomorrow_on_a_timed_line(self):
        self.say("fix the gate today", "add_to_today", {"items": ["Fix the gate"]})
        out = self.say("gate tomorrow", "if_there_is_time", {"item": "Fix the gate", "action": "tomorrow"})
        self.assertIn("tomorrow's list", out)
        self.assertNotIn("Fix the gate", self.body("n_today"))
        self.assertEqual(self.planned("Fix the gate"), [])
        self.assertEqual([r["title"] for r in AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1))],
                         ["Fix the gate"])
        self.assert_guarded()

    def test_skip_said_takes_an_untimed_line_off_with_remove_event(self):
        self.untimed_line("Oil the hinges")
        out = self.say("skip the hinges today", "remove_event", {"event": "Oil the hinges", "today_list": "later"})
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertNotIn("Oil the hinges", self.body("n_today"))
        self.assertIsNotNone(OpenPool(self.sched.state_db).all().get("oil the hinges"))  # skipped, not dropped
        self.assert_guarded()

    def test_an_item_not_on_today_is_refused(self):
        out = self.say("drop the kite", "if_there_is_time", {"item": "Fly the kite", "action": "drop"})
        self.assertTrue(out.startswith("NOT_DONE"), out)

    def test_a_routines_group_is_not_taken_off(self):
        self.api.notes["n_today"]["body"] += "- [ ] Kiln check\n    - [ ] Vent the kiln\n"
        from unittest.mock import patch
        with patch.object(type(self.a), "_routines", return_value=[{"id": "n_routine", "title": "Kiln check",
                                                                      "body": "- [ ] Vent the kiln\n"}]):
            out = self.say("drop the kiln check", "if_there_is_time", {"item": "Kiln check",
                                                                             "action": "drop"})
        self.assertTrue(out.startswith("NOT_DONE"), out)
        self.assertIn("- [ ] Kiln check", self.body("n_today"))


if __name__ == "__main__":
    unittest.main()
