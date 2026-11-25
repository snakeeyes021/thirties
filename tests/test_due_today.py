"""An item due today never leaves today silently (due_today.py): Thirties' own planning keeps it, a vague word
keeps it and says it's due today, a clear request moves it and names the deadline. One test per way off today.
Fixtures and wordings are invented."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_if_time import Base
from tests.test_today import morning_today
from thirties_core.carry import OpenPool
from thirties_core.day_lists import AheadLists, pencilled_in
from thirties_core.due_today import CLEAR_DAY_RE, is_due_today
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

TOMORROW = DAY + timedelta(days=1)


class DueBase(Base):
    def setUp(self):
        super().setUp()
        self.a = self.t.assistant

    def due_today(self, title, task_id=""):
        """Due today, the way the user's lists keep it (its urgency and due date)."""
        ListCatalog(self.sched.state_db).set_urgency(task_id or f"line:{title}", "soon", due=DAY, title=title)
        for t in self.services.tasks:
            if t.id == task_id:
                t.due = DAY.isoformat()

    def planned(self, title, plan=None):
        return [s for s in (plan or self.t.day_plan).spans if s.title == title and s.status == SpanStatus.PLANNED]

    def bank_today(self):
        self.due_today("Call the bank", "t_bank")
        self.say("call the bank today", "add_to_today", {"items": ["Call the bank"]})
        return self.planned("Call the bank")[0]


class FactsTest(DueBase):
    def test_due_dates_come_from_what_items_already_carry(self):
        self.due_today("Call the bank", "t_bank")
        self.assertTrue(is_due_today(self.t, "Call the bank", "t_bank"))
        self.assertTrue(is_due_today(self.t, "Call the bank"))  # by its wording
        self.assertFalse(is_due_today(self.t, "Buy stamps", "t_stamps"))
        self.due_today("Oil the hinges")  # an item only on Today: kept by its wording
        self.assertTrue(is_due_today(self.t, "Oil the hinges"))
        self.assertTrue(CLEAR_DAY_RE.search("to tomorrow please"))
        self.assertIsNone(CLEAR_DAY_RE.search("later this week, not today"))


