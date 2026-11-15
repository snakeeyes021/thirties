"""Lists and projects: menu lists keep one-offs out, a new list made on request, a chore done by
someone else at home, one-off notes, done items into a note the user picks, parts of a
project, epics and the user's own estimates. Every list, note and item here is invented."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_list_placement import NOTES, ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.done_log import DoneLog
from thirties_core.joplin_writeback import WriteLog
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE

LANE_NOTES = {
    **NOTES,
    "n_menu": ("Free time ideas", "1. Tasks", "- [ ] Go for a walk\n- [ ] Sketch something\n", "menu"),
    "n_log": ("Completed log", "1. Tasks", "My finished things, newest last.\n", "reference"),
    "n_sv": ("Lighthouse Radio", "2. Dev", "# Station app\n- [ ] Fix the tuner\n- [ ] Add presets\n", "backlog"),
    "n_garage": ("Garage rebuild", "1. Tasks", "- [ ] Pour the slab\n- [ ] Frame the walls\n- [ ] Hang the door\n",
                 "project"),
}
FOLDERS = {"f_1. Tasks": {"id": "f_1. Tasks", "title": "1. Tasks", "parent_id": ""},
           "f_Dev": {"id": "f_Dev", "title": "Dev", "parent_id": ""},
           "f_2. Dev": {"id": "f_2. Dev", "title": "2. Dev", "parent_id": ""},
           "f_Archive": {"id": "f_Archive", "title": "Archive", "parent_id": ""}}


class LaneCase(ListsTestCase):
    notes = LANE_NOTES

    def setUp(self):
        super().setUp()
        self.api.folders = {k: dict(v) for k, v in FOLDERS.items()}
        self.before = {nid: n["body"] for nid, n in self.api.notes.items()}

    def task(self, title):
        return next(t for t in self.services.tasks if t.title == title)

    def untouched(self, *but):
        for nid, body in self.before.items():
            if nid not in but and nid in self.api.notes:
                self.assertEqual(self.api.notes[nid]["body"], body, nid)


class TestMenuLists(LaneCase):
    """A menu list holds options picked again and again: nothing one-off is filed in it."""

    def test_a_dated_item_never_goes_on_a_menu(self):
        self.t.user_message = "add renew the passport by friday"
        out = self.t.execute("add_task", {"title": "Renew the passport", "list": "Free time ideas", "due": "Friday"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("is a menu", out)
        self.assertIn("Errands/Admin", out)  # where it could go instead
        self.untouched()

    def test_an_item_called_a_one_off_never_goes_on_a_menu(self):
        self.t.user_message = "add the plumber visit"
        out = self.t.execute("add_task", {"title": "Book the plumber", "list": "Free time ideas", "kind": "once"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.untouched()

    def test_an_option_for_free_time_goes_on_the_menu(self):
        self.t.user_message = "add try the new bakery to my free time ideas"
        out = self.t.execute("add_task", {"title": "Try the new bakery", "list": "Free time ideas"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [ ] Try the new bakery", self.body("n_menu"))
        self.untouched("n_menu")

    def test_a_dated_item_isnt_moved_onto_a_menu_either(self):
        ListCatalog(self.sched.state_db).set_urgency(self.task("Call the bank").id, "now")
        self.services.tasks[:] = self.catalog.apply(self.services.tasks)
        self.t.user_message = "move call the bank to free time ideas"
        out = self.t.execute("edit_list_item", {"item": "Call the bank", "action": "move", "to": "Free time ideas"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.untouched()


class TestCreateList(LaneCase):
    """A new list is made only when the user asks for one, or agrees to one Thirties suggests."""

    def test_made_on_request_in_a_notebook_thirties_reads(self):
        self.t.user_message = "yes, make a Garden list in 1. Tasks"
        out = self.t.execute("create_list", {"title": "Garden", "notebook": "1. Tasks",
                                             "items": ["Plant the bulbs", "Rake the leaves"]})
        self.assertTrue(out.startswith("Made the list Garden in 1. Tasks"), out)
        made = [n for n in self.api.notes.values() if n.get("title") == "Garden"]
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["body"], "- [ ] Plant the bulbs\n- [ ] Rake the leaves\n")
        self.assertEqual(made[0]["parent_id"], "f_1. Tasks")
        self.assertEqual(ListCatalog(self.sched.state_db).role(made[0]["id"]), "list")
        writes = [w for w in WriteLog(self.sched.state_db).since("") if w["op"]["op"] == "create_list"]
        self.assertEqual([w["note_id"] for w in writes], [made[0]["id"]])  # logged, one note
        self.untouched()  # nothing else changed

    def test_only_when_the_user_asked_or_agreed(self):
        self.t.user_message = "the garden needs work this weekend"
        out = self.t.execute("create_list", {"title": "Weekend garden", "notebook": "1. Tasks"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertFalse(any(n.get("title") == "Weekend garden" for n in self.api.notes.values()))

    def test_never_a_second_note_of_the_same_name(self):
        self.t.user_message = "make a list called Home"
        out = self.t.execute("create_list", {"title": "Home", "notebook": "1. Tasks"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(sum(1 for n in self.api.notes.values() if n.get("title") == "Home"), 1)

    def test_never_in_a_notebook_it_doesnt_read(self):
        self.t.user_message = "make a list called Old stuff in Archive"
        out = self.t.execute("create_list", {"title": "Old stuff", "notebook": "Archive"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("doesn't read", out)
        self.assertFalse(any(n.get("title") == "Old stuff" for n in self.api.notes.values()))

    def test_never_a_routine_or_todays_list(self):
        self.t.user_message = "make an evening routine list"
        out = self.t.execute("create_list", {"title": "Evening", "notebook": "1. Tasks", "role": "routine"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_a_retry_after_joplin_went_away_makes_no_second_note(self):
        from thirties_core.note_ops import create_list_op
        writer = self.t.assistant._writer()
        op = writer.stamp(create_list_op("Garden", "1. Tasks", []))
        self.assertTrue(writer._apply(op)[0])
        self.assertTrue(writer._apply(op)[0])  # delivered again from the outbox
        self.assertEqual(sum(1 for n in self.api.notes.values() if n.get("title") == "Garden"), 1)

    def test_queued_while_joplin_is_closed(self):
        self.api.up = False
        self.t.user_message = "yes make the Garden list"
        out = self.t.execute("create_list", {"title": "Garden", "notebook": "1. Tasks"})
        self.assertIn("as soon as Joplin is open", out)


class TestDoneNote(LaneCase):
    """(10-09: done items dropped "into a specific Thirties completed tasks Note")."""

    def setUp(self):
        super().setUp()
        self.log = DoneLog(self.sched.state_db)
        self.log.sweep(self.t.assistant)

    def choose(self):
        self.t.user_message = "put my done items in my completed log"
        out = self.t.execute("set_list_settings", {"done_note": "Completed log"})
        self.assertIn("done items go to Completed log", out)

    def do_bank(self):
        self.t.user_message = "called the bank"  # a done goes with the user's words about it (done_reference.py)
        self.t.execute("log_outcome", {"event": self.task("Call the bank").id, "status": "done"})
        set_debug_time(at("14:05"))
        self.log.sweep(self.t.assistant)
        set_debug_time(at("14:45"))
        return self.log.sweep(self.t.assistant)

    def test_filed_in_the_chosen_note(self):
        self.choose()
        self.assertEqual(self.do_bank(), ["filed Call the bank from Errands/Admin"])
        self.assertEqual(self.body("n_log"), "My finished things, newest last.\n"
                                             "- [x] 2026-10-15: Call the bank (Errands/Admin)\n")
        self.assertNotIn("Call the bank", self.body("n_errands"))
        self.assertFalse(any((n.get("title") or "").startswith("Done ") for n in self.api.notes.values()))
        self.untouched("n_log", "n_errands")

    def test_undo_takes_it_back_out_of_the_chosen_note(self):
        self.choose()
        self.do_bank()
        out = self.t.execute("reopen_item", {"item": "Call the bank"})
        self.assertIn("back on Errands/Admin", out)
        self.assertEqual(self.body("n_log"), "My finished things, newest last.\n")
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))

    def test_unset_it_goes_to_the_months_done_note(self):
        self.do_bank()
        self.assertTrue(any(n.get("title") == "Done 2026-10" for n in self.api.notes.values()))
        self.assertEqual(self.body("n_log"), "My finished things, newest last.\n")

    def test_a_chosen_note_gone_from_joplin_falls_back_to_the_month(self):
        self.choose()
        del self.api.notes["n_log"]
        self.assertEqual(self.do_bank(), ["filed Call the bank from Errands/Admin"])
        done = next(n for n in self.api.notes.values() if n.get("title") == "Done 2026-10")
        self.assertIn("Call the bank", done["body"])

    def test_never_one_of_the_lists(self):
        self.t.user_message = "put done items in Home"
        out = self.t.execute("set_list_settings", {"done_note": "Home"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_never_a_routine_note_even_if_set_behind_its_back(self):
        from thirties_core.list_settings import set_done_note
        out = self.t.execute("set_list_settings", {"done_note": "Morning Routine"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        set_done_note(self.sched.state_db, "n_routine", "Morning Routine")  # say a synced setting points there
        self.do_bank()
        self.assertEqual(self.body("n_routine"), self.before["n_routine"])  # routine notes are never written
        self.assertEqual(ListCatalog(self.sched.state_db).role("n_routine"), "routine")
        self.assertTrue(any(n.get("title") == "Done 2026-10" for n in self.api.notes.values()))

    def test_back_to_the_monthly_notes(self):
        from thirties_core.list_settings import done_note
        self.choose()
        self.t.execute("set_list_settings", {"done_note": "monthly"})
        self.assertIsNone(done_note(self.sched.state_db))


class TestOneOffNotes(LaneCase):
    """How a to-do note with no checklist is read is the user's choice."""

    def one_off(self):
        return TaskItem(id="n_dentist", source_notebook="1. Tasks", title="Book the dentist", is_checkbox_item=False)

    def test_by_default_one_is_an_item(self):
        from thirties_core.list_settings import apply_one_off
        out = apply_one_off(self.sched.state_db, [self.one_off()])
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].someday)

    def test_someday_or_not_read(self):
        from thirties_core.list_settings import apply_one_off, set_one_off_mode
        set_one_off_mode(self.sched.state_db, "someday")
        self.assertTrue(apply_one_off(self.sched.state_db, [self.one_off()])[0].someday)
        set_one_off_mode(self.sched.state_db, "ignore")
        self.assertEqual(apply_one_off(self.sched.state_db, [self.one_off(), self.task("Call the bank")]),
                         [self.task("Call the bank")])  # list lines are never touched

    def test_a_note_with_a_role_the_user_gave_keeps_it(self):
        from thirties_core.list_settings import apply_one_off, set_one_off_mode
        set_one_off_mode(self.sched.state_db, "ignore")
        ListCatalog(self.sched.state_db).set_role("n_dentist", "list", "Book the dentist", source="user")
        self.assertEqual(len(apply_one_off(self.sched.state_db, [self.one_off()])), 1)

    def test_the_whole_structure_in_one_view(self):
        from thirties_core.list_settings import structure
        self.t.execute("set_list_settings", {"one_off_notes": "someday"})
        view = structure(self.sched.state_db, self.sched.config)
        self.assertEqual(view["one_off_notes"], "someday")
        self.assertEqual(view["excluded_notebooks"], ["Archive"])
        self.assertIn({"id": "n_menu", "title": "Free time ideas", "role": "menu", "role_source": "user",
                       "horizon": ""}, view["lists"])

    def test_synced_as_the_persons_setting(self):
        from thirties_core.settings import SETTINGS
        for key in ("lists.one_off_notes", "lists.done_note", "lists.effort_unit", "lists.effort_unit_minutes"):
            self.assertEqual(SETTINGS[key].scope, "shared")


