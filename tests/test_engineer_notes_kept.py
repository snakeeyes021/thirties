"""A note to the engineers is always kept, and the reply never says it's kept when it isn't.

- **Python's fast path takes the bracketed shape too**: "(engineers: ...)" or "[devs - ...]" anywhere in a message
  is a note, the same as "engineers:" leading it; a bracketed "(dev-only ...)" is not.
- **The model keeps the rest**: a message that speaks to the people who build the app in a wording the fast path
  doesn't know reaches the model, which keeps it with note_for_engineers (same store and file as Python's), once a
  turn, and never twice beside a note Python already kept.
- **"Kept" only when kept**: a reply saying a note was kept or passed on, with none kept this turn, goes back to the
  model once; a claim still untrue after that is cut. Listing the kept notes, a question, or a "can't" is no claim.

Whole turns on a scripted model; the fixtures are invented (a print shop's day).
"""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core import engineer_notes
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.engineer_notes import EngineerNotes, split
from thirties_core.inference import MockInferenceEngine

ASIDE = "(builders: the toner reorder shows up on the supply list twice again)"


def call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": name, "arguments": arguments}]}


class FastPathTest(unittest.TestCase):
    def test_a_bracketed_who_with_a_colon_or_dash_is_a_note(self):
        cases = {
            "(engineers: the proof preview flips sideways on the narrow window)":
                ("the proof preview flips sideways on the narrow window", ""),
            "[devs - the ink tally went blank after the sync] trim the banner stock at 2:40":
                ("the ink tally went blank after the sync", "trim the banner stock at 2:40"),
            "Laminate the menus first (the developers: the timeline skipped a row) then quote the flyers":
                ("the timeline skipped a row", "Laminate the menus first then quote the flyers"),
        }
        for message, (note, rest) in cases.items():
            with self.subTest(message=message):
                self.assertEqual(split(message), (note, rest))

    def test_brackets_that_are_not_a_note(self):
        for message in ("Reprint the posters (dev-only proofs went out by mistake)",
                        "Call the binder (engineers came by the shop yesterday)",
                        "(the drivers: pick up at 3:10) load the van"):
            with self.subTest(message=message):
                self.assertEqual(split(message)[0], "")


class ClaimTest(unittest.TestCase):
    def test_what_counts_as_saying_a_note_is_kept(self):
        tools = type("T", (), {})()
        engineer_notes.start_turn(tools, "cutter booking", "app", None)
        for text in ("Noted for the engineers.", "I've passed that along to the developers.",
                     "Flagged for the dev team, and the cutter has room this afternoon.",
                     "The engineers will see it."):
            with self.subTest(text=text):
                self.assertTrue(engineer_notes.false_kept_claim(tools, text))
        for text in ("Should I pass that on to the engineers?", "I can't pass notes to the developers from here.",
                     "The cutter has room this afternoon.", "The film developer arrives at 1:50."):
            with self.subTest(text=text):
                self.assertFalse(engineer_notes.false_kept_claim(tools, text))

    def test_a_false_claim_loses_only_its_own_sentence(self):
        tools = type("T", (), {})()
        engineer_notes.start_turn(tools, "book the cutter", "app", None)
        out = engineer_notes.without_false_kept(tools, "The cutter has room this afternoon. Noted for the engineers.")
        self.assertEqual(out, "The cutter has room this afternoon.")
        self.assertEqual(engineer_notes.without_false_kept(tools, "Noted for the engineers."), "OK.")


class KeptThroughTheTurn(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine, active_notes=[])

    def notes(self):
        return EngineerNotes(self.sched.state_db).notes()

    def tool_results(self, name):
        return [str(m.get("content")) for m in self.m.brain.messages
                if m.get("role") == "tool" and m.get("name") == name]

    def test_the_bracketed_shape_is_kept_by_python_alone(self):
        out = self.m.send_user_message("(engineers: the proof preview flips sideways on the narrow window)")
        self.assertEqual(out, engineer_notes.ACK)
        self.assertEqual(self.engine.call_history, [])
        self.assertEqual([n.text for n in self.notes()], ["the proof preview flips sideways on the narrow window"])

    def test_a_wording_python_misses_is_kept_by_the_model(self):
        self.engine.responses = [call("note_for_engineers", text="the toner reorder shows up on the supply list "
                                                                 "twice again"),
                                 reply("Kept for the people who build the app.")]
        out = self.m.send_user_message(ASIDE)
        self.assertIn("Kept", out)
        notes = self.notes()
        self.assertEqual([n.text for n in notes], ["the toner reorder shows up on the supply list twice again"])
        self.assertEqual(notes[0].message, ASIDE)  # with the message it was said in, as the fast path keeps it
        self.assertEqual(notes[0].via, "app")
        self.assertIn("twice again", EngineerNotes(self.sched.state_db).file.read_text(encoding="utf-8"))

    def test_never_twice_beside_a_note_python_kept(self):
        self.engine.responses = [call("note_for_engineers", text="the ink tally went blank after the sync"),
                                 reply("Banner trim added; your note went to the engineers.")]
        self.m.send_user_message("[devs - the ink tally went blank after the sync] trim the banner stock at 2:40")
        self.assertEqual(len(self.notes()), 1)
        self.assertTrue(self.tool_results("note_for_engineers")[0].startswith("Already kept"))

    def test_a_false_claim_goes_back_and_the_model_keeps_the_note(self):
        self.engine.responses = [reply("Noted for the engineers."),
                                 call("note_for_engineers", text="the toner reorder shows up on the supply list "
                                                                 "twice again"),
                                 reply("Kept for the people who build the app.")]
        out = self.m.send_user_message(ASIDE)
        told = [str(m.get("content")) for m in self.engine.call_history[1] if m.get("role") == "user"]
        self.assertIn(engineer_notes.CHECK, told[-1])
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("Kept", out)

    def test_a_claim_still_untrue_after_the_rewrite_is_cut(self):
        self.engine.responses = [reply("The cutter is free all afternoon. Noted for the engineers."),
                                 reply("The cutter is free all afternoon. I've passed that along to the developers.")]
        out = self.m.send_user_message("is the cutter free this afternoon? the builders should hear the jam "
                                       "alarm is too quiet")
        self.assertEqual(self.notes(), [])
        self.assertNotIn("engineers", out)
        self.assertNotIn("developers", out)
        self.assertIn("free all afternoon", out)

    def test_listing_the_kept_notes_is_no_false_claim(self):
        EngineerNotes(self.sched.state_db).add("the proof preview flips sideways", via="app")
        self.engine.responses = [call("engineer_notes"),
                                 reply("Your note kept for the engineers: proof preview flips sideways.")]
        out = self.m.send_user_message("what have I told the builders so far?")
        self.assertIn("kept for the engineers", out)
        self.assertEqual(len(self.engine.call_history), 2)  # no send-back


if __name__ == "__main__":
    unittest.main()