class IfTheresTimeActionsTest(DueBase):
    def test_vague_words_keep_it_and_say_its_due(self):
        self.due_today("Water the ferns")
        self.say("water the ferns if there's time", "add_to_today", {"items": ["Water the ferns"]})
        for said, action in (("not today for the ferns", "tomorrow"), ("ferns later this week", "week")):
            out = self.say(said, "if_there_is_time", {"item": "Water the ferns", "action": action})
            self.assertTrue(out.startswith(NOT_DONE), out)
            self.assertIn("Water the ferns is due today, so it stays on today", out)
        self.assertIn("Water the ferns", self.body("n_today"))
        self.assertEqual(AheadLists(self.sched.state_db).for_day(TOMORROW), [])

    def test_a_clear_word_moves_it_and_names_the_deadline(self):
        self.due_today("Water the ferns")
        self.say("water the ferns if there's time", "add_to_today", {"items": ["Water the ferns"]})
        out = self.say("move the ferns to tomorrow", "if_there_is_time", {"item": "Water the ferns",
                                                                          "action": "tomorrow"})
        self.assertIn("Water the ferns was due today; it's on tomorrow now.", out)
        self.assertNotIn("Water the ferns", self.body("n_today"))

    def test_a_line_outside_if_theres_time_too(self):
        self.bank_today()
        out = self.say("not today for the bank", "if_there_is_time", {"item": "Call the bank", "action": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertTrue(self.planned("Call the bank"))
        out = self.say("move the bank to tomorrow", "if_there_is_time", {"item": "Call the bank",
                                                                         "action": "tomorrow"})
        self.assertIn("Call the bank was due today; it's on tomorrow now.", out)
        self.assertEqual(self.planned("Call the bank"), [])


class SkipTest(DueBase):
    def test_a_skip_stands_and_says_it_was_due(self):
        span = self.bank_today()
        out = self.say("skip the bank today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("Call the bank is due today.", out)  # a fact for the model, never a hold
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.assertEqual(self.planned("Call the bank"), [])
        self.assertNotIn("Call the bank", pencilled_in(self.a))


class MoveToAnotherDayTest(DueBase):
    def test_vague_keeps_it_clear_moves_it(self):
        span = self.bank_today()
        out = self.say("not today, the bank can wait", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("due today", out)
        self.assertTrue(self.planned("Call the bank"))
        out = self.say("move the bank to tomorrow", "update_event", {"event": span.id, "to_day": "tomorrow"})
        self.assertIn("Call the bank was due today; it's on tomorrow now.", out)
        self.assertEqual(self.planned("Call the bank"), [])
        self.assertTrue(self.planned("Call the bank", self.t.plan_for(TOMORROW)))

    def test_onto_a_later_days_list(self):
        self.bank_today()
        out = self.say("not today for the bank, maybe later", "add_to_today",
                       {"items": ["Call the bank"], "day": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(AheadLists(self.sched.state_db).for_day(TOMORROW), [])
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        out = self.say("push the bank to tomorrow's list", "add_to_today",
                       {"items": ["Call the bank"], "day": "tomorrow"})
        self.assertIn("Call the bank was due today; it's on tomorrow now.", out)


class RemoveTest(DueBase):
    def test_the_removal_stands_and_says_the_deadline(self):
        span = self.bank_today()
        out = self.say("skip the bank today", "remove_event", {"event": span.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Call the bank is due today.", out)  # a fact for the model, never a hold
        self.assertNotIn("Call the bank", pencilled_in(self.a))
        self.assertEqual(self.planned("Call the bank"), [])
        self.assertNotIn("Call the bank", self.body("n_today"))
        self.due_today("Oil the hinges")
        self.api.notes["n_today"]["body"] += "- [ ] Oil the hinges\n"
        out = self.say("skip the hinges today", "remove_event", {"event": "Oil the hinges"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("Oil the hinges is due today.", out)
        self.assertNotIn("Oil the hinges", self.body("n_today"))


class PinToPeriodTest(DueBase):
    def test_a_later_period_needs_a_clear_word(self):
        self.due_today("Call the bank", "t_bank")
        out = self.say("the bank can wait", "pin_to_period", {"task": "Call the bank", "when": "next week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("due today", out)
        out = self.say("pin the bank to next week", "pin_to_period", {"task": "Call the bank", "when": "next week"})
        self.assertIn("Call the bank was due today; it's on ", out)


class LeftoversTest(DueBase):
    def setUp(self):
        super().setUp()
        self.due_today("Call the bank", "t_bank")
        OpenPool(self.sched.state_db).add([{"title": "Call the bank", "task_id": "t_bank", "horizon": "week",
                                            "due": DAY.isoformat()}], DAY - timedelta(days=1))

    def test_placing_a_leftover_due_today(self):
        out = self.say("the bank this week", "place_leftover", {"item": "Call the bank", "horizon": "this week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("due today", out)
        out = self.say("move the bank to tomorrow", "place_leftover", {"item": "Call the bank",
                                                                       "horizon": "tomorrow"})
        self.assertIn("Call the bank was due today; it's on tomorrow now.", out)

    def test_carry_over_keeps_it_today(self):
        from thirties_core.carry import TODAY, choose_horizon
        task = next(t for t in self.services.tasks if t.id == "t_bank")
        self.assertEqual(choose_horizon(self.a, task, task.title, DAY), TODAY)


class MorningProposalTest(DueBase):
    def setUp(self):
        super().setUp()
        morning_today(self)

    def test_thirties_own_leftover_proposal_keeps_it_today(self):
        self.t.execute("add_event", {"title": "Buy stamps", "start": "7:00 PM", "duration": "30 min",
                                     "task_id": "t_stamps", "day": (DAY - timedelta(days=1)).isoformat()})
        self.due_today("Buy stamps", "t_stamps")
        self.a.start_the_day()
        self.t.execute("rollover_proposal", {})
        pending = {p["title"] for p in self.a.before.rows()["keep"]}
        keep = [{"item": t, "when": "this week"} for t in pending]
        self.t.execute("propose_leftovers", {"keep": keep})
        rec = OpenPool(self.sched.state_db).all()["buy stamps"]
        self.assertEqual(rec["end"], DAY.isoformat())  # today, whatever was proposed
        out = self.t.execute("propose_leftovers", {"keep": [k for k in keep if k["item"] != "Buy stamps"],
                                                   "drop": [{"item": "Buy stamps"}]})
        self.assertTrue(out.startswith(NOT_DONE), out)


class ProposeDayTest(DueBase):
    def test_a_rested_item_due_today_can_still_be_proposed(self):
        from thirties_core.backoff import MissStreaks
        streaks = MissStreaks(self.sched.state_db)
        for back in (3, 2):
            streaks.record_day(DAY - timedelta(days=back), [{"title": "Buy stamps", "task_id": "t_stamps"}])
        out = self.say("plan my evening", "propose_day", {"items": [{"item": "Buy stamps", "start": "6:00 PM"}]})
        self.assertTrue(out.startswith(NOT_DONE), out)  # missed lately: it rests
        self.due_today("Buy stamps", "t_stamps")
        out = self.say("plan my evening", "propose_day", {"items": [{"item": "Buy stamps", "start": "6:00 PM"}]})
        self.assertFalse(out.startswith(NOT_DONE), out)


class RedrawTest(DueBase):
    def test_no_room_keeps_it_on_today(self):
        from tests.fixtures import at as at_
        from thirties_core.astronomy import set_debug_time
        set_debug_time(at_("21:00"))
        self.t.execute("add_event", {"title": "Pack the swim bag", "start": "9:00 PM", "duration": "90 min"})
        self.t.execute("add_event", {"title": "Fill in the permit form", "start": "10:30 PM", "duration": "60 min"})
        self.due_today("Fill in the permit form")
        out = self.say("redo the evening", "redraw", {})
        self.assertIn("Fill in the permit form doesn't fit before bedtime, but it's due today", out)
        self.assertTrue(self.planned("Fill in the permit form"))
        self.assertNotIn("fill in the permit form", OpenPool(self.sched.state_db).all())


if __name__ == "__main__":
    unittest.main()
