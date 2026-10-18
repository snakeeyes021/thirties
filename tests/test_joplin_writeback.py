"""Writing back to Joplin: note-body edits, the outbox, and the add / break-down tools."""

import unittest
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import (JoplinWriter, append_task, check_off, clear_ticked, insert_subtasks,
                                           remove_item, uncheck, untick_all)
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

BODY = """# Errands
- [ ] Call the bank
    - [ ] Find the account number
- [ ] Taxes
- [x] Mail the form

## Someday
- [ ] Learn the banjo
"""


class TestBodyEdits(unittest.TestCase):

    def test_check_off_keeps_indentation_and_touches_one_line(self):
        out = check_off(BODY, "Find the account number")
        self.assertIn("    - [x] Find the account number\n", out)
        self.assertEqual(out.replace("    - [x] Find", "    - [ ] Find"), BODY)
        self.assertIsNone(check_off(BODY, "Not there"))

    def test_subtasks_nest_after_existing_children_in_the_notes_own_step(self):
        out = insert_subtasks(BODY, "Call the bank", ["Look up hours", "Make the call"])
        self.assertIn("    - [ ] Find the account number\n    - [ ] Look up hours\n    - [ ] Make the call\n- [ ] Taxes",
                      out)
        tabbed = insert_subtasks("- [ ] A\n\t- [ ] old\n- [ ] B\n", "B", ["x", "y"])
        self.assertEqual(tabbed, "- [ ] A\n\t- [ ] old\n- [ ] B\n\t- [ ] x\n\t- [ ] y\n")

    def test_append_to_a_section_or_the_end(self):
        out = append_task(BODY, "Return library books", "Errands")
        self.assertIn("- [x] Mail the form\n- [ ] Return library books\n\n## Someday", out)
        self.assertTrue(append_task(BODY, "Juggle").endswith("- [ ] Learn the banjo\n- [ ] Juggle\n"))
        self.assertIn("## Later\n- [ ] Nap", append_task(BODY, "Nap", "Later"))

    def test_the_days_list_edits(self):
        day = "- [x] Feed kitties\n- [ ] Shower\n    - [ ] Brush teeth\n- [x] PT\n\t- [ ] stretch\n- [ ] Lunch\n"
        self.assertEqual(remove_item(day, "Shower"), "- [x] Feed kitties\n- [x] PT\n\t- [ ] stretch\n- [ ] Lunch\n")
        self.assertIsNone(remove_item(day, "Nap"))
        self.assertNotIn("[x]", untick_all(day))
        self.assertEqual(clear_ticked(day), "- [ ] Shower\n    - [ ] Brush teeth\n- [ ] Lunch\n")
        self.assertIn("- [ ] PT", uncheck(day, "PT"))


class TestWriter(unittest.TestCase):

    def setUp(self):
        self.sched = make_scheduler()
        cfg = self.sched.config
        cfg.joplin.write_back, cfg.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n1": {"id": "n1", "body": BODY}}, up=False)
        self.writer = JoplinWriter(cfg, self.sched.state_db, self.api)

    def test_queued_while_joplin_is_closed_then_delivered_in_order(self):
        op = {"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Taxes"}
        self.assertEqual(self.writer.submit(op)[0], "queued")
        self.writer.submit({"op": "add_task", "note_id": "n1", "title": "Buy stamps", "section": "Errands"})
        self.assertEqual(self.writer.flush(launch=False), 0)  # still closed
        self.api.up = True
        self.assertEqual(self.writer.flush(), 2)
        self.assertIn("- [x] Taxes", self.api.notes["n1"]["body"])
        self.assertIn("- [ ] Buy stamps", self.api.notes["n1"]["body"])
        self.assertEqual(self.sched.state_db.outbox_pending(), [])

    def test_a_change_made_meanwhile_in_joplin_is_not_forced(self):
        self.writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Taxes"})
        self.api.up = True
        self.api.notes["n1"]["body"] = "# Errands\n- [ ] Something else\n"
        self.assertEqual(self.writer.flush(), 0)
        self.assertEqual(self.sched.state_db.outbox_pending(), [])  # dropped as failed, not retried forever
        self.assertEqual(self.api.updates, [])

    def test_nothing_happens_without_permission(self):
        self.sched.config.joplin.write_back = False
        self.api.up = True
        status, _ = self.writer.submit({"op": "add_task", "note_id": "n1", "title": "X"})
        self.assertEqual(status, "not_allowed")
        self.assertEqual(self.api.updates, [])

    def test_starts_joplin_when_allowed(self):
        self.sched.config.joplin.launch_when_needed = True
        self.writer.submit({"op": "add_task", "note_id": "n1", "title": "X"})

        def start():
            self.api.up = True
            return True
        with patch("thirties_core.joplin_writeback.launch_joplin", side_effect=start) as launch:
            self.assertEqual(self.writer.flush(wait_seconds=1), 1)
        launch.assert_called_once()


