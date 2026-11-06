"""The leftover rules' pieces (carry.py) and how long list lines are said in the morning."""

import unittest
from datetime import date

from tests.fixtures import make_scheduler
from thirties_core.assistant_tools import _short
from thirties_core.carry import OpenPool, done_question

DAY = date(2026, 11, 6)


class TestPool(unittest.TestCase):

    def test_an_item_waits_two_weeks_then_leaves_the_pool(self):
        pool = OpenPool(make_scheduler().state_db)
        pool.add([{"title": "Thank yous", "task_id": "t1"}], date(2026, 11, 2))
        pool.add([{"title": "Thank yous", "task_id": "t1"}], date(2026, 11, 5))  # still open: keeps its first day
        self.assertEqual(pool.since("Thank yous"), date(2026, 11, 2))
        self.assertIn("thank yous", pool.all(date(2026, 11, 16)))
        self.assertNotIn("thank yous", pool.all(date(2026, 11, 17)))  # it's still on its list
        pool.drop(["Thank yous"])
        self.assertIsNone(pool.since("Thank yous"))


class TestQuestion(unittest.TestCase):

    def test_one_short_question(self):
        self.assertEqual(done_question(["Taxes"]), "Did Taxes get done yesterday?")
        self.assertEqual(done_question(["A", "B", "C"]), "Did any of these get done: A, B or C?")
        self.assertEqual(done_question([]), "")


class TestShort(unittest.TestCase):

    def test_a_paragraph_becomes_its_headline(self):
        line = ("**NAS: change the `pm-agent` password** (it leaked into an agent log on 11-04; then give the PM the "
                "new one) · the two fixes below are done")
        self.assertEqual(_short(line), "NAS: change the pm-agent password")
        self.assertEqual(_short("Buy stamps"), "Buy stamps")
        self.assertTrue(len(_short("word " * 40)) <= 81)

    def test_a_lettered_line_keeps_its_words(self):
        self.assertEqual(_short("b. **Hotfix for stable**. It only adds 51 to the list."), "Hotfix for stable.")


class TestWhere(unittest.TestCase):

    def test_only_the_exceptions_to_this_week_are_named(self):
        from thirties_core.carry import where_text
        self.assertEqual(where_text([{"title": "A", "horizon": "week"}, {"title": "B", "horizon": "today"},
                                     {"title": "C", "horizon": "deadline", "due": "2026-11-20"}]),
                         "Whatever wasn't done waits on its list for this week, except B (today), C (by Fri Nov 20).")
        self.assertEqual(where_text([{"title": "A", "horizon": "week"}]),
                         "Whatever wasn't done waits on its list for this week.")
        self.assertEqual(where_text([{"title": "B", "horizon": "someday"}]),
                         "Whatever wasn't done waits on its list: B (someday).")
