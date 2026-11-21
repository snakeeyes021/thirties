"""A write needs a basis in the user's words: the item named or pointed to, a removal ask, a done or a tick ask.

Python checks only that structural fact, and that a note meant for the model never reaches the user; what the words
mean beyond it is the model's reading. Words saying the day is full, a "you pick", a "more" or an "extra" name no
item, so they're no basis on their own. Invented data throughout.
"""

import unittest

from tests.test_model_reads_the_words import WordsCase


class Earlier:
    """Saying something with the user's last messages before it, newest first."""

    def said_after(self, tool, args, words, earlier):
        self.pt.recent_said = list(earlier)
        try:
            return self.said(tool, args, words)
        finally:
            self.pt.recent_said = None


class TestARestoreNeverRemoves(WordsCase):
    """Words asking for an item's earlier place named it, so the model's removal went through."""

    def test_restore_words_keep_it(self):
        for words in ("move the paddle varnish back to 8:40 like I had it", "revert the paddle varnish",
                      "put the paddle varnish back how it was", "undo moving the paddle varnish",
                      "the paddle varnish, the way I had it before"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Varnish the paddle", words))
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)

    def test_an_ask_to_take_it_off_still_takes_it_off(self):
        self.assertWent(self.remove("Varnish the paddle", "take the paddle varnish off, I'm back to the kiln at 8"))


class TestAFullDayNamesNoItem(WordsCase):
    """Words about a full day let any item in that part of the day come off, and matched ordinary talk."""

    def test_full_day_words_alone_take_nothing_off(self):
        for words in ("this evening is way too packed", "today I had a lot of fun", "the evening is not busy",
                      "the varnish is too thick, I'll thin it out", "tonight is overloaded"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Sand the canoe thwart", words))
                self.assertEqual(len(self.planned("Sand the canoe thwart")), 1)

    def test_a_full_day_with_the_item_named_or_a_removal_ask_goes(self):
        self.assertWent(self.remove("Sand the canoe thwart", "tonight is way too packed, the thwart sanding can go"))
        self.assertWent(self.remove("Varnish the paddle", "tonight is way too packed, drop something"))


class TestAKeepHoldsPerClause(WordsCase, Earlier):
    """A keep was missed when its sentence also said "out", or listed more than one item."""

    def test_a_keep_beside_a_removal(self):
        for words in ("keep paddle varnish and take sanding out",
                      "keep the paddle varnish and take the thwart sanding out"):
            with self.subTest(words=words):
                self.assertHeld(self.remove("Varnish the paddle", words))
                self.assertEqual(len(self.planned("Varnish the paddle")), 1)
        self.assertWent(self.remove("Sand the canoe thwart", "keep the paddle varnish and take the thwart sanding out"))

    def test_a_keep_over_a_list(self):
        words = "drop the kiln wash, keep the paddle varnish and the thwart sanding"
        self.assertHeld(self.remove("Sand the canoe thwart", words))
        self.assertHeld(self.remove("Varnish the paddle", words))
        self.assertWent(self.remove("Kiln wash the shelves", words))

    def test_a_keep_said_a_message_ago_still_holds(self):
        earlier = ["drop the kiln wash, keep the paddle varnish"]
        self.assertHeld(self.said_after("remove_event", {"event": "Varnish the paddle"}, "go ahead", earlier))
        self.assertEqual(len(self.planned("Varnish the paddle")), 1)
        self.assertWent(self.said_after("remove_event", {"event": "Kiln wash the shelves"}, "go ahead", earlier))

    def test_this_message_can_take_back_an_earlier_keep(self):
        earlier = ["keep the paddle varnish for now"]
        self.assertWent(self.said_after("remove_event", {"event": "Varnish the paddle"},
                                        "actually drop the paddle varnish", earlier))


if __name__ == "__main__":
    unittest.main()
