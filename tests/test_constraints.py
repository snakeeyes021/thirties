"""Windows and order the user states are kept wherever an item is placed (constraints.py). Invented items."""

import unittest

from tests.fixtures import DAY, at, build_day, event
from thirties_core.astronomy import set_debug_time
from thirties_core.plan_tools import NOT_DONE, PlanTools


class ConstraintsTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched, plan, events = build_day([event("call", "16:00", "16:30", "Client call")])
        self.t = PlanTools(plan, events, self.sched)
        self.t.execute("remove_event", {"event": "Work"})  # a free day, so the slots are easy to reason about

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def test_prep_said_to_come_before_a_call_is_never_placed_after_it(self):
        """The friction: prep scheduled after the call it was for. Said before the item is planned."""
        out = self.t.execute("set_item_facts", {"event": "Icon concepts", "before": "Client call"})
        self.assertIn("kept when it is", out)
        out = self.t.execute("add_event", {"title": "Icon concepts", "start": "5:00 PM", "duration": "1 hour"})
        icons = self.span("Icon concepts")
        self.assertLessEqual(icons.end_dt, at("16:00"), out)
        self.assertIn("has to be done before Client call", out)

    def test_a_window_is_kept(self):
        self.t.execute("set_item_facts", {"event": "Payroll", "not_before": "10:00 AM", "done_by": "2:30 PM"})
        self.t.execute("add_event", {"title": "Payroll", "start": "6:00 PM", "duration": "1 hour"})
        payroll = self.span("Payroll")
        self.assertTrue(at("10:00") <= payroll.start_dt and payroll.end_dt <= at("14:30"))

    def test_a_move_that_breaks_it_is_kept_too(self):
        self.t.execute("add_event", {"title": "Icon concepts", "start": "1:00 PM", "duration": "1 hour"})
        self.t.execute("set_item_facts", {"event": "Icon concepts", "before": "Client call"})
        self.t.execute("update_event", {"event": "Icon concepts", "start": "5:00 PM"})
        self.assertLessEqual(self.span("Icon concepts").end_dt, at("16:00"))

    def test_a_time_the_user_named_that_breaks_it_is_asked_about_once(self):
        self.t.execute("set_item_facts", {"event": "Icon concepts", "before": "Client call"})
        self.t.user_message = "put the icon concepts at 5pm"
        try:
            out = self.t.execute("add_event", {"title": "Icon concepts", "start": "5:00 PM", "duration": "1 hour"})
        finally:
            self.t.user_message = None
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("Ask the user once", out)

    def test_the_other_items_constraint_holds_too(self):
        """"Wrap-up comes after the review": moving the review past the wrap-up isn't allowed either."""
        self.t.execute("add_event", {"title": "Review", "start": "10:00 AM", "duration": "1 hour"})
        self.t.execute("add_event", {"title": "Wrap-up", "start": "12:00 PM", "duration": "30 min"})
        self.t.execute("set_item_facts", {"event": "Wrap-up", "after": "Review"})
        self.t.execute("update_event", {"event": "Review", "start": "1:00 PM"})
        self.assertLessEqual(self.span("Review").end_dt, self.span("Wrap-up").start_dt)

    def test_a_proposed_day_keeps_them(self):
        from thirties_core.assistant_tools import AssistantTools  # noqa: F401  (the proposal needs the assistant)
        if self.t.assistant is None:
            self.skipTest("no assistant in this fixture")
        self.t.execute("set_item_facts", {"event": "Icon concepts", "before": "Client call"})
        self.t.execute("propose_day", {"items": [{"item": "Icon concepts", "start": "5:00 PM", "duration": "1 hour"}]})
        self.assertLessEqual(self.span("Icon concepts").end_dt, at("16:00"))

    def test_old_databases_get_the_new_columns(self):
        from tests.fixtures import make_scheduler
        from thirties_core.facts import SpanFacts
        db = make_scheduler().state_db
        with db._get_connection() as conn:  # the table as it was before 0.3
            conn.execute("CREATE TABLE span_facts (span_id TEXT PRIMARY KEY, after TEXT NOT NULL DEFAULT '[]', "
                         "not_before TEXT NOT NULL DEFAULT '', daylight INTEGER NOT NULL DEFAULT 0, "
                         "anchored INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO span_facts VALUES ('s1', '[\"s0\"]', '', 0, 1, '2026-11-01T00:00:00+00:00')")
        store = SpanFacts(db)
        old = store.get("s1")
        self.assertEqual((old.after, old.anchored, old.before, old.done_by), (["s0"], True, [], None))


if __name__ == "__main__":
    unittest.main()
