"""New items go on the right list and in the right section, without asking (B1, J5); the drop box (soak 5)."""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_engine import checklist_tasks
from thirties_core.joplin_writeback import WriteLog
from thirties_core.lists import ListCatalog
from thirties_core.placement_lists import DropBoxScopes, Placement, dropbox_offer
from thirties_core.plan_tools import NOT_DONE, PlanTools

ERRANDS = "# Active\n- [ ] Call the bank\n- [ ] Buy stamps\n\n# Probation\n- [ ] Learn to juggle\n"
HOME = "- [ ] Fix the closet door\n- [ ] Clean the gutters\n"
NOTES = {
    "n_today": ("Today To-Do", "1. Tasks", "", "today"),
    "n_errands": ("Errands/Admin", "1. Tasks", ERRANDS, "list"),
    "n_home": ("Home", "1. Tasks", HOME, "list"),
    "n_routine": ("Morning Routine", "1. Tasks", "- [ ] Feed kitties\n", "routine"),
    "n_wish": ("Do I want?", "1. Tasks", "- [ ] A boat\n", "reference"),
    "n_quick": ("Assorted Quickies", "Dev", "- [ ] Tell Claude re: machine id\n- [ ] Look into X\n- [ ] Rename Y\n",
                "dropbox"),
}


