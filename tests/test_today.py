"""The day's list: the Today note, linked to the lists its items came from."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools
from thirties_core.today import TodayLinks

TITLES = {"n_today": "Today To-Do", "n_errands": "Errands/Admin", "n_routine": "Morning Routine"}


def item(tid, title, note="n_errands"):
    return TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=TITLES[note], parent_note_id=note)


class TodayTestCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n_today": {"id": "n_today", "body": ""},
                                  "n_errands": {"id": "n_errands", "body": "- [ ] Call the bank\n- [ ] Buy stamps\n"}})
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES[nid], "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_today", "today", "Today To-Do")
        catalog.set_role("n_errands", "list", "Errands/Admin")
        self.services = Services.for_db(self.sched.state_db, catalog.apply(
            [item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps")]), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)

    def body(self, note):
        return self.api.notes[note]["body"]


class TestAddingToToday(TodayTestCase):

    def test_list_items_go_on_linked_and_new_things_go_on_as_they_are(self):
        out = self.t.execute("add_to_today", {"items": ["call the bank", "Clip cat nails", "Buy stamps for the trip"]})
        self.assertEqual(out, "Added to today's list: Call the bank, Clip cat nails, Buy stamps for the trip.")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n- [ ] Clip cat nails\n- [ ] Buy stamps for the trip\n")
        links = TodayLinks(self.sched.state_db)
        self.assertEqual(links.home_of("Call the bank")["home_id"], "t_bank")
        self.assertIsNone(links.home_of("Buy stamps for the trip"))  # only similar to a list item: new
        self.assertEqual(self.t.execute("add_to_today", {"items": "Call the bank"}), "Already on it: Call the bank.")

    def test_scheduling_a_list_item_for_today_puts_it_on_todays_list(self):
        out = self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": "t_bank"})
        self.assertIn("(also added to today's list)", out)
        self.assertIn("- [ ] Call the bank", self.body("n_today"))
        out = self.t.execute("add_event", {"title": "Stamps", "start": "3:00 PM", "duration": "30 min",
                                           "task_id": "t_stamps", "day": "tomorrow"})
        self.assertNotIn("today's list", out)  # tomorrow's things aren't on today's list

    def test_without_a_today_note(self):
        ListCatalog(self.sched.state_db).set_role("n_today", "list", "Today To-Do")
        self.assertTrue(self.t.execute("add_to_today", {"items": ["x"]}).startswith(NOT_DONE))


class TestTickingTwins(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Call the bank"]})

    def test_done_on_today_ticks_the_home_item(self):
        today_item = next(t for t in self.services.tasks if t.parent_note_id == "n_today")
        self.t.assistant._complete_backlog_task(today_item)
        self.assertIn("- [x] Call the bank", self.body("n_today"))
        self.assertIn("- [x] Call the bank", self.body("n_errands"))
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))

    def test_done_at_home_ticks_the_today_item(self):
        home = next(t for t in self.services.tasks if t.id == "t_bank")
        self.t.assistant._complete_backlog_task(home)
        self.assertIn("- [x] Call the bank", self.body("n_errands"))
        self.assertEqual(self.body("n_today"), "- [x] Call the bank\n")


class TestMorning(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.t.execute("add_to_today", {"items": ["Call the bank", "Clip cat nails"]})
        self.api.notes["n_today"]["body"] = "- [x] Feed the cats\n" + self.body("n_today")
        self.api.notes["n_routine"] = {"id": "n_routine", "body": "- [x] Shower\n- [x] PT\n"}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_routine", "routine", "Morning Routine")
        catalog.set_urgency("t_stamps", "now")
        self.services.tasks = catalog.apply(self.services.tasks)

    def test_routines_start_unticked_and_whats_done_leaves_today(self):
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [ ] Shower\n- [ ] PT\n")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n- [ ] Clip cat nails\n")
        self.sched.config.joplin.reset_routines = False
        self.api.notes["n_routine"]["body"] = "- [x] Shower\n"
        self.t.assistant.start_the_day()
        self.assertEqual(self.body("n_routine"), "- [x] Shower\n")  # not allowed: left alone

    def test_the_morning_proposal_keeps_drops_and_adds(self):
        self.t.assistant.start_the_day()
        out = self.t.execute("rollover_proposal", {})
        self.assertEqual(out, "From yesterday, still open:\n- Call the bank\n- Clip cat nails\n"
                              "Also pressing: Buy stamps (now).\nWant today like that?")
        out = self.t.execute("apply_rollover", {"skip": ["bank", "nails"]})
        self.assertIn("Back to your lists: Call the bank.", out)
        self.assertIn("Added to today's list: Buy stamps.", out)
        self.assertIn("with no other list to go back to: Clip cat nails", out)
        self.assertEqual(self.body("n_today"), "- [ ] Buy stamps\n")
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))  # still on its home list


if __name__ == "__main__":
    unittest.main()
