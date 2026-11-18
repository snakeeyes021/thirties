"""A move nobody asked for that didn't happen is never announced (unasked.py).

Asked to move one item, the model also tried to shift another; the guard kept that one where it was, and the reply
came back saying it "is still in the 6:30 PM block; I couldn't move it" though the user never asked. On invented
items only: someone who glazes pottery and paddles a canoe.
"""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.plan_tools import NOT_DONE


class TestAMoveNobodyAskedFor(ConversationTestCase):
    """Asked to move one item, the model also tried to shift another; the guard kept that one where it was, and the
    reply never mentions it."""

    def setUp(self):
        super().setUp()
        t = self.manager.tools
        for title, start in (("Glaze the test tiles", "6:00 PM"), ("Wax the canoe hull", "6:30 PM")):
            out = t.execute("add_event", {"title": title, "start": start, "duration": "30 min"})
            self.assertFalse(out.startswith(NOT_DONE), out)

    def hull(self):
        return next(s for s in self.manager.day_plan.spans if s.title == "Wax the canoe hull")

    def test_the_guard_tells_the_model_to_leave_it_out(self):
        from thirties_core.plan_log import model_call
        from thirties_core.unasked import LEAVE_OUT
        t = self.manager.tools
        t.user_message = "move the test tiles to 6:30"
        try:
            with model_call():
                out = t.execute("update_event", {"event": "Wax the canoe hull", "start": "7:00 PM"})
        finally:
            t.user_message = None
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(LEAVE_OUT, out)

    def test_a_reply_claiming_it_loses_the_claim_and_gets_no_couldnt_line(self):
        self.engine.responses = [
            reply('CALL update_event {"event": "Wax the canoe hull", "start": "7:00 PM"}'),
            reply('CALL update_event {"event": "Glaze the test tiles", "start": "6:30 PM", "alongside": true}'),
            reply("Moved Glaze the test tiles to 6:30 PM and moved Wax the canoe hull to 7:00 PM."),
            reply("Moved Glaze the test tiles to 6:30 PM and moved Wax the canoe hull to 7:00 PM.")]
        out = self.manager.send_user_message("move the test tiles to 6:30")
        self.assertEqual(self.hull().start_dt, at("18:30"))
        self.assertNotIn("couldn't", out.lower())
        self.assertNotIn("Wax the canoe hull is still", out)
        self.assertIn("Glaze the test tiles", out)

    def test_a_reply_that_leaves_it_out_stands(self):
        said = "Glaze the test tiles is at 6:30 PM now."
        self.engine.responses = [
            reply('CALL update_event {"event": "Wax the canoe hull", "start": "7:00 PM"}'),
            reply('CALL update_event {"event": "Glaze the test tiles", "start": "6:30 PM", "alongside": true}'),
            reply(said)]
        self.assertEqual(self.manager.send_user_message("move the test tiles to 6:30"), said)


class TestTheCouldntLineIsForAskedChanges(unittest.TestCase):

    def tools(self, said_before=""):
        span = MagicMock()
        span.title = "Wax the canoe hull"
        plan = MagicMock()
        plan.spans = [span]
        plan.get_span.return_value = span
        tools = MagicMock()
        tools.day_plan = plan
        tools._title.side_effect = lambda s: s.title
        tools.prior_exchange = (said_before, "") if said_before else None
        return tools

    def messages(self):
        from thirties_core.unasked import refusal_note
        return [{"role": "user", "content": "move the test tiles to 6:30"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"name": "update_event", "arguments": {"event": "Wax the canoe hull", "start": "7:00 PM"}}]},
                {"role": "tool", "name": "update_event",
                 "content": "NOT_DONE: The user's message doesn't name Wax the canoe hull, so it stays where it is."
                            + refusal_note("Wax the canoe hull")}]

    def test_an_unasked_move_isnt_a_failure_to_own_up_to(self):
        from thirties_core.own_up import failed_items
        items, _ = failed_items(self.tools(), self.messages(), 0, (), "NOT_DONE")
        self.assertEqual(items, [])

    def test_one_asked_for_in_the_previous_message_still_is(self):
        from thirties_core.own_up import failed_items
        items, _ = failed_items(self.tools("can the canoe hull go later tonight?"), self.messages(), 0, (),
                                "NOT_DONE")
        self.assertEqual(items, [("Wax the canoe hull", "update_event")])

    def test_a_fact_about_it_says_nothing(self):
        from thirties_core.plain_admit import check_text, from_fact
        from thirties_core.unasked import NOT_ASKED
        self.assertEqual(from_fact(f"Wax the canoe hull is still in the 6:30 PM block {NOT_ASKED}"), "")
        self.assertEqual(from_fact("Wax the canoe hull is still in the 6:30 PM block"),
                         "Wax the canoe hull is still in the 6:30 PM block; I couldn't move it.")
        self.assertNotIn("couldn't", check_text([""]))



if __name__ == "__main__":
    unittest.main()