class TestTools(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({"n1": {"id": "n1", "body": BODY}})
        joplin = MagicMock()
        joplin.api_client = self.api
        tasks = [TaskItem(id="t_bank", source_notebook="Tasks", title="Call the bank", is_checkbox_item=True,
                          parent_note_title="Errands", parent_note_id="n1", deferred_count=3)]
        self.services = Services.for_db(self.sched.state_db, tasks, joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)

    def test_break_down_writes_subtasks_and_makes_them_schedulable(self):
        out = self.t.execute("break_down_task", {"task": "call the bank", "steps": ["Look up hours", "Make the call"]})
        self.assertIn("Broke 'Call the bank' into 2 steps", out)
        self.assertIn("    - [ ] Look up hours", self.api.notes["n1"]["body"])
        new = [t for t in self.services.tasks if t.parent_task_id == "t_bank"]
        self.assertEqual([t.title for t in new], ["Look up hours", "Make the call"])

    def test_routine_items_can_be_broken_down_even_when_ticked(self):
        routine = "# Morning\n- [x] Feed kitties\n- [x] Shower\n- [ ] PT\n"
        self.api.notes["r1"] = {"id": "r1", "body": routine}
        self.services.joplin.fetch_active_notes.return_value = [
            {"id": "n1", "title": "Errands", "body": BODY, "source_notebook": "Tasks"},
            {"id": "r1", "title": "Morning routine", "body": routine, "source_notebook": "Routines"}]
        out = self.t.execute("break_down_task", {"task": "shower", "steps": ["Shower", "Brush teeth"]})
        self.assertIn("Broke 'Shower' into 2 steps", out)
        self.assertEqual(self.api.notes["r1"]["body"],
                         "# Morning\n- [x] Feed kitties\n- [x] Shower\n    - [ ] Shower\n    - [ ] Brush teeth\n- [ ] PT\n")
        out = self.t.execute("add_task", {"title": "Stretch", "list": "morning routine"})
        self.assertTrue(out.startswith("Added 'Stretch' to Morning routine"), out)

    def test_an_item_in_two_lists_needs_the_list_named(self):
        self.services.joplin.fetch_active_notes.return_value = [
            {"id": "n1", "title": "Errands", "body": "- [x] Shower\n"},
            {"id": "r1", "title": "Morning routine", "body": "- [x] Shower\n"}]
        self.api.notes["r1"] = {"id": "r1", "body": "- [x] Shower\n"}
        self.assertIn("more than one list", self.t.execute("break_down_task", {"task": "Shower", "steps": ["a", "b"]}))
        out = self.t.execute("break_down_task", {"task": "Shower", "steps": ["a", "b"], "list": "routine"})
        self.assertIn("Broke 'Shower'", out)
        self.assertIn("    - [ ] a", self.api.notes["r1"]["body"])

    def test_add_task_needs_a_clear_list(self):
        self.assertTrue(self.t.execute("add_task", {"title": "Buy stamps", "list": ""}).startswith(NOT_DONE))
        out = self.t.execute("add_task", {"title": "Buy stamps", "list": "errands"})
        self.assertTrue(out.startswith("Added 'Buy stamps' to Errands. To schedule it, use task_id="), out)
        self.assertIn("- [ ] Buy stamps", self.api.notes["n1"]["body"])


    def test_match_backlog_tells_same_from_merely_similar(self):
        from thirties_core.models import TaskItem as T
        self.services.tasks.append(T(id="t_brad", source_notebook="Tasks", title="Message Brad about a weekend trip",
                                     is_checkbox_item=True, parent_note_title="Errands", parent_note_id="n1"))
        self.assertTrue(self.t.execute("match_backlog", {"text": "call the bank"}).startswith("Same task: Call the bank"))
        out = self.t.execute("match_backlog", {"text": "message Brad back"})
        self.assertTrue(out.startswith("No exact match; these are only similar"), out)
        self.assertIn("Message Brad about a weekend trip", out)
        self.assertEqual(self.t.execute("match_backlog", {"text": "feed the cats"}),
                         "Nothing on the lists matches: treat it as a new item.")


if __name__ == "__main__":
    unittest.main()
