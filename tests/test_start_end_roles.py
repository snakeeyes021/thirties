"""Each time a reply gives beside an item is judged by its role: a start against the item's start (or its block), an
end against its end. A right end never excuses a wrong start ("11:45 AM Pottery class (until 12:40 PM)" over a class
at 11:15 AM), and a right start never excuses a wrong end. Invented items: a pottery studio shift and a canoe club."""

import unittest

from tests.fixtures import at, event
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.models import EventSpan, SpanCategory


class RoleCase(ConversationTestCase):

    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(events=[event("cal-pottery", "11:15", "12:40", "Pottery class"),
                                                 event("cal-canoe", "14:05", "15:10", "Canoe club")])
        # the user's own items, shown by their half-hour blocks: one on the blocks, one at odd minutes
        self.manager.tools.day_plan.spans.append(
            EventSpan("own-glaze", "Glaze the mugs", at("09:00"), at("10:00"), SpanCategory.TASK))
        self.manager.tools.day_plan.spans.append(
            EventSpan("own-kiln", "Load the kiln", at("10:05"), at("10:50"), SpanCategory.TASK))

    def wrong(self, said):
        from thirties_core.brains.frontier import FrontierBrain
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        return brain._wrong_times(said)


class TestCalendarEvents(RoleCase):

    def test_a_wrong_start_with_the_right_end_is_caught(self):
        self.assertEqual(self.wrong("11:45 AM Pottery class (until 12:40 PM)"), ["Pottery class is at 11:15 AM"])
        self.assertEqual(self.wrong("11:45 AM – 12:40 PM Pottery class"), ["Pottery class is at 11:15 AM"])
        self.assertEqual(self.wrong("2:30 PM Canoe club (until 3:10 PM)"), ["Canoe club is at 2:05 PM"])
        self.assertEqual(self.wrong("2:30 PM – 3:10 PM Canoe club"), ["Canoe club is at 2:05 PM"])

    def test_the_right_start_with_a_wrong_end_is_caught(self):
        self.assertEqual(self.wrong("11:15 AM Pottery class (until 12:55 PM)"), ["Pottery class is at 11:15 AM"])
        self.assertEqual(self.wrong("11:15 AM – 12:55 PM Pottery class"), ["Pottery class is at 11:15 AM"])
        self.assertEqual(self.wrong("2:05 PM Canoe club (until 3:40 PM)"), ["Canoe club is at 2:05 PM"])

    def test_the_end_said_as_the_start_is_caught(self):
        self.assertTrue(self.wrong("12:40 PM Pottery class (until 12:40 PM)"))

    def test_the_right_start_and_end_pass(self):
        for said in ("11:15 AM Pottery class (until 12:40 PM)", "11:15 AM – 12:40 PM Pottery class",
                     "2:05 PM Canoe club (until 3:10 PM)", "2:05 PM – 3:10 PM Canoe club",
                     "Canoe club starts at 2:05 PM and ends at 3:10 PM."):
            self.assertEqual(self.wrong(said), [], said)

    def test_a_lone_right_time_passes(self):
        for said in ("Pottery class is at 11:15 AM.", "Pottery class runs until 12:40 PM.",
                     "Canoe club wraps up at 3:10 PM."):
            self.assertEqual(self.wrong(said), [], said)

    def test_a_lone_wrong_time_is_still_caught(self):
        self.assertEqual(self.wrong("Pottery class is at 11:45 AM."), ["Pottery class is at 11:15 AM"])

    def test_two_lines_are_judged_each_on_its_own(self):
        said = "Today:\n11:15 AM Pottery class (until 12:40 PM)\n2:30 PM Canoe club (until 3:10 PM)"
        self.assertEqual(self.wrong(said), ["Canoe club is at 2:05 PM"])


class TestTheUsersOwnItemsInBlocks(RoleCase):

    def test_block_times_pass(self):
        for said in ("9:00 AM Glaze the mugs (until 10:00 AM)", "9:00 AM – 10:00 AM Glaze the mugs",
                     # an item at odd minutes is shown by its blocks: 10:00 to 11:00
                     "10:00 AM Load the kiln (until 11:00 AM)", "10:00 AM – 11:00 AM Load the kiln",
                     "10:05 AM Load the kiln (until 10:50 AM)"):
            self.assertEqual(self.wrong(said), [], said)

    def test_a_wrong_start_with_the_right_end_is_caught(self):
        self.assertEqual(self.wrong("9:30 AM Glaze the mugs (until 10:00 AM)"), ["Glaze the mugs is at 9:00 AM"])
        self.assertEqual(self.wrong("9:30 AM – 10:00 AM Glaze the mugs"), ["Glaze the mugs is at 9:00 AM"])
        self.assertEqual(self.wrong("10:30 AM Load the kiln (until 11:00 AM)"), ["Load the kiln is at 10:05 AM"])

    def test_the_right_start_with_a_wrong_end_is_caught(self):
        self.assertEqual(self.wrong("9:00 AM Glaze the mugs (until 10:30 AM)"), ["Glaze the mugs is at 9:00 AM"])
        self.assertEqual(self.wrong("10:00 AM Load the kiln (until 11:30 AM)"), ["Load the kiln is at 10:05 AM"])


class TestTheReplyIsSentBack(RoleCase):

    def test_a_wrong_start_beside_the_right_end_never_reaches_the_user(self):
        self.engine.responses = [reply("Today:\n11:45 AM Pottery class (until 12:40 PM)"),
                                 reply("Today:\n11:15 AM Pottery class (until 12:40 PM)")]
        out = self.manager.send_user_message("what's on today?")
        self.assertIn("Pottery class is at 11:15 AM", self.engine.call_history[1][-1]["content"])
        self.assertEqual(out, "Today:\n11:15 AM Pottery class (until 12:40 PM)")


if __name__ == "__main__":
    unittest.main()
