""""The routine", said when only one routine is on the plan, names that routine for a move's scope."""

from tests.test_routine_halves_together import LATER, PlacedCase
from thirties_core.move_scope import _ROUTINE_WORD_RE
from thirties_core.plan_tools import NOT_DONE


class TestTheRoutineWord(PlacedCase):
    def test_the_word_is_matched(self):
        for message in ("the routine is off", "both routines", "Routine's in the wrong place"):
            with self.subTest(message=message):
                self.assertIsNotNone(_ROUTINE_WORD_RE.search(message))
        self.assertIsNone(_ROUTINE_WORD_RE.search("routinely late"))

    def test_the_routine_alone_covers_its_steps(self):
        message = "that routine landed in a silly place, put it back together"
        for step in LATER:
            out = self.said(message, "update_event", {"event": step, "start": "8:40 PM"})
            self.assertFalse(out.startswith(NOT_DONE), out)
