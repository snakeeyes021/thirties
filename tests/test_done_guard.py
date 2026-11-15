"""A done is for what the user said is done, and only for that.

- Asking why an item still shows as not marked, quoting the app's status ("not marked yet"), and saying "I did it
  at 6:40" is no denial: the done goes through. Saying it hasn't happened still holds it, and being in the
  middle of it is under way, not done (started.py).
- A done for an item the user's words don't point to is refused, and nothing is ticked in Joplin: the model has to
  ask which one they mean (done_reference.py). A tick in the app, and a request for many at once, go through.
"""

import unittest

from thirties_core.started import not_finished, said_not_done

OTHERS = ["Water the tomato seedlings", "Stretch routine", "Fold the towels"]


class TestAQuestionIsNoDenial(unittest.TestCase):

    def said(self, message, title="Water the tomato seedlings"):
        return not_finished(message, title, OTHERS)

    def test_asking_why_it_shows_not_marked_and_saying_it_was_done(self):
        for message in ("why do the seedlings and the stretch say 'not marked yet'?? I did them at 6:40",
                        'why do the seedlings still say "not marked yet"? did those at 6:40 pls fix',
                        "why do the seedlings still say “not marked yet”",
                        "how come the seedlings aren't marked done? I watered them first thing",
                        "the seedlings still show not done, I watered them at 6:40",
                        "the seedlings aren't ticked even though I did them",
                        "is the seedlings one not marked? I did it"):
            self.assertEqual(self.said(message), "", message)
            self.assertFalse(said_not_done(message, "Water the tomato seedlings", OTHERS), message)

    def test_saying_it_hasnt_happened_still_holds(self):
        for message in ("haven't watered the seedlings yet",
                        "didn't get to the seedlings",
                        "the seedlings didn't happen, sorry",
                        "I didn't water the seedlings, why does it say done?",
                        "not the seedlings yet, the towels are done"):
            self.assertEqual(self.said(message), "not", message)

    def test_under_way_is_begun(self):
        self.assertEqual(self.said("I'm doing the seedlings now"), "begun")
        self.assertEqual(self.said("I'm doing the stretch routine now", "Stretch routine"), "begun")

    def test_another_items_denial_is_not_this_ones(self):
        self.assertEqual(self.said("seedlings watered, haven't folded the towels yet"), "")
        self.assertEqual(self.said("seedlings watered, haven't folded the towels yet", "Fold the towels"), "not")



if __name__ == "__main__":
    unittest.main()
