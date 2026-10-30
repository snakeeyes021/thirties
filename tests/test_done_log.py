"""Done items move out of their lists into the month's Done note, and undo moves them back (ROADMAP "Decided")."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_list_placement import ListsTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.done_log import DONE_NOTEBOOK, DoneLog, ticked_to_file
from thirties_core.joplin_writeback import WriteLog
from thirties_core.lists import ListCatalog


class TestTickedToFile(unittest.TestCase):

    def test_only_finished_items_not_steps_of_open_ones_or_the_users_done_section(self):
        body = ("- [x] Call the bank\n    - [x] Find the number\n- [ ] Taxes\n    - [x] Gather forms\n"
                "# Done\n- [x] Old thing\n# Later\n- [x] Buy stamps\n- [x] Buy stamps\n")
        self.assertEqual(ticked_to_file(body), [("Call the bank", 0), ("Buy stamps", 0), ("Buy stamps", 1)])


class TestFiling(ListsTestCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "# Active\n- [ ] Call the bank\n- [ ] Buy stamps\n- [x] Ticked years ago\n"
        self.log = DoneLog(self.sched.state_db)
        self.sweep()  # Thirties' first look: what's ticked already is history

    def sweep(self, when=None):
        if when is not None:
            set_debug_time(when)
        return self.log.sweep(self.t.assistant)

    def done_note(self):
        return next((n for n in self.api.notes.values() if n.get("title") == "Done 2026-10"), None)

    def test_a_done_item_is_filed_a_while_after_its_tick(self):
        bank = next(t for t in self.services.tasks if t.title == "Call the bank")
        self.t.execute("log_outcome", {"event": bank.id, "status": "done"})
        self.assertIn("- [x] Call the bank", self.body("n_errands"))  # ticked in place first
        self.assertEqual(self.sweep(at("14:10")), [])  # a mis-tap can still be taken back
        self.assertEqual(self.sweep(at("14:45")), ["filed Call the bank from Errands/Admin"])
        self.assertEqual(self.body("n_errands"), "# Active\n- [ ] Buy stamps\n- [x] Ticked years ago\n")
        done = self.done_note()
        self.assertEqual(done["body"], "- [x] 2026-10-15: Call the bank (Errands/Admin)\n")
        folder = self.api.folders[done["parent_id"]]
        self.assertEqual(folder["title"], DONE_NOTEBOOK)
        self.assertEqual(ListCatalog(self.sched.state_db).role(done["id"]), "ignore")  # never read as a list
        writes = [w for w in WriteLog(self.sched.state_db).since("") if w["op"]["op"] == "file_done"]
        self.assertEqual({w["note_id"] for w in writes}, {"n_errands", done["id"]})
        self.assertEqual(self.sweep(at("15:30")), [])  # once

    def test_ticked_by_hand_in_joplin_is_filed_too(self):
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [ ] Buy stamps", "- [x] Buy stamps")
        self.sweep(at("14:05"))
        self.sweep(at("14:40"))
        self.assertNotIn("Buy stamps", self.body("n_errands"))
        self.assertIn("2026-10-15: Buy stamps (Errands/Admin)", self.done_note()["body"])

    def test_unticked_again_before_filing_stays(self):
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [ ] Buy stamps", "- [x] Buy stamps")
        self.sweep(at("14:05"))
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [x] Buy stamps", "- [ ] Buy stamps")
        self.sweep(at("14:40"))
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))
        self.assertIsNone(self.done_note())

    def test_routines_and_today_stay_in_place(self):
        self.api.notes["n_routine"]["body"] = "- [x] Feed kitties\n"
        self.api.notes["n_today"]["body"] = "- [x] Something\n"
        self.sweep(at("14:05"))
        self.sweep(at("15:00"))
        self.assertEqual(self.body("n_routine"), "- [x] Feed kitties\n")
        self.assertEqual(self.body("n_today"), "- [x] Something\n")

    def test_switched_off(self):
        self.sched.config.joplin.file_done = False
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [ ] Buy stamps", "- [x] Buy stamps")
        self.sweep(at("14:05"))
        self.sweep(at("15:00"))
        self.assertIn("- [x] Buy stamps", self.body("n_errands"))

    def test_undo_moves_it_back_to_its_section_open_again(self):
        bank = next(t for t in self.services.tasks if t.title == "Call the bank")
        self.t.execute("add_event", {"title": "Bank", "start": "3:00 PM", "duration": "30 min", "task_id": bank.id})
        span = next(s for s in self.t.day_plan.spans if s.title == "Bank")
        set_debug_time(at("15:20"))
        self.t.execute("log_outcome", {"event": span.id, "status": "done"})
        self.sweep(at("15:25"))
        self.sweep(at("16:00"))
        self.assertNotIn("Call the bank", self.body("n_errands"))
        out = self.t.execute("reopen_item", {"item": span.id})
        self.assertIn("put 'Call the bank' back on Errands/Admin", out)
        self.assertIn("Bank is planned again", out)
        self.assertEqual(self.body("n_errands"), "# Active\n- [ ] Buy stamps\n- [x] Ticked years ago\n- [ ] Call the bank\n")
        self.assertEqual(self.done_note()["body"], "")
        self.assertEqual(self.body("n_today"), "- [ ] Call the bank\n")  # its copy on today's list is open again
        self.assertNotIn(bank.id, self.sched.state_db.completed_since(DAY - timedelta(days=1)))  # offered again
        self.assertEqual(span.status.value, "planned")

    def test_undo_before_filing_unticks(self):
        bank = next(t for t in self.services.tasks if t.title == "Call the bank")
        self.t.execute("log_outcome", {"event": bank.id, "status": "done"})
        out = self.t.execute("reopen_item", {"item": "Call the bank"})
        self.assertIn("unticked 'Call the bank' in Errands/Admin", out)
        self.assertIn("- [ ] Call the bank", self.body("n_errands"))

    def test_unticking_the_done_line_by_hand_puts_it_back(self):
        self.api.notes["n_errands"]["body"] = self.body("n_errands").replace("- [ ] Buy stamps", "- [x] Buy stamps")
        self.sweep(at("14:05"))
        self.sweep(at("14:40"))
        done = self.done_note()
        done["body"] = done["body"].replace("- [x]", "- [ ]")
        self.assertEqual(self.sweep(at("14:45")), ["put Buy stamps back on Errands/Admin"])
        self.assertIn("- [ ] Buy stamps", self.body("n_errands"))
        self.assertEqual(done["body"], "")


if __name__ == "__main__":
    unittest.main()
