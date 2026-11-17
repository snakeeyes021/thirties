"""What a nested item's path leaves out (item_path.py): a routine's steps never show one, even as the copies on
Today's list, and a line that only tells the user what to do ("Pick one ...", a question) is no parent. Invented
notes."""

import unittest

from thirties_core.item_path import label, parents
from thirties_core.joplin_engine import checklist_tasks

ROUTINE = "- [ ] Water the ferns\n- [ ] Wind the clock\n"
TODAY = ("- [ ] Evening round\n    - [ ] Water the ferns\n    - [ ] Wind the clock\n"
         "- [ ] Sort the seed packets\n    - [ ] Label the jars\n")


def routine_and_today():
    routine = checklist_tasks("n_round", "Evening round", "Home", ROUTINE, [])
    for t in routine:
        t.list_role = "routine"
    today = checklist_tasks("n_today", "Today", "Home", TODAY, [])
    for t in today:
        t.list_role = "today"
    return routine, today


def find(tasks, title):
    return next(t for t in tasks if t.title == title)


class TestRoutineStepsNeverShowAPath(unittest.TestCase):

    def setUp(self):
        self.routine, self.today = routine_and_today()
        self.tasks = self.routine + self.today

    def test_a_step_in_the_routine_note_is_just_its_words(self):
        self.assertEqual(label(find(self.routine, "Water the ferns"), self.tasks), "Water the ferns")

    def test_a_step_on_todays_list_under_the_routine_is_just_its_words(self):
        step = find(self.today, "Water the ferns")
        self.assertEqual(parents(step, self.tasks), [])
        self.assertEqual(label(step, self.tasks), "Water the ferns")

    def test_the_same_holds_whichever_items_are_known(self):
        step = find(self.today, "Wind the clock")
        self.assertEqual(label(step, self.today + self.routine), "Wind the clock")

    def test_a_step_of_an_ordinary_task_on_todays_list_still_has_its_parent(self):
        self.assertEqual(label(find(self.today, "Label the jars"), self.tasks),
                         "Sort the seed packets › Label the jars")


MENU = """# Seeds
- [ ] Choose one spare jar from 4.
    - [ ] Marigold
    - [ ] Thyme
- [ ] Which jar do I keep?
    - [ ] The tall one
- [ ] Choose one packet:
    - [ ] Basil
# Beds
- [ ] North bed:
    - [ ] Dig the edge
- [ ] Prepare the bed
    - [ ] Choose one spare jar from 4.
        - [ ] Rake it
"""


class TestInstructionsAndQuestionsAreNoParents(unittest.TestCase):

    def setUp(self):
        self.tasks = checklist_tasks("n_seeds", "Seeds", "Home", MENU, [])
        for t in self.tasks:
            t.list_role = "list"

    def test_a_pick_one_line_is_left_out_of_the_path(self):
        self.assertEqual(label(find(self.tasks, "Marigold"), self.tasks), "Seeds › Marigold")
        self.assertEqual(label(find(self.tasks, "Thyme"), self.tasks), "Seeds › Thyme")

    def test_a_question_is_left_out_of_the_path(self):
        self.assertEqual(label(find(self.tasks, "The tall one"), self.tasks), "Seeds › The tall one")

    def test_an_instruction_written_as_a_group_label_is_left_out_too(self):
        self.assertEqual(label(find(self.tasks, "Basil"), self.tasks), "Seeds › Basil")

    def test_a_label_that_names_something_stays(self):
        self.assertEqual(label(find(self.tasks, "Dig the edge"), self.tasks), "Beds › North bed › Dig the edge")

    def test_a_meaning_bearing_parent_above_the_instruction_stays(self):
        self.assertEqual(label(find(self.tasks, "Rake it"), self.tasks), "Beds › Prepare the bed › Rake it")


if __name__ == "__main__":
    unittest.main()
