"""A done for something not on today's plan ticks the matching open item on the user's lists (done_on_lists.py),
never a new unplanned item beside it; when several items match, the model is told to ask which. Data guard: only
the one line the user's words point to is ticked. Invented lists, on a fake Joplin."""

from tests.fixtures import at
from tests.test_list_placement import NOTES, ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import WriteLog
from thirties_core.plan_tools import NOT_DONE

CRAFT = "- [ ] Sew the quilt binding\n- [ ] Buy quilt batting\n- [ ] Oil the sewing machine\n"
GARDEN = "- [ ] Water the fern by the window\n- [ ] Water the fern in the hall\n- [ ] Buy a new kettle\n"


class DoneOnListsCase(ListsTestCase):
    notes = dict(NOTES, n_craft=("Crafts", "1. Tasks", CRAFT, "list"),
                 n_garden=("Garden", "1. Tasks", GARDEN, "list"))

    def setUp(self):
        super().setUp()
        set_debug_time(at("19:00"))

    def say(self, message, event, **args):
        self.t.user_message = message
        try:
            return self.t.execute("log_outcome", dict({"event": event, "status": "done"}, **args))
        finally:
            self.t.user_message = None

    def ticks(self):
        return [w["op"]["title"] for w in WriteLog(self.sched.state_db).since("") if w["op"].get("op") == "complete"]

    def unplanned(self):
        return [s for s in self.t.day_plan.spans if s.status.value == "done" and not s.task_id]


class TestTheListItemIsTicked(DoneOnListsCase):

    def test_one_match_ticks_that_line_not_a_new_item(self):
        out = self.say("I've finished the quilt binding!", "quilt binding", new=True)  # the model said new
        self.assertEqual(self.ticks(), ["Sew the quilt binding"])
        self.assertIn("- [x] Sew the quilt binding", self.body("n_craft"))
        self.assertIn("- [ ] Buy quilt batting", self.body("n_craft"))  # only the one it is
        self.assertEqual(self.unplanned(), [])
        self.assertIn("Sew the quilt binding", out)

    def test_several_matches_ask_which(self):
        out = self.say("watered the fern, that's finished", "Water the fern")
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Water the fern by the window", out)
        self.assertIn("Water the fern in the hall", out)
        self.assertIn("Ask which", out)
        self.assertEqual(self.ticks(), [])
        self.assertEqual(self.unplanned(), [])

    def test_its_distinctive_words_pick_one_of_several(self):
        self.say("watered the fern by the window", "Water the fern")
        self.assertEqual(self.ticks(), ["Water the fern by the window"])

    def test_nothing_like_it_on_a_list_is_new_as_before(self):
        out = self.say("I descaled the kettle", "Descale the kettle", new=True)  # not Buy a new kettle
        self.assertIn("wasn't planned", out)
        self.assertEqual(self.ticks(), [])
        self.assertEqual([s.title for s in self.unplanned()], ["Descale the kettle"])

    def test_a_planned_copy_of_it_is_marked_too(self):
        machine = next(t for t in self.services.tasks if t.title == "Oil the sewing machine")
        self.t.execute("add_event", {"title": "Machine care", "start": "8:00 PM", "duration": "30 min",
                                     "task_id": machine.id})
        set_debug_time(at("20:40"))
        self.say("oiled the sewing machine", "sewing machine oil")
        self.assertIn("- [x] Oil the sewing machine", self.body("n_craft"))
        self.assertEqual(next(s for s in self.t.day_plan.spans if s.title == "Machine care").status.value, "done")

    def test_the_apps_own_buttons_never_guess(self):
        out = self.t.execute("log_outcome", {"event": "quilt binding", "status": "done", "new": True})
        self.assertEqual(self.ticks(), [])  # nobody talking: nothing is matched
        self.assertIn("wasn't planned", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
