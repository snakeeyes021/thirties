"""Working on something isn't finishing it (work_sessions.py): a session reported with its start and end on an item
that goes on, or on anything the user says they worked on, credits the time and leaves the item open, with nothing
ticked in Joplin, unless the user's words say it's finished. Data guard: a work session on a list item never ticks
Joplin; "finished X" does. Invented lists, on a fake Joplin."""

import unittest

from tests.fixtures import at
from tests.test_list_placement import NOTES, ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import WriteLog
from thirties_core.models import SpanStatus
from thirties_core.work_sessions import reading

BIRDHOUSE = "- [ ] Build the birdhouse\n    - [ ] Cut the side panels\n    - [ ] Paint the little roof\n"
HOME = "- [ ] Fix the closet door\n- [ ] Repaint the porch railing\n- [ ] Clean the gutters\n"


class SessionCase(ListsTestCase):
    notes = dict(NOTES, n_home=("Home", "1. Tasks", HOME, "list"),
                 n_birds=("Birdhouse", "1. Tasks", BIRDHOUSE, "project"))

    def setUp(self):
        super().setUp()
        set_debug_time(at("19:00"))

    def task(self, title):
        return next(t for t in self.services.tasks if t.title == title)

    def say(self, message, event, **args):
        self.t.user_message = message
        try:
            return self.t.execute("log_outcome", dict({"event": event, "status": "done"}, **args))
        finally:
            self.t.user_message = None

    def ticks(self):
        return [w for w in WriteLog(self.sched.state_db).since("") if w["op"].get("op") == "complete"]

    def done(self, task):
        return task.id in self.sched.state_db.completed_since(at("07:00").date())

    def span_of(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)


class TestASessionStaysOpen(SessionCase):

    def test_worked_on_a_list_item_from_three_to_half_six_never_ticks_it(self):
        railing = self.task("Repaint the porch railing")
        out = self.say("I worked on the porch railing from 3 to 6:30", railing.id,
                       started="3:00 PM", finished="6:30 PM")
        self.assertEqual(self.ticks(), [])  # nothing ticked in Joplin
        self.assertIn("- [ ] Repaint the porch railing", self.body("n_home"))
        self.assertFalse(self.done(railing))  # still offered
        span = self.span_of("Repaint the porch railing")
        self.assertEqual(span.status, SpanStatus.PARTIAL)  # the time is credited
        self.assertEqual((span.actual_start, span.actual_end), (at("15:00"), at("18:30")))
        self.assertIn("stays open", out)

    def test_a_planned_project_item_with_its_hours_said_is_a_session(self):
        house = self.task("Build the birdhouse")  # pieces still open: it goes on
        self.t.execute("add_event", {"title": "Build the birdhouse", "start": "3:00 PM", "duration": "2 hours",
                                     "task_id": house.id})
        self.say("the birdhouse from 3 to 6:30 this afternoon", self.span_of("Build the birdhouse").id,
                 started="3:00 PM", finished="6:30 PM")
        self.assertEqual(self.ticks(), [])
        self.assertIn("- [ ] Build the birdhouse", self.body("n_birds"))
        self.assertEqual(self.span_of("Build the birdhouse").status, SpanStatus.PARTIAL)

    def test_the_models_finished_for_good_doesnt_finish_it(self):
        railing = self.task("Repaint the porch railing")
        self.say("spent some time on the porch railing, 3 to 6:30", railing.id, started="3:00 PM",
                 finished="6:30 PM", finished_for_good=True)
        self.assertEqual(self.ticks(), [])
        self.assertFalse(self.done(railing))

    def test_finished_at_a_time_is_when_the_session_stopped(self):
        railing = self.task("Repaint the porch railing")
        self.say("worked on the porch railing from 3 and finished at 6:30", railing.id, started="3:00 PM",
                 finished="6:30 PM")
        self.assertEqual(self.ticks(), [])


class TestFinishedIsDone(SessionCase):

    def test_finished_it_ticks_it(self):
        railing = self.task("Repaint the porch railing")
        self.say("I finished the porch railing, 3 to 6:30", railing.id, started="3:00 PM", finished="6:30 PM")
        self.assertEqual(len(self.ticks()), 1)
        self.assertIn("- [x] Repaint the porch railing", self.body("n_home"))
        self.assertTrue(self.done(railing))

    def test_worked_on_it_and_finished_it(self):
        railing = self.task("Repaint the porch railing")
        self.say("worked on the porch railing from 3 to 6:30 and finished it", railing.id, started="3:00 PM",
                 finished="6:30 PM")
        self.assertIn("- [x] Repaint the porch railing", self.body("n_home"))

    def test_a_one_off_with_its_times_is_done_as_before(self):
        gutters = self.task("Clean the gutters")
        self.say("cleaned the gutters from 3 to 4", gutters.id, started="3:00 PM", finished="4:00 PM")
        self.assertIn("- [x] Clean the gutters", self.body("n_home"))

    def test_a_tick_in_the_app_is_a_tick(self):
        railing = self.task("Repaint the porch railing")
        self.t.execute("log_outcome", {"event": railing.id, "status": "done", "started": "3:00 PM",
                                       "finished": "6:30 PM"})  # nobody talking: the app's own button
        self.assertIn("- [x] Repaint the porch railing", self.body("n_home"))


class TestReadingTheWords(unittest.TestCase):

    def test_each_item_by_its_own_words(self):
        message = "I worked on the shelves from 3 to 6 and finished the laundry"
        self.assertEqual(reading(message, "Build the shelves", ["Laundry"]), "session")
        self.assertEqual(reading(message, "Laundry", ["Build the shelves"]), "finished")

    def test_not_finished_yet_is_no_finish(self):
        self.assertEqual(reading("haven't finished the mural, did some of it", "Paint the mural"), "session")
        self.assertEqual(reading("the mural isn't done yet", "Paint the mural"), None)

    def test_done_with_and_completed(self):
        self.assertEqual(reading("done with the mural!", "Paint the mural"), "finished")
        self.assertEqual(reading("completed the mural today", "Paint the mural"), "finished")


if __name__ == "__main__":
    unittest.main()
