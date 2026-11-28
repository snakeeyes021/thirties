"""A reply that tells what an earlier turn moved is never answered with "still at …; I couldn't move it".

Invented items only: a bike co-op's closing hour, at odd times. The model's reply explains how items got where they are,
naming moves calls in earlier turns made. The move check (FrontierBrain._unmade_moves) reads a move sentence as this
turn's claim; an item an earlier turn's call moved, and that no call this turn touched, is a sentence about then. A
move this turn claims with no call behind it is still caught, and so is one whose call this turn was refused.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.brains.frontier import FrontierBrain
from thirties_core.models import EventSpan, SpanCategory

PADS, SADDLES, TILL, TANDEM, RACK = ("Restock the brake pads", "Wipe the loaner saddles", "Count the till",
                                     "Patch the tandem tube", "Oil the tool rack")


def stuck(lines):
    """The items the check says stayed put (each line names the item, then where it still is)."""
    assert all(" is still at " in line for line in lines), lines
    return [line.split(" is still at ")[0] for line in lines]


class ClosingHour(ConversationTestCase):
    """Five closing jobs between 4:10 and 6:40 PM."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("15:55"))
        spans = self.manager.tools.day_plan.spans
        for key, title, start, end in (("pads", PADS, "16:10", "16:35"), ("saddles", SADDLES, "16:50", "17:10"),
                                       ("till", TILL, "17:30", "17:50"), ("tandem", TANDEM, "17:50", "18:10"),
                                       ("rack", RACK, "18:20", "18:40")):
            spans.append(EventSpan(f"close-{key}", title, at(start), at(end), SpanCategory.TASK))

    @staticmethod
    def call(tool, args, result, cid):
        return [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "name": tool, "arguments": args}]},
                {"role": "tool", "tool_call_id": cid, "name": tool, "content": result}]

    def brain(self, earlier=(), now=()):
        """A brain whose history holds ``earlier`` (calls of a turn before this one) and ``now`` (this turn's)."""
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        brain.messages = [{"role": "user", "content": "slot the saddles job in after the pads"}, *earlier,
                          {"role": "assistant", "content": "Done."}, {"role": "user", "content": "how did that go"}]
        turn_start = len(brain.messages)
        brain.messages += list(now)
        brain._turn_start = turn_start
        brain._times_before = brain._times()
        brain._status_before = brain._statuses()
        return brain, turn_start


class TestAnEarlierTurnsMove(ClosingHour):

    def test_telling_how_an_earlier_move_went_is_no_claim_now(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "4:50 PM"},
                            "Changed Wipe the loaner saddles to 4:50 PM.", "e1")
        brain, start = self.brain(earlier)
        for said in ("Earlier I put Wipe the loaner saddles at 4:50 PM.",
                     "Wipe the loaner saddles was moved to 4:50 PM in your last message, at your ask."):
            with self.subTest(said=said):
                self.assertEqual(brain._unmade_moves(start, said), [])

    def test_looking_an_item_up_this_turn_touches_nothing(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "4:50 PM"},
                            "Changed Wipe the loaner saddles to 4:50 PM.", "e1")
        now = self.call("item_history", {"item": "close-saddles"}, "Wipe the loaner saddles: planned.", "n1")
        brain, start = self.brain(earlier, now)
        self.assertEqual(brain._unmade_moves(start, "Wipe the loaner saddles was moved to 4:50 PM in your last message."), [])

    def test_a_move_claimed_now_with_no_call_is_still_caught(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "4:50 PM"},
                            "Changed Wipe the loaner saddles to 4:50 PM.", "e1")
        brain, start = self.brain(earlier)
        self.assertEqual(stuck(brain._unmade_moves(start, "I moved Count the till to 5:10 PM.")), [TILL])

    def test_an_item_moved_before_whose_call_now_was_refused_is_still_caught(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "4:50 PM"},
                            "Changed Wipe the loaner saddles to 4:50 PM.", "e1")
        now = self.call("update_event", {"event": "close-saddles", "start": "5:20 PM"},
                        "NOT_DONE: that slot is taken.", "n1")
        brain, start = self.brain(earlier, now)
        self.assertEqual(stuck(brain._unmade_moves(start, "I moved Wipe the loaner saddles to 5:20 PM.")), [SADDLES])

    def test_an_earlier_call_that_was_refused_moved_nothing(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "5:20 PM"},
                            "NOT_DONE: that slot is taken.", "e1")
        brain, start = self.brain(earlier)
        self.assertEqual(stuck(brain._unmade_moves(start, "Back then I slid Wipe the loaner saddles over to 5:20 PM.")),
                         [SADDLES])

    def test_a_sentence_about_an_earlier_move_and_a_false_one_now_keeps_only_the_false_one(self):
        earlier = self.call("update_event", {"event": "close-saddles", "start": "4:50 PM"},
                            "Changed Wipe the loaner saddles to 4:50 PM.", "e1")
        brain, start = self.brain(earlier)
        said = "Wipe the loaner saddles got bumped back then, and I've moved Patch the tandem tube up to 4:20 PM now."
        self.assertEqual(stuck(brain._unmade_moves(start, said)), [TANDEM])


if __name__ == "__main__":
    unittest.main()
