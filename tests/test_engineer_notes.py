"""Notes to the engineers: kept for review, easy to see, never acted on. Invented messages throughout."""

import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core import engineer_notes
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.engineer_notes import EngineerNotes, split
from thirties_core.inference import MockInferenceEngine


class SplitTest(unittest.TestCase):
    def test_the_ways_a_note_is_written(self):
        cases = {
            "Note to the engineers: the check-in came twice.": ("the check-in came twice.", ""),
            "note for devs - the clock hand hides the sunset mark": ("the clock hand hides the sunset mark", ""),
            "Engineers: the answer came back blank twice": ("the answer came back blank twice", ""),
            "dev note: drag snapping felt off": ("drag snapping felt off", ""),
            "(note to the developers: this took two tries) move lunch to 1":
                ("this took two tries", "move lunch to 1"),
            "Move lunch to 1.\n\nNote to the engineers: it moved dinner too last time.":
                ("it moved dinner too last time.", "Move lunch to 1."),
        }
        for message, (note, rest) in cases.items():
            with self.subTest(message=message):
                self.assertEqual(split(message), (note, rest))

    def test_ordinary_messages_have_no_note(self):
        for message in ("I engineered a new shelf today", "the developers' meetup is at 6, add it",
                        "note to self: buy milk", "Remind me to email the engineers tomorrow"):
            with self.subTest(message=message):
                self.assertEqual(split(message)[0], "")


class KeptNotesTest(unittest.TestCase):
    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        self.m = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine, active_notes=[])

    def test_a_note_alone_is_kept_and_never_reaches_the_model(self):
        before = [s.to_dict() for s in self.m.day_plan.spans]
        out = self.m.send_user_message("Note to the engineers: move the bell later, it rings during calls")
        self.assertEqual(out, engineer_notes.ACK)
        self.assertEqual(self.engine.call_history, [])  # the model never saw it: nothing could be acted on
        self.assertEqual([s.to_dict() for s in self.m.day_plan.spans], before)
        notes = EngineerNotes(self.sched.state_db).notes()
        self.assertEqual([n.text for n in notes], ["move the bell later, it rings during calls"])
        self.assertEqual(notes[0].via, "app")
        history = self.m.history()
        self.assertEqual([h["role"] for h in history[-2:]], ["user", "assistant"])  # in the conversation as said

    def test_a_note_in_a_longer_message_is_kept_and_the_model_told_to_leave_it(self):
        self.engine.responses = [reply("Lunch is at 1 now.")]
        self.m.send_user_message("Move lunch to 1.\n\nNote to the engineers: it moved dinner too last time.")
        seen = self.engine.call_history[0]
        told = [m["content"] for m in seen if m.get("role") == "user"]
        self.assertTrue(any("kept for them" in t and "Take no action" in t for t in told), told)
        self.assertIn("Move lunch to 1.", told[-1])  # the user's own words, as they said them
        self.assertEqual(len(EngineerNotes(self.sched.state_db).notes()), 1)
        self.assertNotIn("kept for them", " ".join(h["content"] for h in self.m.history()))  # hidden from the chat

    def test_easy_to_see_a_file_beside_the_database_and_a_tool(self):
        store = EngineerNotes(self.sched.state_db)
        store.add("the morning message came at 3 AM", via="telegram")
        text = store.file.read_text(encoding="utf-8")
        self.assertIn("# Notes to the engineers", text)
        self.assertIn("the morning message came at 3 AM", text)
        self.assertIn("(telegram)", text)
        out = self.m.tools.execute("engineer_notes", {})
        self.assertIn("the morning message came at 3 AM", out)
        note = store.notes()[0]
        again = store.add("the morning message came at 3 AM", via="telegram")  # a retried turn: kept once
        self.assertEqual(again.id, note.id)
        self.assertEqual(len(store.notes()), 1)
        store.mark(note.id)  # reviewed: off the list and out of the file
        self.assertEqual(store.notes(), [])
        self.assertIn("None open.", store.file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
