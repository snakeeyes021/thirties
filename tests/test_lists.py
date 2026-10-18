"""List roles and item urgency: stored, applied to the backlog, set in conversation, synced."""

import unittest
from datetime import date
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.backlog import load_backlog
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools


def item(tid, title, note="n_errands", note_title="Errands"):
    return TaskItem(id=tid, source_notebook="Tasks", title=title, is_checkbox_item=True,
                    parent_note_title=note_title, parent_note_id=note)


class TestCatalog(unittest.TestCase):

    def setUp(self):
        self.db = make_scheduler().state_db
        self.c = ListCatalog(self.db)

    def test_a_guess_never_replaces_the_users_choice(self):
        self.assertTrue(self.c.set_role("n1", "list", "Home", source="guess"))
        self.assertTrue(self.c.set_role("n1", "menu", "Home", source="user"))
        self.assertFalse(self.c.set_role("n1", "reference", "Home", source="guess"))
        self.assertEqual(self.c.role("n1"), "menu")
        self.c.set_urgency("t1", "whenever", source="user")
        self.assertFalse(self.c.set_urgency("t1", "now", source="guess"))
        self.assertEqual(self.c.urgencies()["t1"]["urgency"], "whenever")

    def test_applying_roles_and_urgency_to_the_backlog(self):
        self.c.set_role("n_errands", "list", "Errands")
        self.c.set_role("n_routine", "routine", "Morning routine")
        self.c.set_role("n_junk", "ignore", "Cheat sheet")
        self.c.set_urgency("t_bank", "soon", due=date(2026, 10, 30))
        self.c.set_urgency("t_banjo", "someday")
        tasks = [item("t_bank", "Call the bank"), item("t_banjo", "Learn the banjo"),
                 item("t_shower", "Shower", "n_routine", "Morning routine"), item("t_x", "Python", "n_junk", "Cheat sheet")]
        out = {t.id: t for t in self.c.apply(tasks)}
        self.assertNotIn("t_x", out)  # ignored notes aren't read
        self.assertEqual((out["t_bank"].urgency, out["t_bank"].due), ("soon", "2026-10-30"))
        self.assertTrue(out["t_banjo"].someday)  # someday items are never pushed, by any brain
        self.assertEqual((out["t_shower"].list_role, out["t_shower"].urgency), ("routine", "now"))

    def test_the_users_urgency_beats_a_someday_heading(self):
        gutter = item("t_gutter", "Fix the gutter")
        gutter.someday = True  # it sits under a Someday heading
        self.c.set_urgency("t_gutter", "now", source="user")
        self.assertFalse(self.c.apply([gutter])[0].someday)
        maybe = item("t_trip", "Plan a trip")
        maybe.someday = True
        self.c.set_urgency("t_trip", "soon", source="guess")
        self.assertTrue(self.c.apply([maybe])[0].someday)  # a guess doesn't take the heading away

    def test_what_still_needs_a_guess(self):
        self.c.set_role("n_errands", "list", "Errands")
        self.c.set_role("n_routine", "routine", "Morning routine")
        self.c.set_urgency("t_bank", "soon")
        notes = [{"id": "n_errands"}, {"id": "n_routine"}, {"id": "n_new"}]
        self.assertEqual([n["id"] for n in self.c.notes_without_role(notes)], ["n_new"])
        tasks = [item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps"),
                 item("t_shower", "Shower", "n_routine", "Morning routine")]
        self.assertEqual({k: [t.id for t in v] for k, v in self.c.items_without_urgency(tasks).items()},
                         {"n_errands": ["t_stamps"]})  # routine items take their urgency from the role

    def test_syncs_last_write_wins(self):
        self.c.set_role("n1", "list", "Home", source="guess")
        self.c.set_urgency("t1", "soon", note_id="n1", title="Fix the door")
        other = ListCatalog(make_scheduler().state_db)
        for record in self.c.all_records():
            self.assertTrue(other.merge_record(record))
        self.assertEqual(other.role("n1"), "list")
        self.assertEqual(other.urgencies()["t1"]["urgency"], "soon")
        self.assertFalse(other.merge_record(self.c.all_records()[0]))  # nothing newer: no change


