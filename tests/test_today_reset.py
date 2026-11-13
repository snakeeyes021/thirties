"""The morning's Today reset is safe: what's done is filed into Done before it's cleared, what can't be filed
stays, and Today itself changes in one version-checked write. Invented items throughout."""

import unittest
from unittest.mock import patch

from tests.test_today import TodayTestCase, morning_today
from thirties_core.joplin_writeback import WriteLog


class SafeResetTest(TodayTestCase):
    def setUp(self):
        super().setUp()
        morning_today(self)

    def done_note(self):
        return next((n for nid, n in self.api.notes.items() if (n.get("title") or "").startswith("Done ")), None)

    def test_a_line_ticked_by_hand_on_today_is_filed_not_lost(self):
        self.api.notes["n_today"]["body"] += "- [x] Return the drill\n"  # only on Today, ticked in Joplin
        self.t.assistant.start_the_day()
        self.assertNotIn("Return the drill", self.body("n_today"))
        done = self.done_note()
        self.assertIsNotNone(done)
        self.assertIn(": Return the drill (Today To-Do)", done["body"])  # dated, with where it came from

    def test_if_filing_fails_the_line_stays_on_today(self):
        self.api.notes["n_today"]["body"] += "- [x] Return the drill\n"
        with patch.object(type(self.api), "create_note", lambda *a, **k: None):  # no Done note can be made
            self.t.assistant.start_the_day()
        self.assertIn("- [x] Return the drill", self.body("n_today"))  # never cleared unfiled

    def test_today_changes_in_one_write(self):
        self.t.assistant.start_the_day()
        today_writes = [w for w in WriteLog(self.sched.state_db).since()
                        if w["note_title"] == "Today To-Do" and w["op"]["op"] != "file_done"]
        self.assertEqual([w["op"]["op"] for w in today_writes], ["today_reset"])
        self.assertNotIn("Feed the fish", self.body("n_today"))  # a daily step that was done: cleared


if __name__ == "__main__":
    unittest.main()
