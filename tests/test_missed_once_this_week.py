"""When unsure, this week: an item the plan held yesterday and missed once waits for this week, even from a
pick-from list or with a guessed someday. Someday only by the user's own word.

Invented items for someone who throws pots on a late shift and paddles a canoe on days off."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY
from tests.test_today import TITLES, TodayTestCase
from thirties_core.carry import SOMEDAY, WEEK, sort_leftovers
from thirties_core.horizons import HorizonCatalog
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem


class TestMissedOnce(TodayTestCase):

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_menu", "Studio projects")
        self.api.notes["n_menu"] = {"id": "n_menu", "body": "- [ ] Glaze the test tiles\n- [ ] Rebuild the paddle rack\n"
                                                            "- [ ] Carve a canoe paddle\n"}
        ListCatalog(self.sched.state_db).set_role("n_menu", "menu", "Studio projects")
        for tid, title in (("t_tiles", "Glaze the test tiles"), ("t_rack", "Rebuild the paddle rack"),
                           ("t_carve", "Carve a canoe paddle")):
            self.services.tasks.append(TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                                                parent_note_title="Studio projects", parent_note_id="n_menu",
                                                list_role="menu"))
        db = self.sched.state_db
        HorizonCatalog(db).set_item("t_rack", "someday", source="guess")  # a guess, not the user's word
        HorizonCatalog(db).set_item("t_carve", "someday", source="user")  # the user's word
        for i, (tid, title) in enumerate((("t_tiles", "Glaze the test tiles"), ("t_rack", "Rebuild the paddle rack"),
                                          ("t_carve", "Carve a canoe paddle"))):
            self.t.execute("add_event", {"title": title, "start": f"{2 + i}:00 PM", "duration": "30 min",
                                         "day": (DAY - timedelta(days=1)).isoformat(), "task_id": tid,
                                         "category": "task"})
        db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")

    def horizons(self):
        return {i["task_id"]: i["horizon"] for i in sort_leftovers(self.t.assistant, DAY)["pool_items"]}

    def test_a_menu_item_missed_once_waits_for_this_week(self):
        self.assertEqual(self.horizons()["t_tiles"], WEEK)

    def test_a_guessed_someday_isnt_someday(self):
        self.assertEqual(self.horizons()["t_rack"], WEEK)

    def test_the_users_someday_stays_someday(self):
        self.assertEqual(self.horizons()["t_carve"], SOMEDAY)

    def test_the_morning_keeps_them_for_this_week(self):
        self.t.execute("rollover_proposal", {})
        brief = self.t.assistant.leftover_brief()
        tiles = next(line for line in brief.splitlines() if "Glaze the test tiles" in line)
        self.assertNotIn("someday", tiles)


if __name__ == "__main__":
    unittest.main()
