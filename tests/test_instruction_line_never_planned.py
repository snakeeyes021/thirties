"""A line that says what to do with the lines under it is never on a plan: only the items under it are.

A list can hold a line such as "Decide between these medallions for the pavers" with items under it. The line
isn't something to do. It's never offered or placed by the day's proposal or a period plan, and if a plan holds it
already, it simply ends when it comes off: it never waits for a later day.

Invented: someone setting medallions into a courtyard of pavers."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests.test_today import TITLES, TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import OpenPool, sort_leftovers
from thirties_core.day_lists import AheadLists
from thirties_core.joplin_engine import checklist_tasks
from thirties_core.list_reading import offerable, says_what_to_do
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE

LINE = "Decide between these medallions for the pavers"
COMPASS, SUNBURST = "Set the compass medallion", "Set the sunburst medallion"
BODY = f"# Courtyard\n- [ ] {LINE}\n    - [ ] {COMPASS}\n    - [ ] {SUNBURST}\n- [ ] Cut the smalti\n"


def paver_items():
    tasks = checklist_tasks("n_pavers", "Paver medallions", "1. Tasks", BODY, [])
    for t in tasks:
        t.list_role = "list"
    return tasks


def find(tasks, title):
    return next(t for t in tasks if t.title == title)


class TestReadingTheLine(unittest.TestCase):

    def test_the_line_says_what_to_do_and_the_items_under_it_are_offered(self):
        tasks = paver_items()
        line = find(tasks, LINE)
        self.assertTrue(says_what_to_do(line))
        self.assertFalse(offerable(line))
        self.assertTrue(offerable(find(tasks, COMPASS)))
        self.assertTrue(offerable(find(tasks, SUNBURST)))
        self.assertFalse(says_what_to_do(find(tasks, COMPASS)))

    def test_the_same_words_with_nothing_under_them_are_an_item(self):
        tasks = checklist_tasks("n_pavers", "Paver medallions", "1. Tasks", f"- [ ] {LINE}\n", [])
        self.assertFalse(says_what_to_do(tasks[0]))
        self.assertTrue(offerable(tasks[0]))


class PaverCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:40"))
        TITLES.setdefault("n_pavers", "Paver medallions")
        self.api.notes["n_pavers"] = {"id": "n_pavers", "body": BODY}
        catalog = ListCatalog(self.sched.state_db)
        catalog.set_role("n_pavers", "list", "Paver medallions")
        tasks = catalog.apply(paver_items())
        self.services.tasks.extend(tasks)
        self.line, self.compass = find(tasks, LINE), find(tasks, COMPASS)
        self.a = self.t.assistant

    def held_on(self, plan, start, minutes=40):
        """The line held by a plan from before this rule."""
        span = EventSpan(f"s_line_{plan.target_date.isoformat()}", LINE, start, start + timedelta(minutes=minutes),
                         SpanCategory.TASK, task_id=self.line.id)
        plan.spans.append(span)
        self.sched.state_db.upsert_span(span)
        return span

    def later_days(self):
        ahead = AheadLists(self.sched.state_db)
        return [r["title"] for d in range(1, 8) for r in ahead.for_day(DAY + timedelta(days=d))]


class TestNeverPlaced(PaverCase):

    def test_the_proposal_refuses_the_line_and_names_the_items_under_it(self):
        out = self.t.execute("propose_day", {"items": [{"item": self.line.id, "start": "3:40 PM"}]})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(COMPASS, out)
        self.assertIn(SUNBURST, out)
        self.assertFalse(any(s.task_id == self.line.id for s in self.t.day_plan.spans))

    def test_the_proposal_takes_an_item_under_it(self):
        out = self.t.execute("propose_day", {"items": [{"item": self.compass.id, "start": "3:40 PM"}]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertTrue(any(s.task_id == self.compass.id for s in self.t.day_plan.spans))

    def test_the_line_by_its_words_is_refused_too(self):
        out = self.t.execute("add_event", {"title": LINE, "start": "3:40 PM", "duration": "40 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertFalse(any(s.title == LINE for s in self.t.day_plan.spans))

    def test_a_period_plan_refuses_it(self):
        out = self.t.execute("pin_to_period", {"task": self.line.id, "when": "this week"})
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_the_days_options_never_list_it(self):
        self.line.due = self.compass.due = DAY.isoformat()
        titles = [c["title"] for c in self.a.proposals.candidates(self.t.day_plan)]
        self.assertNotIn(LINE, titles)
        self.assertIn(COMPASS, titles)


class TestNeverCarried(PaverCase):

    def test_taken_off_todays_plan_it_ends(self):
        span = self.held_on(self.t.day_plan, at("15:40"))
        self.api.notes["n_today"]["body"] = f"- [ ] {LINE}\n"
        self.t.user_message = "take the medallion decision off today's plan"
        try:
            out = self.t.execute("remove_event", {"event": span.id})
        finally:
            self.t.user_message = None
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertNotIn("comes back", out)
        self.assertEqual(self.later_days(), [])
        self.assertNotIn(LINE.lower(), OpenPool(self.sched.state_db).all())

    def test_skipped_it_ends(self):
        span = self.held_on(self.t.day_plan, at("15:40"))
        self.t.user_message = "skip the medallion decision today"
        try:
            self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})
        finally:
            self.t.user_message = None
        self.assertEqual(self.later_days(), [])
        self.assertNotIn(LINE.lower(), OpenPool(self.sched.state_db).all())

    def test_left_unmarked_yesterday_the_carry_skips_it(self):
        self.held_on(self.t.plan_for((DAY - timedelta(days=1)).isoformat()), at("15:40", -1))
        out = sort_leftovers(self.a, DAY)
        self.assertNotIn(LINE, [i["title"] for i in out["pool_items"]])
        self.assertNotIn(LINE, out["ask"])
        self.assertIn(LINE, out["ended"])
        self.assertNotIn(LINE, [p["title"] for p in self.a.rollover_plan()])


if __name__ == "__main__":
    unittest.main()
