"""A routine is packed by how long its steps really take the user.

Thirties keeps minute lengths under the hood only to pack items into thirties (AGENTS.md §7, "Natural units"). A
routine's step is as long as the user said, else as long as it really takes them (done steps they timed), else a
few minutes. So steps they finish quickly share a thirty, and ones that run long get one of their own.
"""

import unittest
from datetime import timedelta

from tests.fixtures import at
from tests.test_given_times import RESET_STEPS, GivenCase
from thirties_core.models import EventSpan, SpanCategory, SpanStatus


class TestPackingByHowLongStepsReallyTake(GivenCase):
    """A routine's parts are packed by how long each step really takes the user: steps they finish quickly share a
    thirty, ones that run long get one of their own."""

    def history(self, minutes: int, days: int = 4):
        """Each step of the invented routine done on the past few days, timed by the user, ``minutes`` long."""
        for d in range(1, days + 1):
            t = at("18:00", -d)
            for n, step in enumerate(RESET_STEPS):
                end = t + timedelta(minutes=minutes)
                self.sched.state_db.upsert_span(EventSpan(
                    f"h{d}-{n}", step, t, end, SpanCategory.TASK, status=SpanStatus.DONE,
                    actual_start=t, actual_end=end, time_source="reported"))
                t = end

    def parts(self):
        from thirties_core.routines import natural_parts, step_minutes
        note = {"body": "".join(f"- [ ] {s}\n" for s in RESET_STEPS)}
        history = self.sched.state_db.get_spans_between(at("00:00", -30), at("00:00", 1))
        return natural_parts(note, step_minutes(self.t._lasting(), history))

    def test_with_no_history_the_default_length_applies(self):
        self.assertEqual(len(self.parts()), 2)  # 8 x 5 minutes = 40: two thirties

    def test_quick_steps_share_one_thirty(self):
        self.history(3)
        self.assertEqual(self.parts(), [RESET_STEPS])  # 24 minutes: one thirty

    def test_slow_steps_take_two_thirties(self):
        self.history(7)
        parts = self.parts()
        self.assertEqual(len(parts), 2)  # 56 minutes: two thirties
        self.assertEqual(sum(parts, []), RESET_STEPS)  # in the note's order

    def test_a_stated_length_wins_over_history(self):
        from thirties_core.item_facts import Lasting
        self.history(3)
        self.t._lasting().set(Lasting(name="Charge the radio", title="Charge the radio", minutes=25))
        self.assertEqual(len(self.parts()), 2)  # 21 + 25 minutes

    def test_put_routine_packs_by_the_history(self):
        self.add_reset_routine()
        self.history(3)
        out = self.t.execute("put_routine", {"routine": "Evening Reset", "start": "6:00 PM"})
        self.assertIn("6:00 PM: " + ", ".join(RESET_STEPS), out)  # quick for this user: one thirty
        self.assertNotIn("6:30 PM:", out)

    def test_put_routine_gives_slow_steps_more_thirties(self):
        self.add_reset_routine()
        self.history(7)
        out = self.t.execute("put_routine", {"routine": "Evening Reset", "start": "6:00 PM"})
        self.assertIn("6:30 PM:", out)


if __name__ == "__main__":
    unittest.main()
