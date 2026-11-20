"""Words that say an item ISN'T done are never a done for it, even beside a complaint about being asked.

"stop asking, it isn't done" right after the app asked about an item, "the paddles aren't finished", "no, not done":
the plan keeps the item open and nothing is ticked in Joplin, however the model names the item. A plain "it's done"
beside the same complaint still ticks.
"""

from tests.test_no_wrong_done import WrongDoneBase
from thirties_core import write_guard
from thirties_core.models import SpanStatus

ASKED = ("", "Are the canoe paddles waxed?")


class TestNotDoneSaidPlainly(WrongDoneBase):
    NOT_DONE = ("enough with the asking, it isn't done",
                "the paddles aren't finished",
                "no, not done",
                "it's not complete, quit checking",
                "they really aren't done")

    def test_not_done_never_ticks(self):
        for message in self.NOT_DONE:
            for how in self.WAYS:
                with self.subTest(message=message, how=how):
                    self.fresh()
                    out = self.say(message, self.way("Wax the canoe paddles", how), prior=ASKED)
                    self.assert_not_ticked("Wax the canoe paddles", out)

    def test_done_beside_the_same_complaint_still_ticks(self):
        out = self.say("enough with the asking, it's done", self.way("Wax the canoe paddles", "id"), prior=ASKED)
        self.assertEqual(self.span("Wax the canoe paddles").status, SpanStatus.DONE, out)

    def test_another_item_not_done_leaves_this_one_free(self):
        out = self.say("the soup bowls aren't finished, but the paddles are waxed",
                       self.way("Wax the canoe paddles", "id"))
        self.assertEqual(self.span("Wax the canoe paddles").status, SpanStatus.DONE, out)

    def test_the_words_alone(self):
        for message in self.NOT_DONE:
            with self.subTest(message=message):
                self.assertFalse(write_guard.says_done(message, "Wax the canoe paddles", []))
