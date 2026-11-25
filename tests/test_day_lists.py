"""Pencilled-in items (O3), planning tomorrow (O2), and "remind me" as a planned item (B5, C4)."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, line_with
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.day_lists import AheadLists, explicit_ping, pencilled_in, planned_ahead_text
from thirties_core.plan_tools import PlanTools


class TestPencilledIn(ListsTestCase):

    def test_a_line_with_no_plan_item_is_never_shown_and_the_plan_build_gives_it_a_time(self):
        from tests.test_today import as_earlier_lines
        from thirties_core.full_day import settle_loose
        with as_earlier_lines():  # lines with no time: from an earlier day, or written by hand
            self.t.execute("add_to_today", {"items": ["Call the bank", "Rinse the tank filter"]})
        bank = next(t for t in self.services.tasks if t.title == "Call the bank" and t.parent_note_id == "n_errands")
        self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": bank.id})
        self.assertEqual(pencilled_in(self.t.assistant), ["Rinse the tank filter"])  # the bank has a time now
        self.assertNotIn("no time yet", self.t.execute("list_schedule", {}))
        settle_loose(self.t.assistant)
        self.assertEqual(pencilled_in(self.t.assistant), [])
        self.assertTrue(any(s.title == "Rinse the tank filter" for s in self.t.day_plan.spans))

    def test_pencilled_in_today_comes_off_the_plan_into_if_theres_time(self):
        # 10-29: items asked to be pencilled in stayed on the plan, and were never on the Today note
        from thirties_core import if_time
        self.t.execute("add_event", {"title": "Walk", "start": "4:00 PM", "duration": "30 min"})
        out = self.t.execute("pencil_in", {"items": ["Walk", "Read a chapter"]})
        line_with(out, "If there's time", "Walk", "Read a chapter")
        self.assertFalse(any(s.title == "Walk" and s.status.value == "planned" for s in self.t.day_plan.spans))
        self.assertEqual(self.body("n_today"), "## If there's time\n- [ ] Walk\n- [ ] Read a chapter\n")
        self.assertEqual(if_time.items(self.api.notes["n_today"]), ["Walk", "Read a chapter"])
        self.assertEqual(pencilled_in(self.t.assistant), [])  # its own section, never "no plan item"


class TestTomorrow(ListsTestCase):

    def test_planned_for_tomorrow_reaches_today_that_morning_with_a_time(self):
        # 10-24: an item planned for tomorrow didn't show where the user expected it
        from thirties_core.carry import OpenPool
        from thirties_core.full_day import settle_loose
        out = self.t.execute("add_event", {"title": "Dentist call", "start": "10:00 AM", "duration": "30 min",
                                           "day": "tomorrow"})
        line_with(out, "Today list", "that morning")
        self.assertEqual(self.body("n_today"), "")
        out = self.t.execute("add_to_today", {"items": ["Buy stamps", "Water plants"], "day": "tomorrow"})
        line_with(out, "Friday", "Buy stamps", "Water plants")
        self.assertNotIn("no time", out)
        self.assertEqual(self.body("n_today"), "")  # never today's
        self.assertEqual(OpenPool(self.sched.state_db).all()["buy stamps"]["start"], (DAY + timedelta(days=1)).isoformat())
        tomorrow = self.t.execute("list_schedule", {"day": "tomorrow"})
        self.assertIn("Dentist call", tomorrow)
        self.assertNotIn("no time yet", tomorrow)

        set_debug_time(at("08:00", 1))  # the next morning
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        t2 = PlanTools(plan, pending, self.sched, services=self.services)
        t2.assistant.start_the_day()
        self.assertEqual(self.body("n_today"), "- [ ] Dentist call\n")  # nothing reaches Today without a time
        settle_loose(t2.assistant)  # the day's plan is built: each gets its time, and Today follows the plan
        for title in ("Buy stamps", "Water plants"):
            self.assertTrue(any(s.title == title for s in t2.day_plan.spans), title)
            self.assertIn(f"- [ ] {title}\n", self.body("n_today"))
        self.assertEqual(AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1)), [])
        morning = planned_ahead_text(t2.assistant)
        self.assertTrue(morning.startswith("Already planned for today:\n"), morning)  # plain lines, time first
        self.assertIn("\n10:00 AM Dentist call", morning)
        self.assertNotIn("no time yet", morning)


class TestTomorrowsListItemGone(ListsTestCase):

    def test_a_list_item_done_in_the_meantime_goes_on_by_its_wording(self):
        from thirties_core.full_day import settle_loose
        self.t.execute("add_to_today", {"items": ["Buy stamps"], "day": "tomorrow"})
        self.services.tasks = [t for t in self.services.tasks if t.title != "Buy stamps"]  # done and filed since
        set_debug_time(at("08:00", 1))
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        t2 = PlanTools(plan, pending, self.sched, services=self.services)
        t2.assistant.start_the_day()
        settle_loose(t2.assistant)
        self.assertEqual(self.body("n_today"), "- [ ] Buy stamps\n")  # never its id


class TestRemindMe(ListsTestCase):

    def test_remind_me_is_a_planned_linked_item_and_a_bare_ping_needs_asking(self):
        bank = next(t for t in self.services.tasks if t.title == "Call the bank")
        self.t.user_message = "remind me to call the bank at 4"
        out = self.t.execute("set_reminder", {"text": "Call the bank", "time": "4:00 PM", "task_id": bank.id,
                                              "ping_only": True})  # the model reached for a bare ping
        self.assertIn("On the plan for", out)
        self.assertIn("and on today's list", out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Call the bank")
        self.assertEqual(span.task_id, bank.id)  # done on the plan ticks the list item
        self.t.user_message = "ping me at 5 that the laundry's done"
        out = self.t.execute("set_reminder", {"text": "the laundry's done", "time": "5:00 PM", "ping_only": True})
        self.assertTrue(out.startswith("Ping set for"), out)

    def test_explicit_ping_words(self):
        for said in ("ping me at 3", "set an alarm for 6", "text me when it's time", "notify me"):
            self.assertTrue(explicit_ping(said), said)
        self.assertFalse(explicit_ping("remind me to take the bins out"))
        self.assertTrue(explicit_ping(None))


if __name__ == "__main__":
    unittest.main()
