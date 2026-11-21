"""A write needs a basis in the user's words: the item named or pointed to, a removal ask, a done or a tick ask.

Python checks only that structural fact, and that a note meant for the model never reaches the user; what the words
mean beyond it is the model's reading. Words saying the day is full, a "you pick", a "more" or an "extra" name no
item, so they're no basis on their own. Invented data throughout.
"""

import unittest

from tests.test_words_behind_a_write import AddCase
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


class TestAnAddNeedsTheItemsOwnWords(AddCase):
    """A sentence saying there was something to do let any add through, and reworded copies with it."""

    def test_an_add_named_from_nothing_said_is_refused(self):
        words = "the weather should clear by 3 so push the paddle varnish then"
        for title in ("Sand the hull", "Paddle varnish coat"):
            with self.subTest(title=title):
                self.assertHeld(self.add(title, words))
                self.assertEqual(self.planned(title), [])

    def test_an_add_in_the_users_words_goes(self):
        self.assertWent(self.add("Buy spar varnish and sandpaper", "need a tin of spar varnish and some sandpaper"))
        self.assertWent(self.add("Sweep the shop", "fit something quick in after the thwart", start="10:40 PM"))


class TestASecondSessionIsTheModelsReading(AddCase):
    """Only a listed word ("another", "more", "extra") let a second session on; the item named is the basis."""

    def test_named_with_again_goes(self):
        self.assertWent(self.add("Varnish the paddle", "one coat of the paddle varnish at 8:40 and one at 10:10",
                                 start="10:10 PM", again=True))
        self.assertEqual(len(self.planned("Varnish the paddle")), 2)

    def test_not_named_stays_one(self):
        self.assertHeld(self.add("Varnish the paddle", "more sandpaper would help", start="10:10 PM", again=True))
        self.assertEqual(len(self.planned("Varnish the paddle")), 1)


class TestAWordForWhichKindKeepsTheItemsOwn(WordsCase):
    """A word for which kind pushed out the item's own describing word: the kitchen light ticked the bike light."""

    def test_its_own_describing_word_is_needed(self):
        from thirties_core.done_fit import names_something_else
        self.assertTrue(names_something_else("the kitchen light is fixed", "Fix the bike light"))
        self.assertTrue(names_something_else("the boat varnish is done", "Paddle varnish"))
        self.assertEqual(names_something_else("the old bike light is fixed", "Fix the bike light"), [])
        self.assertEqual(names_something_else("the ash paddle is varnished", "Varnish the paddle"), [])

    def test_the_list_tick_is_refused(self):
        from thirties_core.write_guard import refused
        self.assertNotEqual(refused({"op": "complete", "title": "Fix the bike light"},
                                    "[app] the kitchen light is fixed", None), "")
        self.assertEqual(refused({"op": "complete", "title": "Fix the bike light"},
                                 "[app] the bike light is fixed", None), "")


class TestDownIsANamingWord(WordsCase):
    """"down" was dropped from every reading, so the down jacket ticked the rain jacket."""

    def test_the_down_jacket_names_something_else(self):
        from thirties_core.done_fit import names_something_else
        from thirties_core.write_guard import refused
        self.assertTrue(names_something_else("the down jacket is washed", "Wash the rain jacket"))
        self.assertNotEqual(refused({"op": "complete", "title": "Wash the rain jacket"},
                                    "[app] the down jacket is washed", None), "")


class TestNoNoteReachesTheUser(unittest.TestCase):
    """A note for the model that starts "(The user's message" could be copied into the reply and sent."""

    NOTES = ("(The user's message asks why about Varnish the paddle. item_history has the record for Varnish the "
             "paddle: answer from what it records, in this reply; if it records no reason, say you don't know.)",
             "(The user's message asks for quiet, and Thirties has already set it: quiet until 9:10 PM. No call is "
             "needed for it.)",
             "(The user's message makes today an on-site day: nothing else fits during work. Thirties already moved "
             "what it had placed inside work; pass these lines on as they are, and don't ask about moving anything:"
             "\nNothing Thirties placed was inside work.)",
             "(The user's message leaves the choice to you: pick, make the change, and say in a line what you chose "
             "and why.)")

    def notes(self):
        """These, and the one Python writes for a note to the engineers, word for word."""
        from thirties_core.engineer_notes import hidden_instruction

        class Note:
            text = "the week view flickers"
        return self.NOTES + (hidden_instruction(Note()),)

    def test_a_copied_note_goes(self):
        from thirties_core.internal_text import has_internal, strip_internal
        for note in self.notes():
            with self.subTest(note=note[:40]):
                reply = f"{note} I put the fence staining at 4:10."
                self.assertTrue(has_internal(reply))
                self.assertEqual(strip_internal(reply), "I put the fence staining at 4:10.")
                self.assertEqual(strip_internal(f"Sure thing.\n{note}\nThe fence is at 4:10."),
                                 "Sure thing.\n\nThe fence is at 4:10.")

    def test_the_notes_python_writes_start_that_way(self):
        from thirties_core.internal_text import NOTE_OPENER
        for note in self.notes():
            self.assertTrue(note.startswith(NOTE_OPENER))


class TestNoChoiceNote(WordsCase):
    """A loose "you pick" match told the model to make a change: "I just put the kettle on" got the note."""

    def test_no_note_goes_with_the_message(self):
        from tests.test_conversation import reply
        for words in ("I just put the kettle on", "you pick when the paddle varnish goes"):
            with self.subTest(words=words):
                self.engine.set_next_response(reply("Sounds good."))
                self.manager.send_user_message(words)
                notes = [m for m in self.manager.brain.messages if m.get("hidden")
                         and "leaves the choice" in str(m.get("content") or "")]
                self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
