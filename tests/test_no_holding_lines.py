"""A reply never only announces a lookup, and text Python edits still reads as clean prose (holding_lines.py,
why_notes.py, clean_prose.py, plain_admit.py, reply_checks.py). Whole mock-model turns; invented data throughout."""

import re
import unittest
from unittest.mock import patch

from tests.fixtures import DAY, at
from tests.test_conversation import ConversationTestCase, reply
from thirties_core import clean_prose
from thirties_core.astronomy import set_debug_time
from thirties_core.holding_lines import COULDNT, holds, is_holding, only_holds

WHY = "how come the bowls got pushed behind the canoe?"
LOGGED = "Logged the kiln firing. Moved the bowls, with the hay order shifted after:\nPatch the canoe\nFeed the goats"
ANSWER = "The bowls went on this morning, after the canoe was already there; the record gives no other reason."


class HoldingCase(ConversationTestCase):

    bookkeeping = False

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:20"))
        self.manager.scheduler.config.assistant.turn_bookkeeping = self.bookkeeping
        self.manager.execute_tool("add_event", {"title": "Patch the canoe", "start": "4:40 PM", "duration": "1 hour"})
        self.manager.execute_tool("add_event", {"title": "Glaze the bowls", "start": "6:10 PM",
                                                "duration": "1 hour"})
        from thirties_core import morning
        morning.mark_done(self.manager.scheduler.state_db, DAY, at("07:10"))

    def script(self, *texts):
        for text in texts:
            self.engine.set_next_response(reply(text))

    def seen(self):
        """Every message the user can see."""
        return [m["content"] for m in self.manager.history() if m["role"] == "assistant"]

    def checks(self):
        return [str(m.get("content") or "") for m in self.manager.messages if self.manager.brain._is_check(m)]

    def assertNoHoldingSent(self, out):
        self.assertFalse(holds(out), out)
        for said in self.seen():
            self.assertFalse(holds(said), said)


class TestAWhyGetsItsAnswerInTheSameTurn(HoldingCase):
    """Asked why, the model hears where the record is and answers once, after its lookup, in the same turn."""

    def test_the_note_comes_with_the_message(self):
        self.script('Checking why the bowls sit behind the canoe.\nCALL item_history {"item": "Glaze the bowls"}',
                    ANSWER)
        out = self.manager.send_user_message(WHY)
        first = self.engine.call_history[0]
        notes = [str(m.get("content")) for m in first if m.get("role") == "user" and "item_history has" in
                 str(m.get("content"))]
        self.assertEqual(len(notes), 1)
        self.assertIn("Glaze the bowls", notes[0])
        self.assertIn("Patch the canoe", notes[0])
        self.assertTrue(out.startswith(ANSWER), out)
        self.assertNoHoldingSent(out)
        self.assertEqual(self.checks(), [])  # a note, never a rewrite
        self.assertEqual(len(self.engine.call_history), 2)

    def test_no_note_without_a_why(self):
        self.script("The canoe patch is up first this evening.")
        self.manager.send_user_message("what's on after the canoe?")
        self.assertFalse(any("item_history has" in str(m.get("content")) for m in self.engine.call_history[0]))

    def test_a_rewrite_that_looks_something_up_answers_after_the_result(self):
        # the first draft talks about the workings, so it's sent back; the rewrite looks the item up and says only
        # that it's checking: the turn goes on to the answer, which is what's sent
        self.script("My tools show the bowls after the canoe.",
                    'Checking why the bowls sit behind the canoe.\nCALL item_history {"item": "Glaze the bowls"}',
                    ANSWER)
        out = self.manager.send_user_message(WHY)
        self.assertTrue(out.startswith(ANSWER), out)
        self.assertNoHoldingSent(out)
        tools = [m.get("name") for m in self.manager.messages if m.get("role") == "tool"]
        self.assertIn("item_history", tools)

    def test_a_reply_that_only_announces_goes_back_for_the_answer(self):
        self.script("Let me look into that.", ANSWER)
        out = self.manager.send_user_message(WHY)
        self.assertTrue(out.startswith(ANSWER), out)
        self.assertNoHoldingSent(out)
        self.assertTrue(any("announces a lookup and stops there" in c for c in self.checks()))

    def test_a_model_that_never_answers_never_sends_the_announcement(self):
        self.script('Let me look into that.\nCALL item_history {"item": "Glaze the bowls"}', "One moment.",
                    "Hold on.", "Bear with me.", "Hang on a sec.")
        out = self.manager.send_user_message(WHY)
        self.assertTrue(out.startswith(COULDNT), out)
        self.assertNoHoldingSent(out)

    def test_an_answer_with_a_holding_line_loses_only_that_line(self):
        self.script("Let me look into that. " + ANSWER)
        out = self.manager.send_user_message(WHY)
        self.assertTrue(out.startswith(ANSWER), out)
        self.assertNoHoldingSent(out)


