"""An item under a line that only says what to do with the lines below it is named by its own words.

A list can hold a line such as "Which skein from crate 4. gets mordanted next?" with items under it. That line says
nothing about what the items are, so an item from under it goes on the plan, on Today's list and into every
message by its own words, never as "<that line>: <item>" (which, shortened, read as the line alone and made two
different items share one name).

Invented: someone who dyes wool and spins it at home."""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.item_path import span_label, tasks_of
from thirties_core.joplin_engine import checklist_tasks
from thirties_core.lists import ListCatalog
from thirties_core.project_pieces import piece_title

LINE = "Which skein from crate 4. gets mordanted next?"
SKEIN, FIBRE = "Mordant the tapestry skein", "Wind the spindle fibre"
BODY = f"# Dyeing\n- [ ] {LINE}\n    - [ ] {SKEIN}\n    - [ ] {FIBRE}\n- [ ] Card the fleece\n    - [ ] Rinse it\n"


def wool_items():
    tasks = checklist_tasks("n_wool", "Wool shelf", "1. Tasks", BODY, [])
    for t in tasks:
        t.list_role = "list"
    return tasks


def find(tasks, title):
    return next(t for t in tasks if t.title == title)


class TestPieceTitle(unittest.TestCase):

    def test_an_item_under_an_instruction_is_its_own_words(self):
        tasks = wool_items()
        self.assertEqual(piece_title(tasks, find(tasks, SKEIN)), SKEIN)
        self.assertEqual(piece_title(tasks, find(tasks, FIBRE)), FIBRE)

    def test_an_item_under_a_line_that_names_something_keeps_it(self):
        tasks = wool_items()
        self.assertEqual(piece_title(tasks, find(tasks, "Rinse it")), "Card the fleece: Rinse it")

    def test_an_instruction_left_only_in_the_items_path_is_dropped_too(self):
        tasks = wool_items()
        skein = find(tasks, SKEIN)
        skein.parent_task_id = None  # its parent line ticked: only the path still names it
        skein.path = [LINE]
        self.assertEqual(piece_title(tasks, skein), SKEIN)


class TestOnThePlanAndInMessages(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:40"))
        TITLES.setdefault("n_wool", "Wool shelf")
        self.api.notes["n_wool"] = {"id": "n_wool", "body": BODY}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_wool", "list", "Wool shelf")
        tasks = catalog.apply(wool_items())
        self.services.tasks.extend(tasks)
        self.skein, self.fibre = find(tasks, SKEIN), find(tasks, FIBRE)

    def propose(self):
        return self.t.execute("propose_day", {"items": [{"item": self.skein.id, "start": "8:10 PM"},
                                                       {"item": self.fibre.id, "start": "9:10 PM"}]})

    def test_the_plan_names_each_by_its_own_words(self):
        self.propose()
        spans = {s.task_id: s for s in self.t.day_plan.spans if s.task_id in (self.skein.id, self.fibre.id)}
        self.assertEqual(spans[self.skein.id].title, SKEIN)
        self.assertEqual(spans[self.fibre.id].title, FIBRE)

    def test_check_ins_and_the_agenda_show_two_names_never_the_line(self):
        self.propose()
        spans = [s for s in self.t.day_plan.spans if s.task_id in (self.skein.id, self.fibre.id)]
        names = [span_label(s, tasks_of(self.t)) for s in spans]
        self.assertEqual(len(set(names)), 2, names)
        for name in names:
            self.assertNotIn("Which skein", name)
            self.assertNotIn("crate 4", name)
        self.assertTrue(any(n.endswith(SKEIN) for n in names), names)
        self.assertTrue(any(n.endswith(FIBRE) for n in names), names)

    def test_the_tool_message_names_each(self):
        out = self.propose()
        self.assertIn(SKEIN, out)
        self.assertIn(FIBRE, out)
        self.assertNotIn("Which skein", out)

    def test_on_todays_list_by_its_own_words(self):
        self.t.execute("add_to_today", {"items": [self.skein.id, self.fibre.id]})
        body = self.body("n_today")
        self.assertIn(f"- [ ] {SKEIN}\n", body)
        self.assertIn(f"- [ ] {FIBRE}\n", body)
        self.assertNotIn("Which skein", body)


if __name__ == "__main__":
    unittest.main()
