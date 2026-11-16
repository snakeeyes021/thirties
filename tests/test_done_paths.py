"""Every way of saying something is done follows the same rules.

- A record after the fact (add_event with happened=true) is a done like any other: a list item it names is ticked
  in Joplin, a session on something that goes on leaves it open, and it needs the user's words to point to it.
- A done whose name only matches an item already settled earlier today (done, skipped, a session) still looks for
  the open list item the user's words name.
- An answer to "which one?" picks the item it points to.
- With several items sharing a word, or copies of one item, a done is for the one whose time has come.

Invented lists, on a fake Joplin.
"""

import unittest

from tests.fixtures import at
from tests.test_list_placement import NOTES, ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import WriteLog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

CRAFT = "- [ ] Sew the quilt binding\n- [ ] Buy quilt batting\n- [ ] Oil the sewing machine\n"
GARDEN = "- [ ] Water the fern by the window\n- [ ] Water the fern in the hall\n- [ ] Buy a new kettle\n"
HOME = "- [ ] Fix the closet door\n- [ ] Repaint the porch railing\n- [ ] Clean the gutters\n"
TIDE = "- [ ] Tide chart app\n"


class DoneCase(ListsTestCase):
    notes = dict(NOTES, n_craft=("Crafts", "1. Tasks", CRAFT, "list"),
                 n_garden=("Garden", "1. Tasks", GARDEN, "list"),
                 n_home=("Home", "1. Tasks", HOME, "list"),
                 n_tide=("Tide Charts", "2. Dev", TIDE, "backlog"))

    def setUp(self):
        super().setUp()
        set_debug_time(at("19:00"))

    def task(self, title):
        return next(t for t in self.services.tasks if t.title == title)

    def call(self, message, tool, args, prior=None):
        self.t.user_message, self.t.prior_exchange = message, prior
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message, self.t.prior_exchange = None, None

    def done(self, message, event, prior=None, **args):
        return self.call(message, "log_outcome", dict({"event": event, "status": "done"}, **args), prior)

    def ticks(self):
        return [w["op"]["title"] for w in WriteLog(self.sched.state_db).since("") if w["op"].get("op") == "complete"]

    def completed(self, task):
        return task.id in self.sched.state_db.completed_since(at("07:00").date())

    def spans(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title]


class TestARecordAfterTheFact(DoneCase):
    """add_event with happened=true goes through the same rules as log_outcome."""

    def test_a_finished_list_item_recorded_after_the_fact_is_ticked_in_joplin(self):
        gutters = self.task("Clean the gutters")
        out = self.call("cleaned the gutters from 3 to 4", "add_event",
                        {"title": "Clean the gutters", "start": "3:00 PM", "end": "4:00 PM", "task_id": gutters.id,
                         "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), ["Clean the gutters"])
        self.assertIn("- [x] Clean the gutters", self.body("n_home"))
        self.assertTrue(self.completed(gutters))

    def test_its_list_wording_without_an_id_is_that_item(self):
        gutters = self.task("Clean the gutters")
        self.call("cleaned the gutters from 3 to 4", "add_event",
                  {"title": "Clean the gutters", "start": "3:00 PM", "end": "4:00 PM", "happened": True})
        self.assertIn("- [x] Clean the gutters", self.body("n_home"))
        self.assertEqual(self.spans("Clean the gutters")[0].task_id, gutters.id)

    def test_a_done_named_loosely_ticks_the_open_list_item(self):
        self.call("I finished the quilt binding, 3 to 4 this afternoon", "add_event",
                  {"title": "Quilt binding", "start": "3:00 PM", "end": "4:00 PM", "happened": True})
        self.assertEqual(self.ticks(), ["Sew the quilt binding"])
        self.assertIn("- [ ] Buy quilt batting", self.body("n_craft"))

    def test_several_open_items_match_ask_which(self):
        out = self.call("watered the fern from 3 to 3:15", "add_event",
                        {"title": "Water the fern", "start": "3:00 PM", "end": "3:15 PM", "happened": True})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Ask which", out)
        self.assertEqual(self.ticks(), [])
        self.assertEqual(self.spans("Water the fern"), [])

    def test_a_work_session_recorded_after_the_fact_stays_open(self):
        railing = self.task("Repaint the porch railing")
        out = self.call("I worked on the porch railing from 3 to 6:30", "add_event",
                        {"title": "Repaint the porch railing", "start": "3:00 PM", "end": "6:30 PM",
                         "task_id": railing.id, "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), [])
        self.assertIn("- [ ] Repaint the porch railing", self.body("n_home"))
        self.assertFalse(self.completed(railing))  # still offered
        span = self.spans("Repaint the porch railing")[0]
        self.assertEqual(span.status, SpanStatus.PARTIAL)  # the time is on the record
        self.assertEqual((span.actual_start, span.actual_end), (at("15:00"), at("18:30")))

    def test_a_session_on_an_ongoing_item_recorded_after_the_fact_keeps_it_offered(self):
        app = self.task("Tide chart app")
        out = self.call("I worked on the tide chart app from 3 to 5", "add_event",
                        {"title": "Tide chart app", "start": "3:00 PM", "end": "5:00 PM", "task_id": app.id,
                         "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertFalse(self.completed(app))  # still offered tomorrow
        self.assertIn("- [ ] Tide chart app", self.body("n_tide"))
        self.assertIn("stays open", out)

    def test_an_item_the_users_words_dont_point_to_is_refused(self):
        door = self.task("Fix the closet door")
        out = self.call("cleaned the gutters from 3 to 4", "add_event",
                        {"title": "Fix the closet door", "start": "3:00 PM", "end": "4:00 PM", "task_id": door.id,
                         "happened": True})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), [])
        self.assertFalse(self.completed(door))
        self.assertEqual(self.spans("Fix the closet door"), [])

    def test_something_on_no_list_is_recorded_as_before(self):
        out = self.call("descaled the kettle from 3 to 3:20", "add_event",
                        {"title": "Descale the kettle", "start": "3:00 PM", "end": "3:20 PM", "happened": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), [])
        self.assertEqual(self.spans("Descale the kettle")[0].status, SpanStatus.DONE)


class TestASettledMatchStillLooksAtTheLists(DoneCase):

    def test_a_skipped_plan_item_with_the_same_words_doesnt_hide_the_list_item(self):
        self.t.execute("add_event", {"title": "Quilt binding", "start": "3:00 PM", "duration": "1 hour"})
        self.t.execute("log_outcome", {"event": "Quilt binding", "status": "skipped"})
        out = self.done("I've finished the quilt binding after all!", "quilt binding")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), ["Sew the quilt binding"])

    def test_a_skipped_plan_item_with_no_list_item_is_corrected_as_before(self):
        self.t.execute("add_event", {"title": "Evening jog", "start": "3:00 PM", "duration": "30 minutes"})
        self.t.execute("log_outcome", {"event": "Evening jog", "status": "skipped"})
        self.done("actually I did go for the jog", "evening jog")
        self.assertEqual([s.status for s in self.spans("Evening jog")], [SpanStatus.DONE])


class TestTheAnswerToWhichOne(DoneCase):

    def test_the_first_one(self):
        out = self.done("watered the fern, that's finished", "Water the fern")
        self.assertIn("Ask which", out)
        asked = ("watered the fern, that's finished",
                 "Which fern did you water: Water the fern by the window, or Water the fern in the hall?")
        window = self.task("Water the fern by the window")
        out = self.done("the first one", window.id, asked)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), ["Water the fern by the window"])

    def test_the_first_one_when_both_are_planned(self):
        for title, start in (("Water the fern by the window", "3:00 PM"), ("Water the fern in the hall", "3:30 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "15 minutes",
                                         "task_id": self.task(title).id})
        asked = ("watered the fern", "Which one: Water the fern by the window, or Water the fern in the hall?")
        out = self.done("the first one", self.spans("Water the fern in the hall")[0].id, asked)
        self.assertTrue(out.startswith(NOT_DONE), out)  # the first one is the window's
        self.assertEqual(self.ticks(), [])
        out = self.done("the first one", self.spans("Water the fern by the window")[0].id, asked)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [x] Water the fern by the window", self.body("n_garden"))
        self.assertIn("- [ ] Water the fern in the hall", self.body("n_garden"))

    def test_the_wrong_one_of_two_unplanned_list_items_is_refused(self):
        asked = ("watered the fern, that's finished",
                 "Which fern did you water: Water the fern by the window, or Water the fern in the hall?")
        out = self.done("the first one", self.task("Water the fern in the hall").id, asked)
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = self.done("yes", self.task("Water the fern in the hall").id, asked)
        self.assertTrue(out.startswith(NOT_DONE), out)  # which one is still unclear
        self.assertEqual(self.ticks(), [])
        out = self.done("the last one", self.task("Water the fern in the hall").id, asked)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.ticks(), ["Water the fern in the hall"])


