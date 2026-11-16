"""Edge cases of how a list note is read (list_reading.py) and what the rest of Thirties does with the reading.
Invented lists: a street party (stalls, a venue, a picnic) and a garden. DAY is Thu Oct 15."""

import json
import unittest
from datetime import date
from types import SimpleNamespace

from tests.test_list_placement import ListsTestCase
from thirties_core import list_reading as lr
from thirties_core.joplin_engine import checklist_tasks


def tasks_of(body, note="n_party", title="Street party"):
    tasks = checklist_tasks(note, title, "Club", body, [])
    for t in tasks:
        t.list_role = "list"
    return tasks


def find(tasks, title):
    return next(t for t in tasks if t.title == title)


PARENTS = """- [ ] Book the coach for the outing
    - [ ] https://coach.example.com/routes
- [ ] Plan the cake stall
    - [ ] Toppings:
- [ ] Choose a hall
    - [ ] Which hall is cheaper?
        - [ ] The scout hut
        - [ ] The parish rooms
- [ ] Ask about a market pitch
    - [ ] Riverside Market - cancelled
- [ ] Restock the first-aid box
    - [ ] Plasters
    - [x] Antiseptic wipes
    - [x] Ice packs - not needed
- [ ] Plan the picnic
    - [ ] Food:
        - [ ] Sandwiches
- [ ] Mend the bike trailer
Paused:
    - [ ] Order a new hitch
"""


class TestWhatMakesAParentAContainer(unittest.TestCase):
    """Only open entries still to do under a line make it a container; a link, an empty label, a question, or a
    dropped, paused or optional line under it leave the parent itself to be offered."""

    def setUp(self):
        self.tasks = tasks_of(PARENTS)

    def test_a_parent_with_nothing_to_do_under_it_is_offered_itself(self):
        for title in ("Book the coach for the outing", "Plan the cake stall", "Choose a hall",
                      "Ask about a market pitch", "Mend the bike trailer"):
            t = find(self.tasks, title)
            self.assertFalse(lr.is_container(t), title)
            self.assertTrue(lr.offerable(t), title)
            self.assertEqual(lr.progress_count(t), "", title)

    def test_open_entries_under_it_still_make_it_a_container(self):
        kit = find(self.tasks, "Restock the first-aid box")
        self.assertTrue(lr.is_container(kit))
        self.assertEqual(lr.progress_count(kit), "1 of 2 done")  # a dropped line isn't counted
        picnic = find(self.tasks, "Plan the picnic")  # its open work sits under a label
        self.assertTrue(lr.is_container(picnic))
        self.assertTrue(lr.offerable(find(self.tasks, "Sandwiches")))


class TestDoneMatchingSkipsWhatIsNotWork(unittest.TestCase):

    def test_a_dropped_line_or_an_option_is_never_matched_as_done(self):
        from thirties_core.done_on_lists import matches
        tasks = tasks_of(PARENTS + "# Venue\n- [x] Hire the marquee\n    - [ ] Return the tent pegs\n")
        assistant = SimpleNamespace(s=SimpleNamespace(tasks=tasks))
        self.assertEqual(matches(assistant, "Riverside Market", "went to riverside market"), [])
        self.assertEqual(matches(assistant, "The scout hut", "booked the scout hut"), [])
        # a line left open under a finished one is still real work the user can finish
        self.assertEqual([t.title for t in matches(assistant, "Returned the tent pegs", "returned the tent pegs")],
                         ["Return the tent pegs"])


class TestNamesThatLookLikeNotes(unittest.TestCase):
    """A " - X" tail is a note only when X reads as one; titles and plain descriptions stay in the name."""

    def test_names_stay_whole(self):
        for line in ("Garden gate - broken spring", "Book club - No Small Matter", "Film night - Skip the Interval",
                     "Lantern parade - Lost Lanterns", "Bake - no-knead loaf"):
            got = lr.read_text(line)
            self.assertEqual((got.name, got.notes), (line, []), line)
            self.assertEqual(lr._state_of(lr.Line(0, line, False, 0, None, "", got), None), "", line)

    def test_real_notes_still_split(self):
        cases = {"Mend the fence - waiting on posts": ("Mend the fence", ["blocker"]),
                 "Mend the fence - no posts yet": ("Mend the fence", ["blocker"]),
                 "Mend the fence - Waiting on Rowan": ("Mend the fence", ["blocker"]),
                 "Mend the fence - broken": ("Mend the fence", ["blocker"]),
                 "Mend the fence - skip": ("Mend the fence", ["dropped"]),
                 "Mend the fence - Cancelled": ("Mend the fence", ["dropped"]),
                 "Mend the fence - nope, too dear": ("Mend the fence", ["dropped"]),
                 "Mend the fence - ready": ("Mend the fence", ["state"])}
        for line, (name, kinds) in cases.items():
            got = lr.read_text(line)
            self.assertEqual((got.name, [n["kind"] for n in got.notes]), (name, kinds), line)

    def test_a_deadline_in_parentheses_is_a_when_not_a_wait(self):
        got = lr.read_text("Sweep the porch (before Friday)")
        self.assertEqual((got.name, got.notes), ("Sweep the porch", [{"kind": "when", "text": "before Friday"}]))
        self.assertEqual(got.blocker, "")
        waits = lr.read_text("Paint the shed (after the rain stops)")
        self.assertEqual(waits.blocker, "after the rain stops")

    def test_an_estimate_before_a_parenthesis_is_still_split(self):
        got = lr.read_text("Prune the roses - 1/6 (after the frost)")
        self.assertEqual(got.name, "Prune the roses")
        self.assertEqual(got.estimate_minutes, 10)
        self.assertEqual(got.blocker, "after the frost")

    def test_a_hyphenated_word_is_not_a_fact(self):
        got = lr.read_text("Sow the seeds (forget-me-not)")
        self.assertEqual((got.name, got.notes), ("Sow the seeds (forget-me-not)", []))


