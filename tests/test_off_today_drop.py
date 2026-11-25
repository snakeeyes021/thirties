"""if_there_is_time's drop on a line of today's list: off today and gone, never carried to a later day by itself,
filed as dropped only on the user's drop word; a due date is said, never a hold; a refused write says its real
reason (if_time.py, off_today.leave_list). Invented fixtures: a community garden on a wet afternoon."""

import unittest

from tests.fixtures import DAY
from tests.test_off_today_means_off import OffBase
from thirties_core.carry import OpenPool
from thirties_core.plan_tools import NOT_DONE


class IfThereIsTimeDropTest(OffBase):
    def test_drop_without_a_drop_word_is_off_today_and_not_carried(self):
        self.api.notes["n_today"]["body"] += "- [ ] Rinse the watering cans\n"
        out = self.said("watering cans aren't happening", "if_there_is_time",
                        {"item": "Rinse the watering cans", "action": "drop"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Rinse the watering cans", self.body("n_today"))
        self.assertIsNone(OpenPool(self.sched.state_db).all(DAY).get("rinse the watering cans"))
        self.assert_guarded()

    def test_a_refused_write_says_its_real_reason(self):
        self.api.notes["n_today"]["body"] += "- [ ] Rinse the watering cans\n"
        self.sched.config.joplin.write_back = False
        out = self.said("watering cans another day", "if_there_is_time",
                        {"item": "Rinse the watering cans", "action": "week"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("just now", out)


if __name__ == "__main__":
    unittest.main()
