"""The model's reading stands, but a write still needs words behind it.

The model reads indirect asks, idioms and paraphrases (test_model_reads_the_words.py). These are the cases where the
words give no basis at all for the change, or say something else: an add nobody's words touch, a give-up or a
grumble read as a finish, a keep read as a removal, a busy van read as a busy day, a question read as a done. Each
stays refused, and the readings around it still go through. Invented data throughout.
"""

import unittest

from tests.fixtures import at
from tests.test_leave_today import LeaveBase
from tests.test_model_reads_the_words import WordsCase
from thirties_core.astronomy import set_debug_time


class AddCase(WordsCase):
    def add(self, title, words, start="9:10 PM", **more):
        from thirties_core.plan_log import model_call
        with model_call():
            return self.said("add_event", {"title": title, "start": start, "duration": "30 min", **more}, words)


class TestAGiveUpOrAGrumbleIsNoDone(WordsCase):
    """Phrases of giving up, grumbling or place read as a finish, and ticked the item."""

    NOT_DONE = ("I've given up on the kiln permit", "I gave up on the kiln permit",
                "the kiln permit has bugged me all week", "I've dreaded the kiln permit", "I've skipped the kiln permit",
                "dealt with the kiln permit badly", "the kiln permit folder is out of the way of the door",
                "the fumes knocked me out, kiln permit later", "the kiln permit is in, or so I thought",
                "the kiln permit is in the mail")

    def test_the_list_tick_is_refused(self):
        from thirties_core.write_guard import refused
        op = {"op": "complete", "title": "Mail the kiln permit"}
        for words in self.NOT_DONE + ("the kiln door stuck again",):
            with self.subTest(words=words):
                self.assertNotEqual(refused(op, f"[app] {words}", None), "")

    def test_says_done_reads_none_of_them_as_done(self):
        from thirties_core.write_guard import says_done
        for words in self.NOT_DONE:
            with self.subTest(words=words):
                self.assertFalse(says_done(words, "Mail the kiln permit"))
        self.assertFalse(says_done("the kiln door stuck again", "Fix the kiln door"))

    def test_a_report_must_name_it_or_point_back(self):
        from thirties_core.write_guard import says_done
        self.assertFalse(says_done("the kiln permit is due friday, I've written to the clay supplier",
                                   "Mail the kiln permit"))
        self.assertTrue(says_done("the kiln permit, I've sent it", "Mail the kiln permit"))
        self.assertTrue(says_done("I've mailed it", "Mail the kiln permit"))

    def test_a_report_ahead_of_time_ticks_nothing(self):
        from thirties_core.write_guard import says_done
        for words in ("once I've written the glaze notes I'll rest", "when I've written up the glaze notes"):
            with self.subTest(words=words):
                self.assertFalse(says_done(words, "Write the glaze notes"))
        self.assertTrue(says_done("I've written the glaze notes", "Write the glaze notes"))

    def test_the_turn_tick_is_refused(self):
        for words in ("I've given up on the paddle varnish", "the paddle varnish has bugged me all week",
                      "I've skipped the paddle varnish", "dealt with the paddle varnish badly",
                      "the paddle varnish is in, or so I thought"):
            with self.subTest(words=words):
                self.assertHeld(self.tick("Varnish the paddle", words))


if __name__ == "__main__":
    unittest.main()
