"""Whether to ask the user before a change ("Would you like me to…?") is the model's judgement: such a question
reaches the user as the model wrote it, never sent back for a rewrite. A notice passes as it is too.

A mock model plays the assistant; invented items for someone who throws pots on a late shift and paddles a canoe."""

import unittest

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time


class TestNoLeaveAsked(ConversationTestCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:41"))
        self.manager.execute_tool("add_event", {"title": "Wax the canoe hull", "start": "5:00 PM",
                                                "duration": "1 hour"})
        from thirties_core import morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("07:30"))

    def rewrite_asked(self):
        return len(self.engine.call_history) > 1 and "waits on a yes" in self.engine.call_history[1][-1]["content"]

    def test_would_you_like_me_to_reaches_the_user(self):
        self.engine.set_next_response(reply('CALL update_event {"event": "Wax the canoe hull", "start": "6:00 PM"}'))
        self.engine.set_next_response(reply("The hull waxing is at 6 now. Would you like me to move it to tomorrow "
                                            "instead?"))
        out = self.manager.send_user_message("wax the canoe hull at 6, the clay delivery comes at 5")
        self.assertFalse(any("waits on a yes" in str(c[-1]["content"]) for c in self.engine.call_history))
        self.assertEqual(out, "The hull waxing is at 6 now. Would you like me to move it to tomorrow instead?")

    def test_move_these_to_tonight_reaches_the_user(self):
        self.engine.set_next_response(reply("Two things are behind. Move these 2 to tonight?"))
        out = self.manager.send_user_message("long day at the studio")
        self.assertFalse(self.rewrite_asked())
        self.assertEqual(out, "Two things are behind. Move these 2 to tonight?")

    def test_want_the_week_like_that_reaches_the_user(self):
        self.engine.set_next_response(reply("This week: the hull and the paddle rack. Want this week like that?"))
        self.manager.send_user_message("ok, morning")
        self.assertFalse(self.rewrite_asked())

    def test_overflow_may_still_ask(self):
        self.engine.set_next_response(reply("The hull doesn't fit before bed tonight. Should I put it at 9 tomorrow?"))
        self.manager.send_user_message("add an hour of trimming bowls tonight")
        self.assertFalse(self.rewrite_asked())

    def test_a_notice_passes(self):
        self.engine.set_next_response(reply("The hull waxing is at 5."))
        self.assertEqual(self.manager.send_user_message("what's at 5?"), "The hull waxing is at 5.")
        self.assertEqual(len(self.engine.call_history), 1)


if __name__ == "__main__":
    unittest.main()
