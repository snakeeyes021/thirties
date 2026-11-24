"""A time in a plain sentence belongs to the item whose whole title the sentence names, and a time said as when
something finished or started this turn is what happened, never a claim about where it sits on the plan.

- **A longer name is no claim about the short title.** "Quench the tongs is at 1:10 PM." beside the user's own
  Quench names the tongs, never Quench. "Quench is at 1:10 PM." still claims Quench is at 1:10 PM.
- **A finish or a start reported this turn.** "I marked Quench done at 9:10 PM." after this turn's log_outcome gave
  9:10 PM as its finish, and Python kept it, says when it finished. The same sentence is still checked when Python
  dropped that time, when the call was refused, and when no call gave it.

Through the reply check (no real calls). Fixtures are invented: a smithy day beside a demonstration shown on it.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory

QUENCH, RIVETS, FLUX, TONGS = "Quench", "Anneal the rivets", "Flux the crucible", "Quench the tongs"


class SmithyDay(ConversationTestCase):
    """Three items of the user's own, and a demonstration shown on the day whose name opens with one of them."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("21:20"))
        spans = self.manager.tools.day_plan.spans
        spans.append(EventSpan("own-quench", QUENCH, at("10:40"), at("11:10"), SpanCategory.TASK))
        spans.append(EventSpan("own-rivets", RIVETS, at("11:20"), at("11:50"), SpanCategory.TASK))
        spans.append(EventSpan("own-flux", FLUX, at("14:20"), at("14:50"), SpanCategory.TASK))
        spans.append(EventSpan("cal:tongs", TONGS, at("13:10"), at("13:40"), SpanCategory.CALENDAR, source="calendar",
                               shown_only=True))

    def wrong(self, said, calls=()):
        """The reply check after a turn whose calls are (call, its result) pairs."""
        from thirties_core.brains.frontier import FrontierBrain
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain.messages = [{"role": "user", "content": "how did the day go?"},
                          {"role": "assistant", "content": "", "tool_calls": [c for c, _ in calls]}]
        brain.messages += [{"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": r} for c, r in calls]
        return brain._wrong_times(said, 0)


class TestALongerNameInASentence(SmithyDay):

    def test_a_longer_name_is_no_claim_about_the_short_title(self):
        for said in ("Quench the tongs is at 1:10 PM.", "Quench the tongs starts at 1:10 PM, after the demonstration.",
                     "Today's Quench the tongs is at 1:10 PM."):
            with self.subTest(said=said):
                self.assertEqual(self.wrong(said), [])

    def test_the_whole_title_in_a_sentence_is_still_a_claim(self):
        for said in ("Quench is at 1:10 PM.", "Quench at 1:10 PM.", "Quench starts at 1:10 PM."):
            with self.subTest(said=said):
                self.assertEqual(self.wrong(said), ["Quench is at 10:40 AM"])
        self.assertEqual(self.wrong("Flux the crucible is at 2:50 PM."), [])  # its end: a time it has
        self.assertEqual(self.wrong("Flux the crucible is at 3:40 PM."), ["Flux the crucible is at 2:20 PM"])


class TestAFinishOrStartReported(SmithyDay):

    QUENCHED = {"id": "c1", "name": "log_outcome",
                "arguments": {"event": "own-quench", "status": "done", "finished": "9:10 PM"}}

    def test_a_finish_logged_this_turn_is_no_plan_claim(self):
        kept = "Marked Quench done (8:40 PM – 9:10 PM, estimated)."
        self.assertEqual(self.wrong("I marked Quench done at 9:10 PM.", [(self.QUENCHED, kept)]), [])

    def test_a_start_logged_this_turn_is_no_plan_claim(self):
        call = {"id": "c1", "name": "log_outcome",
                "arguments": {"event": RIVETS, "status": "done", "started": "8:40 PM", "finished": "9:10 PM"}}
        kept = "Marked Anneal the rivets done (8:40 PM – 9:10 PM, reported)."
        self.assertEqual(self.wrong("You started Anneal the rivets at 8:40 PM and finished at 9:10 PM.",
                                    [(call, kept)]), [])

    def test_a_time_python_dropped_is_still_checked(self):
        dropped = "Marked Quench done (10:40 AM – 11:10 AM, planned)."
        self.assertEqual(self.wrong("I marked Quench done at 9:10 PM.", [(self.QUENCHED, dropped)]),
                         ["Quench is at 10:40 AM"])
        refused = "NOT_DONE: The user's words don't point to Quench, so it isn't marked done."
        self.assertEqual(self.wrong("I marked Quench done at 9:10 PM.", [(self.QUENCHED, refused)]),
                         ["Quench is at 10:40 AM"])

    def test_with_no_such_call_the_time_is_still_checked(self):
        self.assertEqual(self.wrong("I marked Quench done at 9:10 PM."), ["Quench is at 10:40 AM"])
        other = {"id": "c1", "name": "log_outcome",
                 "arguments": {"event": "own-flux", "status": "done", "finished": "9:10 PM"}}
        self.assertEqual(self.wrong("I marked Quench done at 9:10 PM.",
                                    [(other, "Marked Flux the crucible done (8:40 PM – 9:10 PM, estimated).")]),
                         ["Quench is at 10:40 AM"])


if __name__ == "__main__":
    unittest.main()
