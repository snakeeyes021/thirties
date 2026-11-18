"""Anything for today gets a time: "add X to the list" said while the day is under way is today's list,
so X goes on today's plan with a block; a later day or a horizon said with it leaves it off today.

Invented items for someone who throws pots on a late shift and paddles a canoe on days off."""

import unittest

from tests.fixtures import at
from tests.test_assistant_behaviour import done_with_morning
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus


class TestTheListIsTodays(RoutineCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("16:10"))
        done_with_morning(self.t)

    def planned(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]

    def test_add_it_to_the_list_puts_it_on_today_with_a_block(self):
        self.t.user_message = "add buy kiln shelf wash to the list"
        out = self.t.execute("add_task", {"title": "Buy kiln shelf wash", "list": "Errands/Admin"})
        self.assertIn("On today's plan at", out)
        spans = self.planned("Buy kiln shelf wash")
        self.assertEqual(len(spans), 1)
        self.assertIn(spans[0].start_dt.minute, (0, 30))  # a block, not an odd minute
        self.assertIn("Buy kiln shelf wash", self.body("n_errands"))  # its home list, still
        self.assertIn("Buy kiln shelf wash", self.body("n_today"))  # and the Today note

    def test_my_to_do_list_too(self):
        self.t.user_message = "put rinse the paddles on my to-do list"
        out = self.t.execute("add_task", {"title": "Rinse the paddles", "list": "Errands/Admin"})
        self.assertIn("On today's plan at", out)

    def test_another_day_or_a_named_list_waits(self):
        self.t.user_message = "add wax the canoe hull to the list for tomorrow"
        out = self.t.execute("add_task", {"title": "Wax the canoe hull", "list": "Errands/Admin"})
        self.assertNotIn("On today's plan", out)
        self.assertEqual(self.planned("Wax the canoe hull"), [])
        self.t.user_message = "add order more clay to my errands"
        out = self.t.execute("add_task", {"title": "Order more clay", "list": "Errands/Admin"})
        self.assertNotIn("On today's plan", out)


if __name__ == "__main__":
    unittest.main()
