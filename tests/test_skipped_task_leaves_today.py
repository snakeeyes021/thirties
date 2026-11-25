"""A list item skipped for today that isn't on today's plan (log_outcome skipped on the task itself) leaves today's
list the same way a planned one does (leave_today.after_task_skip): its Today line goes, its home list is
untouched, nothing is ticked, and it comes back on a later day. Invented fixtures: a pottery studio's errands."""

import unittest

from tests.test_if_time import Base
from thirties_core.carry import OpenPool
from thirties_core.day_lists import pencilled_in
from thirties_core.plan_tools import NOT_DONE

HOME = "- [ ] Call the bank\n- [ ] Buy stamps\n"


class SkippedTaskTest(Base):
    def setUp(self):
        super().setUp()
        self.a = self.t.assistant
        self.api.notes["n_today"]["body"] = "- [ ] Buy stamps\n- [ ] Rake the kiln yard\n"

    def test_a_skipped_list_task_leaves_todays_list(self):
        self.assertIn("Buy stamps", pencilled_in(self.a))
        out = self.say("stamps can wait, skipping them today", "log_outcome",
                       {"event": "Buy stamps", "status": "skipped"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Buy stamps", self.body("n_today"))
        self.assertNotIn("Buy stamps", pencilled_in(self.a))
        self.assertIn("- [ ] Rake the kiln yard", self.body("n_today"))  # the other line is untouched
        self.assertEqual(self.body("n_errands"), HOME)  # its home list keeps it, unticked
        self.assertNotIn("[x]", self.body("n_today"))
        self.assertIsNotNone(OpenPool(self.sched.state_db).all().get("buy stamps"))  # back on a later day

    def test_a_task_still_planned_today_keeps_its_line(self):
        self.say("stamps at 4:40", "add_event", {"title": "Buy stamps", "start": "4:40 PM", "duration": "30 min",
                                                 "task_id": "t_stamps", "because": "stamps at 4:40"})
        self.say("stamps can wait, skipping them today", "log_outcome",
                 {"event": "t_stamps", "status": "skipped"})
        self.assertEqual(self.body("n_errands"), HOME)
        self.assertNotIn("[x]", self.body("n_today"))


if __name__ == "__main__":
    unittest.main()