class TestDoneBySomeone(LaneCase):
    """Done by the user or by someone else at home, an item is done: it isn't carried over."""

    def test_finished_ticked_and_not_the_users_time(self):
        bank = self.task("Buy stamps")
        self.t.execute("add_event", {"title": "Buy stamps", "start": "3:00 PM", "duration": "30 min",
                                     "task_id": bank.id, "category": "task"})
        set_debug_time(at("15:40"))
        self.t.user_message = "Sam bought the stamps"
        out = self.t.execute("done_by_someone", {"event": "Buy stamps", "who": "Sam"})
        self.assertIn("Done by Sam", out)
        span = next(s for s in self.t.day_plan.spans if s.title == "Buy stamps")
        self.assertEqual(span.status, SpanStatus.DONE)
        self.assertEqual(span.time_source, "estimated")  # not the user's own time
        self.assertIn("- [x] Buy stamps", self.body("n_errands"))
        from thirties_core.carry import OpenPool, settle
        set_debug_time(at("09:00", 1))
        settle(self.t.assistant, DAY + timedelta(days=1))
        self.assertNotIn("buy stamps", OpenPool(self.sched.state_db).all())  # not rolled over


class TestProjectParts(LaneCase):
    """(10-30: a named piece is part of a project, though it's not a line in the project's list)."""

    def span(self, title, start="4:00 PM"):
        self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def test_a_part_the_user_named(self):
        from thirties_core.project_pieces import project_of
        self.t.user_message = "the tuner rewrite is part of lighthouse radio"
        out = self.t.execute("set_project_part", {"name": "tuner rewrite", "project": "Lighthouse Radio"})
        self.assertIn("tuner rewrite is part of Lighthouse Radio", out)
        self.assertEqual(project_of(self.t.assistant, self.span("Tuner rewrite")), "n_sv")
        self.assertEqual(project_of(self.t.assistant, self.span("Work on the tuner rewrite", "5:00 PM")), "n_sv")

    def test_a_heading_in_the_list_and_time_on_it_by_name(self):
        from thirties_core.project_pieces import project_of
        self.assertEqual(project_of(self.t.assistant, self.span("Station app")), "n_sv")
        self.assertEqual(project_of(self.t.assistant, self.span("Time on Lighthouse Radio", "5:00 PM")), "n_sv")
        self.assertIsNone(project_of(self.t.assistant, self.span("Lunch", "12:00 PM")))

    def test_time_past_its_end_counts_for_the_project(self):
        self.t.execute("set_project_part", {"name": "tuner rewrite", "project": "Lighthouse Radio"})
        span = self.span("Tuner rewrite", "1:00 PM")
        set_debug_time(at("14:10"))
        out = self.t.execute("log_outcome", {"event": span.id, "status": "done", "started": "1:00 PM",
                                             "finished": "2:00 PM"})
        self.assertIn("counts as time on Tuner rewrite and on Lighthouse Radio", out)

    def test_only_a_projects_list(self):
        out = self.t.execute("set_project_part", {"name": "stamps", "project": "Errands/Admin"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_parts_of_a_backlog_are_never_offered_as_tickets(self):
        titles = [c["title"] for c in self.t.assistant.proposals.candidates(self.t.day_plan)]
        self.assertNotIn("Fix the tuner", titles)


class TestEpics(LaneCase):
    """Items grouped into epics, to plan and report by."""

    def setUp(self):
        super().setUp()
        self.t.execute("set_epic", {"epic": "Move house", "items": ["Fix the closet door", "Call the bank"],
                                    "lists": ["Garage rebuild"]})

    def test_report(self):
        self.sched.state_db.record_task_completed(self.task("Call the bank").id, DAY)
        out = self.t.execute("epic_report", {"epic": "Move house"})
        self.assertIn("Move house: 1 done, 4 open", out)
        self.assertIn("Next: Fix the closet door, Pour the slab, Frame the walls", out)
        self.assertTrue(self.t.execute("epic_report", {}).startswith("Move house:"))

    def test_time_spent_counts(self):
        door = self.task("Fix the closet door")
        self.t.execute("add_event", {"title": "Fix the closet door", "start": "1:00 PM", "duration": "1 hour",
                                     "task_id": door.id, "category": "task"})
        set_debug_time(at("14:10"))
        self.t.execute("log_outcome", {"event": "Fix the closet door", "status": "done"})
        self.assertRegex(self.t.execute("epic_report", {"epic": "Move house"}), r"; 1h( \d+m)? on it this week")

    def test_plan_it_into_a_period(self):
        out = self.t.execute("plan_epic", {"epic": "Move house", "period": "this week", "count": 2})
        self.assertIn("Call the bank; Fix the closet door", out)  # in their lists' order
        from thirties_core.horizons import HorizonPlans
        self.assertEqual({r["title"] for r in HorizonPlans(self.sched.state_db).rows()},
                         {"Fix the closet door", "Call the bank"})

    def test_remove_and_forget(self):
        out = self.t.execute("set_epic", {"epic": "Move house", "remove": ["Call the bank", "Garage rebuild"]})
        self.assertIn("took out Call the bank; Garage rebuild", out)
        self.t.execute("set_epic", {"epic": "Move house", "delete": True})
        self.assertEqual(self.t.execute("epic_report", {}), "No epics yet.")
        self.untouched()  # nothing is written into the notes

    def test_synced(self):
        from thirties_core.project_facts import ProjectFacts
        records = ProjectFacts(self.sched.state_db).all_records()
        other = make_other_db()
        store = ProjectFacts(other)
        for r in records:
            store.merge_record(r)
        self.assertEqual(store.all("epic"), ProjectFacts(self.sched.state_db).all("epic"))


def make_other_db():
    from tests.fixtures import make_scheduler
    return make_scheduler().state_db


class TestEstimates(LaneCase):
    """The user's own effort estimates, in the unit they choose."""

    def test_in_the_users_words_and_unit(self):
        from thirties_core.efforts import estimate_for
        out = self.t.execute("set_estimate", {"task": "Clean the gutters", "estimate": "about 90 min"})
        self.assertIn("Clean the gutters is about 90 min of work", out)
        self.assertEqual(estimate_for(self.sched.state_db, self.task("Clean the gutters").id)["minutes"], 90)
        self.t.execute("set_effort_unit", {"unit": "points", "length": "45 minutes"})
        self.t.execute("set_estimate", {"task": "Fix the closet door", "estimate": "2"})  # a bare number: points
        rec = estimate_for(self.sched.state_db, self.task("Fix the closet door").id)
        self.assertEqual((rec["unit"], rec["minutes"]), ("points", 90))
        self.t.execute("set_estimate", {"task": "Call the bank", "estimate": "2 thirties"})
        self.assertEqual(estimate_for(self.sched.state_db, self.task("Call the bank").id)["minutes"], 60)

    def test_parsing(self):
        from thirties_core.efforts import parse_estimate
        db = self.sched.state_db
        self.assertEqual(parse_estimate(db, "1 hour 30 minutes")["minutes"], 90)
        self.assertEqual(parse_estimate(db, "half an hour")["minutes"], 30)
        self.assertEqual(parse_estimate(db, "1.5 days")["minutes"], 540)
        with self.assertRaises(ValueError):
            parse_estimate(db, "not long")

    def test_the_day_plans_it_for_that_long(self):
        ListCatalog(self.sched.state_db).set_urgency(self.task("Clean the gutters").id, "now")
        self.services.tasks[:] = self.catalog.apply(self.services.tasks)
        self.t.execute("set_estimate", {"task": "Clean the gutters", "estimate": "90 min"})
        cand = next(c for c in self.t.assistant.proposals.candidates(self.t.day_plan) if c["title"] == "Clean the gutters")
        self.assertEqual(cand["minutes"], 90)

    def test_a_big_one_says_how_much_in_all(self):
        ListCatalog(self.sched.state_db).set_urgency(self.task("Clean the gutters").id, "now")
        self.services.tasks[:] = self.catalog.apply(self.services.tasks)
        self.t.execute("set_estimate", {"task": "Clean the gutters", "estimate": "6 hours"})
        cand = next(c for c in self.t.assistant.proposals.candidates(self.t.day_plan) if c["title"] == "Clean the gutters")
        self.assertIn("about 6h in all", cand["why"])
        self.assertLess(cand["minutes"], 360)

    def test_forget(self):
        from thirties_core.efforts import estimate_for
        self.t.execute("set_estimate", {"task": "Clean the gutters", "estimate": "90 min"})
        self.t.execute("set_estimate", {"task": "Clean the gutters", "estimate": ""})
        self.assertIsNone(estimate_for(self.sched.state_db, self.task("Clean the gutters").id))


if __name__ == "__main__":
    unittest.main()