class TestNeverWriteAnIdEither(unittest.TestCase):

    def test_more_id_shapes(self):
        for text in ("cal:4kq7z2m9@2026-10-15T09:00", "id=7c0e5b2a", "task_id=7c0e5b2a9d4f8e13",
                     "task_id: 7c0e5b2a", "Sweep the porch [ID: 7c0e5b2a9d4f8e13]"):
            self.assertTrue(lr.looks_like_id(text), text)
        for text in ("Sweep the porch", "Call 555 0199", "Calendar: set up the rota", "Idea: a lantern walk"):
            self.assertFalse(lr.looks_like_id(text), text)

    def test_a_new_list_from_moved_items_is_checked(self):
        self.assertEqual(lr.refused_text({"op": "move_many", "note_id": "n1", "title": "Odd jobs",
                                          "items": [{"title": "Oil the hinges"}], "new_title": "7c0e5b2a9d4f8e13"}),
                         "7c0e5b2a9d4f8e13")
        self.assertNotIn("split_note", lr.WRITTEN_FIELDS)


GARDEN = """# Beds
- [ ] Weed the herb bed - 45
- [ ] Ask neighbours for cuttings
    - [ ] Hollins family
    - [ ] Ostrowski family
"""


class TestAddingToToday(ListsTestCase):
    notes = {"n_today": ("Today To-Do", "1. Tasks", "", "today"),
             "n_garden": ("Garden", "Home", GARDEN, "list")}

    def test_an_item_named_by_its_clean_name_is_linked_not_copied(self):
        from thirties_core.today import TodayLinks
        self.t.execute("add_to_today", {"items": ["Weed the herb bed"]})
        today = self.body("n_today")
        self.assertEqual(today.count("- [ ]"), 1, today)
        home = next(t for t in self.services.tasks if t.title == "Weed the herb bed - 45")
        line = today.strip().splitlines()[0][len("- [ ] "):]
        link = TodayLinks(self.sched.state_db).home_of(line)
        self.assertIsNotNone(link, today)
        self.assertEqual(link["home_id"], home.id)

    def test_a_nested_item_goes_on_today_with_its_parent(self):
        self.t.execute("add_to_today", {"items": ["Hollins family"]})
        self.assertIn("Ask neighbours for cuttings: Hollins family", self.body("n_today"))


class TestTheLastPieceLeavesTheItemOpen(ListsTestCase):
    notes = {"n_today": ("Today To-Do", "1. Tasks", "", "today"),
             "n_garden": ("Garden", "Home", GARDEN, "list")}

    def test_ticking_every_child_never_ticks_the_parent(self):
        tasks = self.services.tasks
        for title in ("Hollins family", "Ostrowski family"):
            task = find(tasks, title)
            self.t.assistant._complete_backlog_task(task)
        body = self.body("n_garden")
        self.assertIn("    - [x] Hollins family", body)
        self.assertIn("    - [x] Ostrowski family", body)
        self.assertIn("- [ ] Ask neighbours for cuttings", body)


class TestSortingSeesEstimatesAsTimes(unittest.TestCase):
    """A fraction after a line is a part of an hour: the model sorting the list sees it as minutes, and a due date
    read off it ("1/6" as January 6) is never kept."""

    BODY = "- [ ] Prune the roses - 1/6\n- [ ] Order bulbs - by Oct 30\n"

    def test_the_outline_and_the_due_date(self):
        from tests.test_list_sorter import FakeEngine
        from thirties_core.brains.list_sorter import FrontierListSorter
        from thirties_core.models import TaskItem
        items = [TaskItem(id=i, source_notebook="Home", title=t, is_checkbox_item=True, parent_note_title="Garden",
                          parent_note_id="n_garden")
                 for i, t in (("a", "Prune the roses - 1/6"), ("b", "Order bulbs - by Oct 30"))]
        reply = json.dumps({"items": [{"n": 1, "urgency": "soon", "due": "2027-01-06"},
                                      {"n": 2, "urgency": "soon", "due": "2026-10-30"}]})
        eng = FakeEngine([reply])
        out = FrontierListSorter(eng).urgencies({"title": "Garden", "body": self.BODY}, "list", items,
                                                date(2026, 10, 18))
        self.assertNotIn("1/6", eng.asked[0])
        self.assertIn("about 10 min", eng.asked[0])
        self.assertEqual(out, {"a": ("soon", None), "b": ("soon", date(2026, 10, 30))})


if __name__ == "__main__":
    unittest.main()
