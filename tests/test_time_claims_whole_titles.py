"""A time in a reply belongs to the item whose whole title stands as the entry, and a correction never costs the
reply its true list lines.

- **A short title inside a longer one is no claim.** "1:10 PM Varnish the kite spars" is a line about the kite spars,
  never a claim that the item Varnish is at 1:10 PM. A list line with a time in front names the item whose whole
  title is its entry; "1:10 PM Varnish" still claims Varnish is at 1:10 PM.
- **A correction leaves the list whole.** When a list line's time is wrong and the rewrite is still wrong, that line
  says the plan's time in its place and every true line stays, under its lead-in.

Through the reply check and whole turns with a scripted model (no real calls). Fixtures are invented: a workshop
morning beside someone else's kite meet.
"""

import re
import unittest

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory

VARNISH, CHISELS, BELLOWS, SPARS = "Varnish", "Sharpen the chisels", "Patch the bellows", "Varnish the kite spars"
DAY_LIST = ("Here's the rest of your day:\n10:40 AM Varnish\n11:20 AM Sharpen the chisels\n1:10 PM Varnish the kite "
            "spars\n2:20 PM Patch the bellows")


def entries(text):
    """Each line of the text without the time in front of it (the reply shows the user's own items by block)."""
    return [re.sub(r"^\d{1,2}:\d{2} [AP]M ", "", line) for line in text.splitlines()]


class WorkshopDay(ConversationTestCase):
    """Three items of the user's own at odd minutes, and someone else's event shown on the day."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("09:10"))
        from thirties_core import morning  # the day's first message was earlier: this turn isn't the morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("08:10"), "in_conversation")
        spans = self.manager.tools.day_plan.spans
        spans.append(EventSpan("own-varnish", VARNISH, at("10:40"), at("11:10"), SpanCategory.TASK))
        spans.append(EventSpan("own-chisels", CHISELS, at("11:20"), at("11:50"), SpanCategory.TASK))
        spans.append(EventSpan("own-bellows", BELLOWS, at("14:20"), at("14:50"), SpanCategory.TASK))
        spans.append(EventSpan("cal:spars", SPARS, at("13:10"), at("13:50"), SpanCategory.CALENDAR, source="calendar",
                               shown_only=True))

    def wrong(self, said):
        from thirties_core.brains.frontier import FrontierBrain
        brain = FrontierBrain.__new__(FrontierBrain)
        brain.tools = self.manager.tools
        return brain._wrong_times(said)

    def turn(self, *answers):
        before = len(self.manager.brain.messages)
        self.engine.responses = [reply(a) for a in answers]
        out = self.manager.send_user_message("what's left today?")
        checks = [str(m.get("content")) for m in self.manager.brain.messages[before:]
                  if str(m.get("content") or "").startswith("[System check")]
        return out, checks


class TestAShortTitleInsideALongerOne(WorkshopDay):

    def test_a_longer_title_is_no_claim_about_the_short_one(self):
        for said in ("1:10 PM Varnish the kite spars", "- 1:10 PM – Varnish the kite spars",
                     "1:10 PM – 1:50 PM Varnish the kite spars", DAY_LIST):
            with self.subTest(said=said):
                self.assertEqual(self.wrong(said), [])

    def test_the_whole_title_as_the_entry_is_still_a_claim(self):
        for said in ("1:10 PM Varnish", "- 1:10 PM – Varnish", "1:10 PM Varnish (until 1:40 PM)"):
            with self.subTest(said=said):
                self.assertEqual(self.wrong(said), ["Varnish is at 10:40 AM"])

    def test_the_day_listed_goes_out_as_written(self):
        out, checks = self.turn(DAY_LIST)
        self.assertEqual(checks, [])
        self.assertEqual(entries(out), entries(DAY_LIST))


class TestACorrectionLeavesTheListWhole(WorkshopDay):

    def test_every_true_line_stays(self):
        wrong = DAY_LIST.replace("11:20 AM Sharpen the chisels", "12:40 PM Sharpen the chisels")
        out, checks = self.turn(wrong, wrong, wrong)
        self.assertTrue(any("Sharpen the chisels is at 11:20 AM" in c for c in checks), checks)
        lines = entries(out)
        self.assertEqual(len(lines), 5, out)
        self.assertEqual([lines[0], lines[1], lines[3], lines[4]], ["Here's the rest of your day:", VARNISH, SPARS,
                                                                     BELLOWS])
        self.assertTrue(lines[2].startswith("Sharpen the chisels is "), out)
        self.assertNotIn("12:40", out)

    def test_the_replaced_line_keeps_the_list_unbroken(self):
        from thirties_core.clean_prose import problems
        from thirties_core.plain_admit import say_in_place
        text = "Today:\n- 10:40 AM Varnish\n- 11:50 AM Sharpen the chisels\n- 2:20 PM Patch the bellows\n\nThat's all."
        out, changed = say_in_place(
            text, lambda s: ["Sharpen the chisels is at 11:20 AM"] if "chisels" in s else [],
            re.compile(r"\d"), [VARNISH, CHISELS, BELLOWS])
        self.assertTrue(changed)
        self.assertEqual(out.splitlines(), ["Today:", "- 10:40 AM Varnish", "- Sharpen the chisels is at 11:20 AM",
                                            "- 2:20 PM Patch the bellows", "", "That's all."])
        self.assertEqual(problems(out), [])


if __name__ == "__main__":
    unittest.main()
