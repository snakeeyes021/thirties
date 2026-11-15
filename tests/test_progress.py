"""Progress reports include what was pushed back to the backlog. Invented items throughout."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.assistant_tools import AssistantTools, Services
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import DROPPED, SOMEDAY, WEEK, OpenPool
from thirties_core.models import TaskItem
from thirties_core.plan_tools import PlanTools
from thirties_core.progress import HEADING, pushed_back


class PushedBackTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        tasks = [TaskItem(id="t-shelf", source_notebook="1. Tasks", title="Hang the shelf", parent_note_title="House",
                          parent_note_id="n-house"),
                 TaskItem(id="t-tax", source_notebook="1. Tasks", title="Sort tax receipts", parent_note_title="Admin",
                          parent_note_id="n-admin")]
        self.pt = PlanTools(plan, pending, self.sched)
        self.pt.assistant = AssistantTools(self.pt, Services.for_db(self.sched.state_db, tasks))
        self.pt.services = self.pt.assistant.s
        self.db = self.sched.state_db

    def test_a_list_item_taken_off_the_plan_is_pushed_back(self):
        self.pt.execute("add_event", {"title": "Hang the shelf", "start": "5:00 PM", "duration": "1 hour",
                                      "task_id": "t-shelf"})
        self.pt.execute("add_event", {"title": "Tea with Sam", "start": "6:00 PM", "duration": "1 hour"})
        shelf = next(s for s in self.pt.day_plan.spans if s.title == "Hang the shelf")
        self.assertEqual(shelf.task_id, "t-shelf")
        self.pt.execute("remove_event", {"event": shelf.id})
        tea = next(s for s in self.pt.day_plan.spans if s.title == "Tea with Sam")
        self.pt.execute("remove_event", {"event": tea.id})  # not a list item: nothing to push back to
        self.assertEqual(pushed_back(self.db, DAY), [("Hang the shelf", "taken off the plan, still on its list")])

    def test_a_done_item_removed_isnt_pushed_back(self):
        self.pt.execute("add_event", {"title": "Hang the shelf", "start": "9:00 AM", "duration": "30 minutes",
                                      "task_id": "t-shelf"})
        self.pt.execute("log_outcome", {"event": "Hang the shelf", "status": "done"})
        shelf = next(s for s in self.pt.day_plan.spans if s.title == "Hang the shelf")
        self.pt.execute("remove_event", {"event": shelf.id})
        self.assertEqual(pushed_back(self.db, DAY), [])

    def test_leftovers_kept_for_later_or_dropped(self):
        pool = OpenPool(self.db)
        pool.add([{"title": "Sort tax receipts", "task_id": "t-tax", "horizon": WEEK},
                  {"title": "Learn the ukulele", "horizon": SOMEDAY},
                  {"title": "Water the plants", "horizon": "today"}], DAY)
        pool.set("Learn the ukulele", horizon=DROPPED, dropped_on=DAY.isoformat())
        got = dict(pushed_back(self.db, DAY))
        self.assertEqual(got["Sort tax receipts"], "kept for this week")
        self.assertIn("dropped", got["Learn the ukulele"])
        self.assertNotIn("Water the plants", got)  # kept for today: not pushed back

    def test_the_reports_end_with_it(self):
        OpenPool(self.db).add([{"title": "Sort tax receipts", "task_id": "t-tax", "horizon": WEEK}], DAY)
        out = self.pt.execute("review_day", {})
        self.assertIn(HEADING + "\nSort tax receipts (kept for this week)", out)
        week = self.pt.execute("week_overview", {})
        self.assertIn("Sort tax receipts (kept for this week)", week)
        self.assertTrue(week.startswith("Thu Oct 15"))  # the overview itself is unchanged

    def test_nothing_pushed_back_adds_nothing(self):
        self.assertNotIn(HEADING, self.pt.execute("review_day", {}))


if __name__ == "__main__":
    unittest.main()
