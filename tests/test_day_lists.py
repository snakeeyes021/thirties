"""Pencilled-in items (O3), planning tomorrow (O2), and "remind me" as a planned item (B5, C4)."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.day_lists import AheadLists, explicit_ping, pencilled_in, planned_ahead_text
from thirties_core.plan_tools import PlanTools


class TestPencilledIn(ListsTestCase):

    def test_on_the_list_with_no_time_is_said_as_such(self):
        self.t.execute("add_to_today", {"items": ["Call the bank", "Clip cat nails"]})
        bank = next(t for t in self.services.tasks if t.title == "Call the bank" and t.parent_note_id == "n_errands")
        self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": bank.id})
        self.assertEqual(pencilled_in(self.t.assistant), ["Clip cat nails"])  # the bank has a time now
        self.assertIn("On today's list, no time yet (pencilled in): Clip cat nails", self.t.execute("list_schedule", {}))
        self.assertEqual(self.t.execute("pencilled_in", {}), "On the list, no time yet:\n- Clip cat nails")

    def test_pencilling_out_takes_it_off_the_plan_and_keeps_it_on_the_list(self):
        # 10-29: "pencil them out (not pen)": they stayed on the plan, and were never on the Today note
        self.t.execute("add_event", {"title": "Walk", "start": "4:00 PM", "duration": "30 min"})
        out = self.t.execute("pencil_in", {"items": ["Walk", "Read a chapter"]})
        self.assertEqual(out, "On today's list, no time yet: Walk, Read a chapter. Off the plan: Walk.")
        self.assertFalse(any(s.title == "Walk" for s in self.t.day_plan.spans))
        self.assertEqual(self.body("n_today"), "- [ ] Walk\n- [ ] Read a chapter\n")
        self.assertEqual(pencilled_in(self.t.assistant), ["Walk", "Read a chapter"])
        out = self.t.execute("add_event", {"title": "Walk", "start": "5:00 PM", "duration": "30 min"})  # a time later
        self.assertNotIn("Walk\n- [ ] Walk", self.body("n_today"))
        self.assertEqual(pencilled_in(self.t.assistant), ["Read a chapter"])


class TestTomorrow(ListsTestCase):

    def test_planned_for_tomorrow_reaches_today_that_morning_not_now(self):
        # 10-24: "should I see it in the day plan for tomorrow?"
        out = self.t.execute("add_event", {"title": "Dentist call", "start": "10:00 AM", "duration": "30 min",
                                           "day": "tomorrow"})
        self.assertIn("goes on the Today list that morning, not on today's", out)
        out = self.t.execute("add_to_today", {"items": ["Buy stamps", "Water plants"], "day": "tomorrow"})
        self.assertIn("On Friday's list, no time yet: Buy stamps, Water plants", out)
        self.assertEqual(self.body("n_today"), "")  # never today's
        tomorrow = self.t.execute("list_schedule", {"day": "tomorrow"})
        self.assertIn("Dentist call", tomorrow)
        self.assertIn("On Friday's list, no time yet (pencilled in): Buy stamps; Water plants", tomorrow)

        set_debug_time(at("08:00", 1))  # the next morning
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        t2 = PlanTools(plan, pending, self.sched, services=self.services)
        t2.assistant.start_the_day()
        self.assertEqual(self.body("n_today"), "- [ ] Dentist call\n- [ ] Buy stamps\n- [ ] Water plants\n")
        stamps = next(t for t in self.services.tasks if t.title == "Buy stamps" and t.parent_note_id == "n_today")
        self.assertTrue(stamps)
        from thirties_core.today import TodayLinks
        self.assertEqual(TodayLinks(self.sched.state_db).home_of("Buy stamps")["home_note_id"], "n_errands")
        self.assertEqual(AheadLists(self.sched.state_db).for_day(DAY + timedelta(days=1)), [])
        morning = planned_ahead_text(t2.assistant)
        self.assertIn("Already planned for today:\n- 10:00 AM – Dentist call", morning)
        self.assertIn("On today's list, no time yet: Buy stamps, Water plants.", morning)
        # what yesterday's offer names is said there, once (10-31 sim: four items listed three times)
        self.assertIn("On today's list, no time yet: Water plants.", planned_ahead_text(t2.assistant, skip=["Buy stamps"]))


class TestTomorrowsListItemGone(ListsTestCase):

    def test_a_list_item_done_in_the_meantime_goes_on_by_its_wording(self):
        self.t.execute("add_to_today", {"items": ["Buy stamps"], "day": "tomorrow"})
        self.services.tasks = [t for t in self.services.tasks if t.title != "Buy stamps"]  # done and filed since
        set_debug_time(at("08:00", 1))
        plan, pending = self.sched.build_day_plan(DAY + timedelta(days=1))
        PlanTools(plan, pending, self.sched, services=self.services).assistant.start_the_day()
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
