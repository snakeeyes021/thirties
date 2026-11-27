"""After repeated deferrals, Thirties offers to break the item down into smaller pieces, in code; once it's broken
down, its first piece is offered like any project piece. Invented items."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at
from tests import test_backoff
from thirties_core.astronomy import set_debug_time
from thirties_core.break_down import can_break, morning_question, prompt_section
from thirties_core.lists import ListCatalog
from thirties_core.plan_tools import NOT_DONE

OFFER = "Still want Re-pot the fern? Keep for this week / Park for later / Drop / Break it into smaller pieces"


class BreakDownCase(test_backoff.BackoffDays):
    """BackoffDays' set-up and helpers; its own tests run in test_backoff, not again here."""

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] += "- [ ] Re-pot the fern\n"

    def fern(self):
        return next(t for t in self.services.tasks if t.id == "t_fern")


for _name in [n for n in dir(test_backoff.BackoffDays) if n.startswith("test_")]:
    setattr(BreakDownCase, _name, None)


class TestAfterThreeMisses(BreakDownCase):

    def test_the_question_offers_to_break_it_down(self):
        self.three_misses()
        said = self.t.execute("rollover_proposal", {})
        self.assertEqual(said.count(OFFER), 1, said)

    def test_choosing_it_holds_it_until_it_has_pieces_then_offers_the_first(self):
        self.three_misses()
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "break"})
        self.assertIn("to be broken down", out)
        self.assertIn("break_down_task", out)  # the model is told what to do next
        self.assertIn("Re-pot the fern", prompt_section(self.t.assistant))
        self.assertNotIn("Re-pot the fern", " ".join(self.offered(5)))  # held, however long
        self.t.user_message = "yes, those steps"
        out = self.t.execute("break_down_task", {"task": "Re-pot the fern",
                                                 "steps": ["Buy potting soil", "Move the fern to the new pot"]})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("- [ ] Re-pot the fern\n    - [ ] Buy potting soil\n    - [ ] Move the fern to the new pot",
                      self.api.notes["n_errands"]["body"])
        self.assertEqual(prompt_section(self.t.assistant), "")  # broken down: released
        self.assertIsNone(self.streaks.held_until(DAY + timedelta(days=1), "Re-pot the fern", "t_fern"))
        self.assertIn("Re-pot the fern: Buy potting soil", self.offered(1))  # its first piece, as a piece

    def test_not_offered_for_what_cant_be_broken_down(self):
        menu = next(t for t in self.services.tasks if t.id == "t_bank")
        menu.list_role = "menu"
        self.assertFalse(can_break(self.t.assistant, menu))
        self.assertTrue(can_break(self.t.assistant, self.fern()))
        self.t.execute("break_down_task", {"task": "Re-pot the fern", "steps": ["Buy soil", "Re-pot it"]})
        self.assertFalse(can_break(self.t.assistant, self.fern()))  # it has pieces already
        line = morning_question(self.t.assistant, [{"title": "Re-pot the fern", "task_id": "t_fern"}])
        self.assertNotIn("Break it into smaller pieces", line)

    def test_park_keep_and_drop_still_work(self):
        self.three_misses()
        self.t.execute("rollover_proposal", {})
        self.assertIn("kept for this week", self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "keep"}))


class TestAfterRepeatedPuttingOff(BreakDownCase):

    def put_off(self, n=3):
        for _ in range(n):
            self.sched.state_db.record_task_deferred("t_fern")
        self.sched.attach_history_to_tasks(self.services.tasks)

    def test_put_off_three_times_it_is_offered_once(self):
        self.put_off(2)
        self.assertNotIn("Re-pot the fern", self.t.execute("rollover_proposal", {}))
        self.put_off(1)
        said = self.t.execute("rollover_proposal", {})
        self.assertIn("Re-pot the fern has been put off 3 times. Break it into smaller pieces? Break it down / Keep "
                      "as it is", said)
        set_debug_time(at("09:00", 1))
        self.assertNotIn("put off", self.t.execute("rollover_proposal", {}))  # asked once

    def test_the_question_uses_the_true_count_not_an_old_inflated_one(self):
        import sqlite3
        self.sched.state_db.record_task_allocation("t_fern", DAY)
        with sqlite3.connect(self.sched.state_db.db_path) as conn:  # a counter the old code had pushed up
            conn.execute("UPDATE task_history SET deferred_count = 6 WHERE task_id = 't_fern'")
        self.sched.attach_history_to_tasks(self.services.tasks)
        self.assertNotIn("put off", self.t.execute("rollover_proposal", {}))
        self.put_off(3)
        self.assertIn("Re-pot the fern has been put off 3 times.", self.t.execute("rollover_proposal", {}))

    def test_keep_as_it_is(self):
        self.put_off()
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "keep as it is"})
        self.assertEqual(out, "Re-pot the fern: kept as it is.")
        self.assertIn("Re-pot the fern", self.offered(0))

    def test_break_it_down(self):
        self.put_off()
        self.t.execute("rollover_proposal", {})
        self.t.execute("still_want", {"item": "Re-pot the fern", "choice": "break it down"})
        self.assertNotIn("Re-pot the fern", " ".join(self.offered(0)))
        self.t.execute("break_down_task", {"task": "Re-pot the fern", "steps": ["Buy potting soil", "Re-pot it"]})
        self.assertIn("Re-pot the fern: Buy potting soil", self.offered(0))

    def test_never_for_a_routine_step(self):
        ListCatalog(self.sched.state_db).set_role("n_errands", "routine", "Errands/Admin")
        self.services.tasks[:] = ListCatalog(self.sched.state_db).apply(self.services.tasks)
        self.put_off()
        self.assertNotIn("put off", self.t.execute("rollover_proposal", {}))


if __name__ == "__main__":
    unittest.main()
