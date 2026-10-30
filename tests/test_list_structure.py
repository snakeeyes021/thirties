"""Editing a note's structure when the user asks (J7), and the triage of a list sorted by time (After 0.1, item 8)."""

import unittest

from tests.test_list_placement import NOTES, ListsTestCase
from thirties_core.assistant_tools import _checkbox_id
from thirties_core.brains.frontier import FrontierBrain
from thirties_core.inference import MockInferenceEngine
from thirties_core.joplin_writeback import WriteLog
from thirties_core.lists import ListCatalog
from thirties_core.plan_tools import NOT_DONE
from thirties_core.today import TodayLinks


class TestStructure(ListsTestCase):

    def bank(self):
        return next(t for t in self.services.tasks if t.title == "Call the bank" and t.parent_note_id == "n_errands")

    def test_reword_an_item_and_what_thirties_keeps_about_it_follows(self):
        bank = self.bank()
        self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": bank.id})
        out = self.t.execute("edit_list_item", {"item": "Call the bank", "rename_to": "Call the credit union"})
        self.assertEqual(out, "Reworded 'Call the bank' to 'Call the credit union' in Errands/Admin.")
        self.assertIn("- [ ] Call the credit union\n", self.body("n_errands"))
        new_id = _checkbox_id("n_errands", "Call the credit union")
        span = next(s for s in self.t.day_plan.spans if s.title == "Bank")
        self.assertEqual(span.task_id, new_id)
        stored = self.sched.build_day_plan(self.t.day_plan.target_date)[0]
        self.assertEqual(next(s for s in stored.spans if s.title == "Bank").task_id, new_id)
        self.assertEqual(TodayLinks(self.sched.state_db).home_of("Call the bank")["home_id"], new_id)
        self.assertEqual(WriteLog(self.sched.state_db).since("")[-1]["added"], ["- [ ] Call the credit union"])

    def test_split_a_line_and_add_sub_items(self):
        out = self.t.execute("edit_list_item", {"item": "Buy stamps", "split_into": ["Buy stamps", "Buy envelopes"]})
        self.assertIn("Split 'Buy stamps' in Errands/Admin into: Buy stamps; Buy envelopes", out)
        self.assertIn("- [ ] Buy stamps\n- [ ] Buy envelopes\n", self.body("n_errands"))
        out = self.t.execute("edit_list_item", {"item": "Call the bank", "add_under": "Find the account number"})
        self.assertIn("Added under 'Call the bank'", out)
        self.assertIn("- [ ] Call the bank\n    - [ ] Find the account number\n", self.body("n_errands"))

    def test_move_between_sections(self):
        out = self.t.execute("edit_list_item", {"item": "Learn to juggle", "to_section": "Active"})
        self.assertEqual(out, "Moved 'Learn to juggle' under 'Active' in Errands/Admin.")
        self.assertEqual(self.body("n_errands"), "# Active\n- [ ] Call the bank\n- [ ] Buy stamps\n- [ ] Learn to juggle"
                                                 "\n\n# Probation\n")

    def test_one_change_at_a_time_and_only_exact_items(self):
        out = self.t.execute("edit_list_item", {"item": "Call the bank", "rename_to": "x", "to_section": "Probation"})
        self.assertTrue(out.startswith(NOT_DONE))
        out = self.t.execute("edit_list_item", {"item": "Call bank", "rename_to": "x"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Call the bank", self.body("n_errands"))

    def test_a_routine_changes_only_when_asked(self):
        # 10-17 (J7): "I'm looking at the morning routine note on Joplin right now and it still only has shower"
        self.t.user_message = "add that to my list"
        out = self.t.execute("edit_list_item", {"item": "Feed kitties", "add_under": ["Wet food", "Water"]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.t.user_message = "in my morning routine, feeding the kitties is wet food and water"
        out = self.t.execute("edit_list_item", {"item": "Feed kitties", "add_under": ["Wet food", "Water"]})
        self.assertIn("Added under 'Feed kitties' in Morning Routine", out)
        self.assertEqual(self.body("n_routine"), "- [ ] Feed kitties\n    - [ ] Wet food\n    - [ ] Water\n")


BIG = ("## This week\n- [ ] Call the dentist\n- [ ] Fix the closet door\n- [ ] Refill prescription\n"
       "## Later\n- [ ] Clean the gutters\n- [ ] Book a physical\n- [ ] Paint the fence\n- [ ] Get a flu shot\n"
       "## Someday\n- [ ] Retile the bathroom\n- [ ] Try acupuncture\n- [ ] Build a shed\n")


class TestTriage(ListsTestCase):
    notes = {**NOTES, "n_big": ("Big List", "1. Tasks", BIG, "list")}

    def test_a_list_sorted_by_time_is_spotted_offered_once_and_split_by_area(self):
        brain = FrontierBrain(self.t, MockInferenceEngine(), self.services.tasks, self.active_notes())
        self.assertIn("Sorted by time, not by area", brain.build_backlog_summary())
        out = self.t.execute("triage_list", {"list": "Big List"})
        self.assertIn("Big List is sorted by time, not by area. Its 10 items", out)
        self.assertIn("Call the dentist task_id=", out)
        self.assertIn("[This week → soon]", out)
        self.assertIn("Lists already in its notebook: ", out)
        self.assertNotIn("Sorted by time", brain.build_backlog_summary())  # offered once

        home = [t.id for t in self.services.tasks if t.parent_note_id == "n_big"
                and t.title in ("Fix the closet door", "Clean the gutters", "Paint the fence")]
        out = self.t.execute("apply_triage_group", {"list": "Big List", "items": home, "to_list": "Home"})
        self.assertIn("Moved 3 items from Big List to Home", out)
        self.assertEqual(self.body("n_home"), "- [ ] Fix the closet door\n- [ ] Clean the gutters\n- [ ] Fix the closet "
                                              "door\n- [ ] Clean the gutters\n- [ ] Paint the fence\n")
        urgency = ListCatalog(self.sched.state_db).urgencies()
        self.assertEqual(urgency[_checkbox_id("n_home", "Paint the fence")]["urgency"], "whenever")

        health = ["Call the dentist", "Refill prescription", "Book a physical", "Get a flu shot", "Try acupuncture"]
        out = self.t.execute("apply_triage_group", {"list": "Big List", "items": health, "new_list": "Health"})
        self.assertIn("Moved 5 items from Big List to Health", out)
        made = next(n for n in self.api.notes.values() if n.get("title") == "Health")
        self.assertEqual(made["parent_id"], "f_1. Tasks")  # beside the old one
        self.assertIn("- [ ] Try acupuncture", made["body"])
        self.assertEqual(ListCatalog(self.sched.state_db).role(made["id"]), "list")
        urgency = ListCatalog(self.sched.state_db).urgencies()
        self.assertEqual(urgency[_checkbox_id(made["id"], "Try acupuncture")]["urgency"], "someday")
        self.assertEqual(urgency[_checkbox_id(made["id"], "Refill prescription")]["urgency"], "soon")
        self.assertNotIn("Call the dentist", self.body("n_big"))
        self.assertIn("Build a shed", self.body("n_big"))  # not in a group: stays

    def test_only_items_of_that_list(self):
        out = self.t.execute("apply_triage_group", {"list": "Big List", "items": ["Buy stamps"], "to_list": "Home"})
        self.assertTrue(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