class TestAWhyWithTheBookkeepingOn(TestAWhyGetsItsAnswerInTheSameTurn):
    bookkeeping = True


class TestWhatCountsAsHolding(unittest.TestCase):

    def test_lines(self):
        for line in ("Checking why the hay bales went to the loft.", "Let me look into that.", "One moment.",
                     "Okay, let me check.", "I'm looking into the kiln times.", "Bear with me."):
            self.assertTrue(is_holding(line), line)
        for line in ("Looking at the afternoon, the canoe patch fits after the goats.",
                     "I'll check on the kiln when it's cooled.", "Checking in: how is the glazing going?",
                     "I'll look at the hay order tomorrow.", "I'm looking forward to the market.",
                     "The bowls went there because the kiln was busy.", "Should I check the barn first?"):
            self.assertFalse(is_holding(line), line)

    def test_only_holds_ignores_a_schedules_rows(self):
        self.assertTrue(only_holds("Checking why the bowls sit late.\n\nPatch the canoe\nGlaze the bowls"))
        self.assertFalse(only_holds("Let me check. The bowls sit late because the kiln was busy."))
        self.assertFalse(only_holds("Patch the canoe\nGlaze the bowls"))


class TestPythonEditsReadAsCleanProse(HoldingCase):
    """Each edit Python makes to a reply leaves whole sentences: no fragment, stranded colon, lower-case start, or a
    list without the sentence that introduced it."""

    def plain(self, title):
        from thirties_core.plain_admit import still_line
        return still_line(self.manager.tools, title, "update_event")

    def test_table(self):
        from thirties_core import reply_checks
        from thirties_core.holding_lines import without
        from thirties_core.plain_admit import say_failed, say_in_place
        titles = [s.title for s in self.manager.day_plan.spans]
        moved = re.compile(r"\b(?:moved|shifted|slid)\b", re.IGNORECASE)
        bowls_still = "Glaze the bowls is still in the 8:30 PM block"

        def bowls_fact(sentence):
            return [bowls_still] if "bowls" in sentence.lower() else []

        def lower_fact(sentence):
            return ["glaze the bowls is still in the 8:30 PM block"] if "bowls" in sentence.lower() else []
        plain = self.plain("Glaze the bowls")
        cases = [
            ("a promise cut",
             lambda t: reply_checks.drop_sentences(t, ["I'll stop sending the next lines from now on."]),
             "The kiln is loaded. I'll stop sending the next lines from now on. The canoe patch is up next.",
             "The kiln is loaded. The canoe patch is up next."),
            ("a promise that introduces a list goes with the list",
             lambda t: reply_checks.drop_sentences(t, ["I won't list these again:"]),
             "I won't list these again:\nPatch the canoe\nGlaze the bowls\n\nThe goats are fed.",
             "The goats are fed."),
            ("a reply that was only the promise",
             lambda t: reply_checks.drop_sentences(t, ["No more check-ins about the goats."]),
             "No more check-ins about the goats.", reply_checks.NOTHING_CHANGED),
            ("a correction opener",
             reply_checks.strip_corrections,
             "I need to correct my last message: the kiln load is in the 3:30 PM block.",
             "The kiln load is in the 3:30 PM block."),
            ("a sentence about the unseen draft",
             reply_checks.strip_corrections,
             "My first attempt didn't take. The hay delivery is in the 4:30 PM block.",
             "The hay delivery is in the 4:30 PM block."),
            ("talk of the workings",
             reply_checks.last_resort,
             "My tools show the barn is free after the canoe. Muck out the barn is in the 7:30 PM block.",
             "Muck out the barn is in the 7:30 PM block."),
            ("talk of the workings that introduces a list",
             reply_checks.last_resort,
             "Here is what my tools hold:\nFeed the goats\nGlaze the bowls\n\nThe kiln is free all evening.",
             "The kiln is free all evening."),
            ("a claim with a list, said plainly in place",
             lambda t: say_in_place(t, bowls_fact, moved, titles)[0],
             LOGGED,
             f"Logged the kiln firing. {bowls_still}; I couldn't move it."),
            ("a claim's true part kept",
             lambda t: say_in_place(t, bowls_fact, moved, titles)[0],
             "I moved the canoe patch to the 4:30 PM block and the bowls to the 7:00 PM block.",
             f"I moved the canoe patch to the 4:30 PM block. {bowls_still}; I couldn't move it."),
            ("a plain line for a title written in lower case",
             lambda t: say_in_place(t, lower_fact, moved, titles)[0],
             "Moved the bowls later.",
             f"{bowls_still}; I couldn't move it."),
            ("a failed move named in its claim",
             lambda t: say_failed(self.manager.tools, t, [("Glaze the bowls", "update_event")], moved, titles),
             "Moved the bowls to the 7:00 PM block.", plain),
            ("a lead-in naming nothing is the claim: it goes with its list",
             lambda t: say_failed(self.manager.tools, t, [("Glaze the bowls", "update_event")], moved, titles),
             "Shifted the evening around:\nPatch the canoe\nFeed the goats",
             plain),
            ("a failed move the list's opening sentence doesn't name: said ahead of that sentence, never under it",
             lambda t: say_failed(self.manager.tools, t, [("Glaze the bowls", "update_event")], moved, titles),
             "Moved the canoe patch earlier, and the goats too:\nPatch the canoe\nFeed the goats",
             f"{plain} Moved the canoe patch earlier, and the goats too:\nPatch the canoe\nFeed the goats"),
            ("a holding line beside the answer",
             without,
             "Let me check. The goats were fed in the 10:30 AM block.",
             "The goats were fed in the 10:30 AM block."),
        ]
        for name, edit, before, after in cases:
            with self.subTest(name):
                out = edit(before)
                self.assertEqual(out, after)
                self.assertEqual(clean_prose.introduced(out, [before]), [])

    def test_the_old_fragment_reads_broken(self):
        written = LOGGED
        broken = ("Logged the kiln firing. with the hay order shifted after:. Glaze the bowls is still in the 8:30 PM "
                  "block; I couldn't move it.\nPatch the canoe\nFeed the goats")
        found = {what for what, _ in clean_prose.introduced(broken, [written])}
        self.assertEqual(found, {"a sentence starting in lower case", "a stranded colon",
                                 "a list cut from its lead-in"})
        self.assertEqual(clean_prose.cut_broken(broken, clean_prose.introduced(broken, [written])),
                         "Logged the kiln firing. Glaze the bowls is still in the 8:30 PM block; I couldn't move it.")

    def test_what_the_model_wrote_itself_is_never_counted(self):
        own = "Here's the evening.\nPatch the canoe\nGlaze the bowls"
        self.assertEqual(clean_prose.introduced(own, [own]), [])


