"""What didn't happen is said once and plainly, as part of the answer, and only when the answer lacks it.

A refused change used to add "(One part of that didn't go through, so it isn't recorded yet.)" under the reply,
even when the reply already asked about the refused item, and in words that told the user nothing.
"""

import unittest

from tests.test_conversation import ConversationTestCase, reply
from thirties_core.own_up import handled, plain_line

OLD_WORDS = ("didn't go through", "recorded yet")
# the fern may go (named); Bake bread is refused (the user asked to keep it)
ASKED = "repotting the fern can wait till the weekend, but keep the bread baking"


class TestPlainWords(unittest.TestCase):
    def test_the_items_by_name(self):
        self.assertEqual(plain_line([("Bake bread", "remove_event")]), "(Bake bread is still on the plan.)")
        self.assertEqual(plain_line([("Bake bread", "update_event"), ("Repot the fern", "log_outcome")]),
                         "(Bake bread wasn't changed; Repot the fern isn't marked yet.)")
        self.assertEqual(plain_line([("", "update_event")]), "(One of those changes didn't happen.)")

    def test_a_reply_that_asks_about_the_item_or_says_its_still_there_handles_it(self):
        items = [("Bake bread", "remove_event")]
        self.assertTrue(handled("The fern is off. Want me to take Bake bread off too?", items))
        self.assertTrue(handled("Bake bread is still on at 2:00.", items))
        self.assertFalse(handled("All done, both are off.", items))
        self.assertFalse(handled("Bake bread is off the plan.", items))


class OwnUpCase(ConversationTestCase):
    def setUp(self):
        super().setUp()
        for title, start in (("Repot the fern", "1:00 PM"), ("Bake bread", "5:00 PM")):
            self.manager.tools.execute("add_event", {"title": title, "start": start, "duration": "30 min"})

    def calls(self):
        return reply('CALL remove_event {"event": "Repot the fern"}\nCALL remove_event {"event": "Bake bread"}')

    def planned(self, title):
        return [s for s in self.manager.day_plan.spans if s.title == title]

    def test_a_reply_that_asks_about_the_refused_item_gets_nothing_added(self):
        self.engine.responses = [self.calls(), reply("Removed the fern. Should I take Bake bread off as well?")]
        out = self.manager.send_user_message(ASKED)
        self.assertEqual(out, "Removed the fern. Should I take Bake bread off as well?")
        self.assertEqual(len(self.engine.call_history), 2)  # never sent back
        self.assertEqual(len(self.planned("Bake bread")), 1)

    def test_a_reply_that_hides_it_is_sent_back_and_a_true_rewrite_is_kept(self):
        self.engine.responses = [self.calls(), reply("Removed both."),
                                 reply("The fern is off. Bake bread is still on at 5:00; say if it should go.")]
        out = self.manager.send_user_message(ASKED)
        self.assertEqual(out, "The fern is off. Bake bread is still on at 5:00; say if it should go.")

    def test_if_it_still_claims_it_the_claim_says_the_item_in_plain_words(self):
        self.engine.responses = [self.calls(), reply("Removed both."), reply("Removed both, all done.")]
        out = self.manager.send_user_message(ASKED)
        self.assertEqual(out, "Bake bread is still on the plan at 5:00 PM; I couldn't take it off.")  # in place
        for words in OLD_WORDS:
            self.assertNotIn(words, out)

    def test_all_fixed_over_a_refusal_is_caught_too(self):
        self.engine.responses = [self.calls(), reply("All fixed!"), reply("All fixed!")]
        out = self.manager.send_user_message(ASKED)
        self.assertEqual(out, "Bake bread is still on the plan at 5:00 PM; I couldn't take it off.")

    def test_a_change_that_was_already_so_is_no_failure(self):
        self.engine.responses = [
            reply('CALL update_event {"event": "Bake bread", "start": "5:00 PM"}\n'
                  'CALL update_event {"event": "Repot the fern", "start": "2:00 PM"}'),
            reply("Moved the fern to 2:00, and bread stays at 5:00.")]
        out = self.manager.send_user_message("fern at 2, bread at 5")
        self.assertEqual(out, "Moved the fern to 2:00, and bread stays at 5:00.")


class OwnUpWithBookkeeping(OwnUpCase):
    def setUp(self):
        super().setUp()
        self.manager.scheduler.config.assistant.turn_bookkeeping = True


if __name__ == "__main__":
    unittest.main()