class TestTheOneWhoseTimeCame(DoneCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("13:20"))

    def test_an_earlier_unmarked_one_doesnt_block_the_current_one(self):
        for title, start in (("Morning stroll", "8:00 AM"), ("Afternoon stroll", "1:00 PM"),
                             ("Evening stroll", "6:00 PM")):
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 minutes"})
        out = self.done("the stroll's done", self.spans("Evening stroll")[0].id)
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = self.done("the stroll's done", self.spans("Morning stroll")[0].id)
        self.assertTrue(out.startswith(NOT_DONE), out)  # the current one is the one meant
        out = self.done("the stroll's done", self.spans("Afternoon stroll")[0].id)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.spans("Morning stroll")[0].status, SpanStatus.PLANNED)

    def test_copies_with_one_title_are_the_one_whose_time_came(self):
        for start in ("8:00 AM", "1:00 PM", "9:00 PM"):
            self.t.execute("add_event", {"title": "Stroll", "start": start, "duration": "30 minutes", "again": True})
        late = next(s for s in self.spans("Stroll") if s.start_dt == at("21:00"))
        out = self.done("stroll done", late.id)
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(late.status, SpanStatus.PLANNED)
        now = next(s for s in self.spans("Stroll") if s.start_dt == at("13:00"))
        out = self.done("stroll done", now.id)
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_a_copy_named_by_its_time(self):
        for start in ("8:00 AM", "1:00 PM", "9:00 PM"):
            self.t.execute("add_event", {"title": "Stroll", "start": start, "duration": "30 minutes", "again": True})
        early = next(s for s in self.spans("Stroll") if s.start_dt == at("08:00"))
        out = self.done("did the 8am stroll", early.id)
        self.assertFalse(out.startswith(NOT_DONE), out)

    def test_before_any_copy_has_come_its_the_next_one(self):
        set_debug_time(at("07:00"))
        for start in ("8:00 AM", "1:00 PM"):
            self.t.execute("add_event", {"title": "Stroll", "start": start, "duration": "30 minutes", "again": True})
        first, second = sorted(self.spans("Stroll"), key=lambda s: s.start_dt)
        self.assertTrue(self.done("stroll done early", second.id).startswith(NOT_DONE))
        self.assertFalse(self.done("stroll done early", first.id).startswith(NOT_DONE))


if __name__ == "__main__":
    unittest.main()
