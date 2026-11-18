"""A list item moved onto today from another day goes on the Today note (PlanTools._list_today_on).

Moved from a later day, its Today copy was written while the tool still had that later day loaded, so it went on
that day's list and never reached the Today note. On an invented item.
"""

import unittest

from tests.test_today import TodayTestCase
from thirties_core.plan_tools import NOT_DONE


class TestMovedOntoTodayGoesOnTheTodayNote(TodayTestCase):

    def test_a_list_item_moved_from_tomorrow(self):
        from tests.test_today import item as list_item
        from thirties_core.lists import ListCatalog
        self.api.notes["n_errands"]["body"] += "- [ ] Wax the canoe hull\n"
        self.services.tasks.append(list_item("t_hull", "Wax the canoe hull"))
        self.services.tasks[:] = ListCatalog(self.sched.state_db).apply(self.services.tasks)
        out = self.t.execute("add_event", {"title": "Wax the canoe hull", "start": "2:05 PM", "duration": "30 min",
                                           "day": "tomorrow"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("Wax the canoe hull", self.body("n_today"))
        out = self.t.execute("update_event", {"event": "Wax the canoe hull", "day": "tomorrow", "to_day": "today",
                                              "start": "3:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [ ] Wax the canoe hull", self.body("n_today"))
        self.assertIn("also added to today's list", out)



if __name__ == "__main__":
    unittest.main()