class ListsTestCase(unittest.TestCase):
    notes = NOTES

    def setUp(self):
        set_debug_time(at("14:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.joplin.write_back, self.sched.config.joplin.api_token = True, "tok"
        self.api = FakeJoplinAPI({nid: {"id": nid, "title": t, "body": body, "parent_id": "f_" + nb}
                                  for nid, (t, nb, body, _) in self.notes.items()})
        self.meta = {nid: (t, nb) for nid, (t, nb, _, _) in self.notes.items()}
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.fetch_active_notes.side_effect = self.active_notes
        joplin.fetch_tasks.side_effect = self.read_tasks
        self.catalog = ListCatalog(self.sched.state_db)
        for nid, (title, _, _, role) in self.notes.items():
            self.catalog.set_role(nid, role, title)
        self.services = Services.for_db(self.sched.state_db, self.read_tasks(), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)

    def active_notes(self):
        return [{"id": nid, "title": n.get("title", ""), "source_notebook": self.meta.get(nid, ("", "Thirties"))[1],
                 "body": n["body"]} for nid, n in self.api.notes.items()]

    def read_tasks(self):
        tasks = []
        for n in self.active_notes():
            tasks += checklist_tasks(n["id"], n["title"], n["source_notebook"], n["body"], ["Probation"])
        return self.catalog.apply(tasks) if hasattr(self, "catalog") else tasks

    def body(self, nid):
        return self.api.notes[nid]["body"]


class TestPlacement(ListsTestCase):

    def test_a_dated_item_never_goes_under_a_someday_section(self):
        # 10-24 23:43: "Set an item for tomorrow" went into Errands/Admin under Probation
        out = self.t.execute("add_task", {"title": "Renew passport", "list": "Errands/Admin", "section": "Probation",
                                          "due": "tomorrow"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("someday section", out)
        self.assertIn("'Active'", out)
        out = self.t.execute("add_task", {"title": "Renew passport", "list": "Errands/Admin", "due": "tomorrow",
                                          "reason": "an errand"})
        self.assertTrue(out.startswith("Added 'Renew passport' to Errands/Admin under 'Active'"), out)
        self.assertEqual(self.body("n_errands"), "# Active\n- [ ] Call the bank\n- [ ] Buy stamps\n- [ ] Renew passport"
                                                 "\n\n# Probation\n- [ ] Learn to juggle\n")
        new = next(t for t in self.services.tasks if t.title == "Renew passport")
        self.assertEqual((new.urgency, new.due), ("now", (DAY + timedelta(days=1)).isoformat()))
        write = WriteLog(self.sched.state_db).since("")[-1]
        self.assertEqual(write["op"]["reason"], "an errand")  # the model's reason is in the log
        self.assertEqual(write["added"], ["- [ ] Renew passport"])

    def test_with_no_section_the_obvious_one_never_the_last(self):
        out = self.t.execute("add_task", {"title": "Mail the stamps", "list": "errands"})
        self.assertIn("under 'Active'", out)
        self.assertTrue(self.body("n_errands").endswith("- [ ] Learn to juggle\n"))  # not after the someday item
        self.assertIn("- [ ] Buy stamps\n- [ ] Mail the stamps\n", self.body("n_errands"))

    def test_an_undated_maybe_can_go_where_the_model_put_it(self):
        out = self.t.execute("add_task", {"title": "Learn to whistle", "list": "Errands/Admin", "section": "probation"})
        self.assertIn("under 'Probation'", out)
        self.assertTrue(self.body("n_errands").endswith("- [ ] Learn to juggle\n- [ ] Learn to whistle\n"))

    def test_a_section_that_isnt_there_is_refused_not_made(self):
        out = self.t.execute("add_task", {"title": "x", "list": "Errands/Admin", "section": "Urgent"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("Urgent", self.body("n_errands"))

    def test_no_list_gives_the_model_the_candidates_not_a_question(self):
        out = self.t.execute("add_task", {"title": "Clean the windows"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("don't ask the user", out)
        self.assertIn("Home [list]", out)  # shares "clean" with the gutters
        self.assertLess(out.index("Home [list]"), out.index("Errands/Admin [list]"))
        self.assertIn("Assorted Quickies", out)
        self.assertIn("drop box", out)

    def test_wrong_kinds_of_note_are_refused(self):
        for lst in ("Do I want?", "Today To-Do"):
            out = self.t.execute("add_task", {"title": "Something", "list": lst})
            self.assertTrue(out.startswith(NOT_DONE), (lst, out))
        self.assertIn("add_to_today", self.t.execute("add_task", {"title": "x", "list": "Today To-Do"}))
        self.t.user_message = "add buy milk somewhere"
        self.assertTrue(self.t.execute("add_task", {"title": "Buy milk", "list": "Morning Routine"}).startswith(NOT_DONE))
        self.assertEqual(self.body("n_wish"), "- [ ] A boat\n")

    def test_with_joplin_closed_it_still_never_lands_in_the_trailing_someday_section(self):
        self.api.up = False
        self.services.joplin.fetch_active_notes.side_effect = lambda: []  # nothing read: the note's body is unknown
        out = self.t.execute("add_task", {"title": "Renew passport", "list": "Errands/Admin", "due": "Friday"})
        self.assertIn("Joplin will get it when it's open", out)
        self.api.up = True
        from thirties_core.joplin_writeback import JoplinWriter
        JoplinWriter(self.sched.config, self.sched.state_db, self.api).flush(launch=False)
        self.assertIn("- [ ] Buy stamps\n- [ ] Renew passport\n\n# Probation", self.body("n_errands"))

    def test_a_move_that_fails_halfway_is_put_back_so_a_retry_cant_copy_it_twice(self):
        stamps = next(t for t in self.services.tasks if t.title == "Buy stamps")
        real = self.api.update_note
        self.api.update_note = lambda nid, data: False if nid == "n_errands" and "Buy stamps" not in data["body"] \
            else real(nid, data)  # Joplin takes the copy in Home, then refuses to take it out of Errands
        out = self.t.execute("edit_list_item", {"item": stamps.id, "to_list": "Home"})
        self.assertIn("Joplin will get it when it's open", out)  # queued for a retry
        self.assertEqual(self.body("n_home"), HOME)  # the copy was taken back
        self.assertIn("Buy stamps", self.body("n_errands"))

    def test_the_drop_box_by_name(self):
        out = self.t.execute("add_task", {"title": "Check the fan noise", "list": "drop box", "reason": "unclear"})
        self.assertTrue(out.startswith("Added 'Check the fan noise' to Assorted Quickies"), out)
        self.assertTrue(self.body("n_quick").endswith("- [ ] Check the fan noise\n"))


class TestSmallModel(ListsTestCase):
    """The on-device brain doesn't choose: Python takes a clearly best list, else the drop box."""

    def test_clear_best_list_else_the_drop_box(self):
        from thirties_core.brains.slm.brain import SlmBrain
        from thirties_core.brains.slm.parse import Action
        brain = SlmBrain(self.t, tasks=list(self.services.tasks))
        out, _ = brain._do_task_add(Action("task_add", {"title": "Clean the windows"}))
        self.assertTrue(out.startswith("Added 'Clean the windows' to Home"), out)
        out, _ = brain._do_task_add(Action("task_add", {"title": "Ask Zein about the trip"}))
        self.assertTrue(out.startswith("Added 'Ask Zein about the trip' to Assorted Quickies"), out)


class TestDropBox(ListsTestCase):

    def test_scope_one_box_covers_everything_several_their_own_notebooks(self):
        p = Placement.for_assistant(self.t.assistant)
        self.assertEqual(p.dropbox_for("1. Tasks")["id"], "n_quick")
        self.catalog.set_role("n_home", "dropbox", "Home")
        p = Placement.for_assistant(self.t.assistant)
        self.assertEqual(p.dropbox_for("1. Tasks")["id"], "n_home")
        self.assertEqual(p.dropbox_for("Dev")["id"], "n_quick")
        self.assertIsNone(p.dropbox_for("Elsewhere"))
        DropBoxScopes(self.sched.state_db).set("n_quick", "all")
        p = Placement.for_assistant(self.t.assistant)
        self.assertEqual(p.dropbox_for("Elsewhere")["id"], "n_quick")

    def test_set_list_role_in_conversation(self):
        out = self.t.execute("set_list_role", {"list": "Home", "role": "drop box", "scope": "notebook"})
        self.assertIn("Noted: Home is a drop box", out)
        self.assertEqual(self.catalog.role("n_home"), "dropbox")
        self.assertEqual(DropBoxScopes(self.sched.state_db).all()["n_home"], "notebook")

    def test_review_proposes_homes_and_moving_one_is_one_logged_write(self):
        self.api.notes["n_quick"]["body"] += "- [ ] Buy more stamps\n"
        self.services.tasks = self.read_tasks()
        DropBoxScopes(self.sched.state_db).set("n_quick", "all")
        out = self.t.execute("dropbox_review", {})
        self.assertIn("Assorted Quickies (drop box for everything) holds 4", out)
        self.assertIn("- Buy more stamps task_id=", out)
        self.assertIn("likely Errands/Admin ('Active')", out)
        stamps = next(t for t in self.services.tasks if t.title == "Buy more stamps")
        out = self.t.execute("edit_list_item", {"item": stamps.id, "to_list": "Errands/Admin", "reason": "an errand"})
        self.assertEqual(out, "Moved 'Buy more stamps' from Assorted Quickies to Errands/Admin under 'Active'.")
        self.assertNotIn("Buy more stamps", self.body("n_quick"))
        self.assertIn("- [ ] Buy stamps\n- [ ] Buy more stamps\n", self.body("n_errands"))
        writes = WriteLog(self.sched.state_db).since("")
        self.assertEqual({w["note_id"] for w in writes}, {"n_quick", "n_errands"})  # both notes, both logged
        self.assertEqual(stamps.parent_note_id, "n_errands")

    def test_the_offer_comes_now_and_then_in_the_afternoon(self):
        p = Placement.for_assistant(self.t.assistant)
        db = self.sched.state_db
        self.assertEqual(dropbox_offer(p, db, at("09:00")), "")  # not first thing
        offer = dropbox_offer(p, db, at("14:00"))
        self.assertIn("3 things are waiting in Assorted Quickies", offer)
        self.assertEqual(dropbox_offer(p, db, at("15:00")), "")  # not again so soon
        self.assertEqual(dropbox_offer(p, db, at("14:00", 3)), offer)


if __name__ == "__main__":
    unittest.main()
