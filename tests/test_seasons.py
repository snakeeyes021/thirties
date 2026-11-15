"""Seasons (seasons.py): an item planned only in its months, said by the user or marked in its text; named seasons
follow the hemisphere. Invented items throughout."""

import unittest
from datetime import date, timedelta

from tests.fixtures import DAY
from tests.test_horizons import PlannerCase, item
from thirties_core import seasons as S
from thirties_core.horizons import parse_period


class TestReadingSeasons(unittest.TestCase):
    def test_named_seasons_follow_the_hemisphere(self):
        self.assertEqual(S.parse("only in summer", 38.9), {6, 7, 8})
        self.assertEqual(S.parse("spring", 38.9), {3, 4, 5})
        self.assertEqual(S.parse("a winter thing", -33.9), {6, 7, 8})  # south of the equator
        self.assertEqual(S.parse("May to September"), {5, 6, 7, 8, 9})
        self.assertEqual(S.parse("November to February"), {11, 12, 1, 2})  # round the year end
        self.assertEqual(S.parse("all year"), set())
        self.assertIsNone(S.parse("whenever it feels right"))  # names no season: never guessed

    def test_a_mark_in_the_items_text(self):
        self.assertEqual(S.marked("Hike the ridge trail (summer)", 38.9), {6, 7, 8})
        self.assertEqual(S.marked("Rake the leaves (Oct–Nov)", 38.9), {10, 11})
        self.assertEqual(S.marked("Summer reading list", 38.9), set())  # the wording alone never counts

    def test_a_deadline_out_of_season_is_pulled_into_it(self):
        self.assertEqual(S.pulled_due({6, 7, 8}, date(2027, 10, 15)), date(2027, 8, 31))
        self.assertEqual(S.pulled_due({6, 7, 8}, date(2027, 7, 4)), date(2027, 7, 4))


class TestPlanningInSeason(PlannerCase):
    """DAY is a Thursday in mid October."""

    def test_out_of_season_it_waits_for_its_months(self):
        out = self.t.execute("set_item_facts", {"event": "Paint the hall", "season": "summer"})
        self.assertIn("in season June to August", out)
        week = parse_period("this week", DAY)
        self.assertNotIn("paint", self.ids(self.hp.candidates(week, limit=None)))  # not now, not someday
        year = parse_period("2027", DAY)
        self.assertIn("paint", self.ids(self.hp.candidates(year, limit=None)))  # next year's window
        june = parse_period("June 2027", DAY)
        self.assertIn("paint", self.ids(self.hp.candidates(june, limit=None)))

    def test_an_item_marked_in_season_is_offered(self):
        self.tasks += [item("rake", "Rake the leaves (Oct–Nov)"), item("pool", "Open the pool (summer)")]
        self.build()
        week = parse_period("this week", DAY)
        for t in self.tasks[-2:]:
            self.hp.pin(t, week)
        ids = self.ids(self.hp.candidates(week, limit=None))
        self.assertIn("rake", ids)  # in season (October)
        self.assertNotIn("pool", ids)  # marked for summer: it waits

    def test_bad_season_words_are_refused(self):
        out = self.t.execute("set_item_facts", {"event": "Paint the hall", "season": "whenever"})
        self.assertTrue(out.startswith("NOT_DONE"), out)


if __name__ == "__main__":
    unittest.main()