class TestInUse(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()

    def test_the_user_says_how_pressing_something_is(self):
        tasks = [item("t_thermals", "Thermals for this winter")]
        services = Services.for_db(self.sched.state_db, tasks)
        plan, pending = self.sched.build_day_plan(DAY)
        t = PlanTools(plan, pending, self.sched, services=services)
        out = t.execute("set_urgency", {"task": "thermals", "due": "Nov 1"})
        self.assertTrue(out.startswith("Noted: 'Thermals for this winter' is by Nov 1"), out)
        row = ListCatalog(self.sched.state_db).urgencies()["t_thermals"]
        self.assertEqual((row["urgency"], row["source"]), ("soon", "user"))
        self.assertTrue(t.execute("set_urgency", {"task": "thermals", "urgency": "asap"}).startswith(NOT_DONE))

    def test_the_backlog_skips_ignored_notes(self):
        joplin = MagicMock()
        joplin.fetch_tasks.return_value = [item("t_bank", "Call the bank"), item("t_x", "Python", "n_junk")]
        joplin.fetch_active_notes.return_value = [{"id": "n_errands", "title": "Errands"},
                                                  {"id": "n_junk", "title": "Cheat sheet"}]
        ListCatalog(self.sched.state_db).set_role("n_junk", "ignore", "Cheat sheet")
        tasks, notes = load_backlog(self.sched.config, joplin, self.sched)
        self.assertEqual([t.id for t in tasks], ["t_bank"])
        self.assertEqual([n["id"] for n in notes], ["n_errands"])


class TestBrainsUseThem(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_errands", "list", "Errands")
        catalog.set_role("n_wish", "reference", "Wishlist")
        catalog.set_urgency("t_bank", "soon")
        catalog.set_urgency("t_stamps", "whenever")
        self.tasks = catalog.apply([item("t_bank", "Call the bank"), item("t_stamps", "Buy stamps"),
                                    item("t_pedal", "Buy a pedal", "n_wish", "Wishlist")])
        plan, pending = self.sched.build_day_plan(DAY)
        self.tools = PlanTools(plan, pending, self.sched, services=Services.for_db(self.sched.state_db, self.tasks))

    def test_frontier_sees_what_each_list_is_for_and_whats_pressing(self):
        from thirties_core.brains.frontier import FrontierBrain
        notes = [{"id": "n_errands", "source_notebook": "Tasks", "title": "Errands", "body": ""},
                 {"id": "n_wish", "source_notebook": "Tasks", "title": "Wishlist", "body": ""}]
        text = FrontierBrain(self.tools, None, tasks=self.tasks, active_notes=notes).build_backlog_summary()
        self.assertIn("'Call the bank' [soon]", text)
        self.assertIn("'Buy stamps'\n", text + "\n")  # no hurry: no tag
        self.assertIn('"Wishlist" [Reference / wishlist: never suggest unless asked]', text)
        ListCatalog(self.sched.state_db).set_role("n_wish", "backlog", "Wishlist")
        text = FrontierBrain(self.tools, None, tasks=self.tasks, active_notes=notes).build_backlog_summary()
        self.assertIn("[Project backlog: plan time on the project as one item", text)

    def test_the_slm_favours_whats_pressing_and_never_offers_reference_items(self):
        from thirties_core.brains.slm import SlmBrain
        brain = SlmBrain(self.tools, tasks=self.tasks)
        score = {t.id: brain._score_task(t, set(), set(), set()) for t in self.tasks}
        self.assertGreater(score["t_bank"], score["t_stamps"])
        self.assertEqual(score["t_pedal"], -1.0)
        ListCatalog(self.sched.state_db).set_role("n_errands", "backlog", "Errands")
        brain.tasks = ListCatalog(self.sched.state_db).apply(brain.tasks)
        self.assertEqual([t.title for t in brain._project_blocks()], ["Errands dev"])
        self.assertEqual(brain._score_task(brain.tasks[0], set(), set(), set()), -1.0)  # no single tickets


if __name__ == "__main__":
    unittest.main()