GARBLED = "Patch the canoe comes first. the bowls follow:. \nGlaze the bowls"


class TestABrokenEditFallsBackToARewrite(HoldingCase):

    def test_the_model_rewrites_it(self):
        self.script("I cannot view your plan from here. Patch the canoe comes first.",
                    "I cannot view your plan from here. Patch the canoe comes first.",
                    "Patch the canoe comes first; the bowls follow it.")
        with patch("thirties_core.reply_checks.last_resort", return_value=GARBLED):
            out = self.manager.send_user_message("what's first this evening, the canoe or the bowls?")
        self.assertTrue(out.startswith("Patch the canoe comes first; the bowls follow it."), out)
        self.assertTrue(any("reads broken" in c for c in self.checks()))
        self.assertEqual(clean_prose.problems(out.split("\n\n")[0]), [])

    def test_with_no_usable_rewrite_the_broken_sentences_go(self):
        self.script("I cannot view your plan from here. Patch the canoe comes first.",
                    "I cannot view your plan from here. Patch the canoe comes first.",
                    "One moment.")
        with patch("thirties_core.reply_checks.last_resort", return_value=GARBLED):
            out = self.manager.send_user_message("what's first this evening, the canoe or the bowls?")
        self.assertTrue(out.startswith("Patch the canoe comes first."), out)
        self.assertNotIn("follow:", out)
        self.assertNoHoldingSent(out)

    def test_a_cut_correction_that_strands_a_list_keeps_the_rewrite(self):
        rewrite = "One correction:\nPatch the canoe\nGlaze the bowls"
        self.script("I cannot view your plan from here.", rewrite, rewrite)
        out = self.manager.send_user_message("what's first this evening, the canoe or the bowls?")
        self.assertTrue(any("corrects a message the user never saw" in c for c in self.checks()))
        self.assertTrue(out.startswith(rewrite), out)  # never the rows alone, cut from what introduced them


class TestTheBrokenEditWithTheBookkeepingOn(TestABrokenEditFallsBackToARewrite):
    bookkeeping = True


if __name__ == "__main__":
    unittest.main()
