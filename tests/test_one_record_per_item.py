"""No item without a list, and no duplicate copies: an item kept from before is one record, tied to its home list,
even when a Today line names it with its parents ("Parent: item" or "Parent › item") and no link.

Invented items for someone who throws pots on a late shift and paddles a canoe on days off."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY
from tests.test_today import TITLES, TodayTestCase
from thirties_core.carry import home_by_label, sort_leftovers
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem


class TestOneRecord(TodayTestCase):

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_notes", "Notes to send")
        self.api.notes["n_notes"] = {"id": "n_notes", "body": "- [ ] Postcards to write\n    - [ ] Paddling league\n"}
        ListCatalog(self.sched.state_db).set_role("n_notes", "list", "Notes to send")
        self.services.tasks += [
            TaskItem(id="t_notes", source_notebook="1. Tasks", title="Postcards to write", is_checkbox_item=True,
                     parent_note_title="Notes to send", parent_note_id="n_notes", list_role="list"),
            TaskItem(id="t_row", source_notebook="1. Tasks", title="Paddling league", is_checkbox_item=True,
                     parent_note_title="Notes to send", parent_note_id="n_notes", parent_task_id="t_notes",
                     list_role="list")]
        # yesterday it was planned from its home item and never marked
        self.t.execute("add_event", {"title": "Paddling league", "start": "2:00 PM", "duration": "30 min",
                                     "day": (DAY - timedelta(days=1)).isoformat(), "task_id": "t_row",
                                     "category": "task"})
        # and an earlier day left a Today line naming it with its parent, with no link
        self.api.notes["n_today"]["body"] = "- [ ] Postcards to write: Paddling league\n"
        self.sched.state_db.set_sync_meta(f"today_added:{DAY.isoformat()}", "[]")

    def test_the_label_finds_its_home(self):
        a = self.t.assistant
        self.assertEqual(home_by_label(a, "Postcards to write: Paddling league").id, "t_row")
        self.assertEqual(home_by_label(a, "Postcards to write › Paddling league").id, "t_row")
        self.assertIsNone(home_by_label(a, "Kiln log: Paddling league"))

    def test_one_record_tied_to_its_home(self):
        out = sort_leftovers(self.t.assistant, DAY)
        rows = [i for i in out["pool_items"] if "paddling league" in i["title"].lower()]
        self.assertEqual(len(rows), 1, out["pool_items"])
        self.assertEqual(rows[0]["task_id"], "t_row")

    def test_the_morning_brief_has_no_listless_copy(self):
        self.t.execute("rollover_proposal", {})
        brief = self.t.assistant.leftover_brief()
        self.assertNotIn("no list", brief)
        self.assertEqual(brief.lower().count("paddling league"), 1, brief)


if __name__ == "__main__":
    unittest.main()
