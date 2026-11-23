"""A note to the engineers is never the user's words about an item.

- **A done asked for beside a note stands.** "Aligned the telescope." with a note to the engineers full of
  negative words about the same item ("didn't", "hasn't") is marked done, on the plan and in Joplin; the note is
  still kept.
- **The note's words alone make no change.** A note saying an item was done or moved, beside a question, marks
  and moves nothing.
- **A message that is only a note never reaches the model**, however its words read.
- **Earlier messages are read without their notes too**, by every check that looks back at what the user said,
  and a hold waiting for the user's words is never ended by a note.

Through whole turns with a scripted model on the mock Joplin API (no real calls). Fixtures are invented: an
observatory's morning.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase
from thirties_core import engineer_notes
from thirties_core.astronomy import set_debug_time
from thirties_core.change_asks import recent_said
from thirties_core.done_reference import prior_exchange
from thirties_core.engineer_notes import EngineerNotes
from thirties_core.inference import MockInferenceEngine
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

SCOPE, GAUGE, DIAL = "Align the telescope", "Log the barometer readings", "Calibrate the sundial"
STEPS = (SCOPE, GAUGE, DIAL)
NEGATIVE_NOTE = ("(note to the developers: the telescope row didn't get its shutter icon on the wide layout, and "
                 "it hasn't shown one since)")
CHANGE_NOTE = ("(note to the developers: in the demo, Align the telescope was done and Calibrate the sundial got "
               "moved to 2:10 PM, but the screen didn't refresh)")


class ObservatoryMorning(TodayTestCase):
    """A Dome prep routine of three steps in two parts, past their time, and a conversation on a scripted model."""

    def setUp(self):
        super().setUp()
        TITLES["n_dome"] = "Dome prep"
        self.addCleanup(TITLES.pop, "n_dome", None)
        self.api.notes["n_dome"] = {"id": "n_dome", "body": "".join(f"- [ ] {t}\n" for t in STEPS)}
        ListCatalog(self.sched.state_db).set_role("n_dome", "routine", "Dome prep")
        set_debug_time(at("10:20"))
        out = self.t.execute("put_routine", {"routine": "Dome prep", "parts": [
            {"start": "8:40 AM", "items": [SCOPE, GAUGE]}, {"start": "9:50 AM", "items": [DIAL]}]})
        self.assertFalse(str(out).startswith(NOT_DONE), out)
        from thirties_core.conversation import ConversationManager
        self.engine = MockInferenceEngine()
        self.m = ConversationManager(self.t.day_plan, self.services.tasks, [], self.sched,
                                     joplin_engine=self.services.joplin, inference_engine=self.engine)

    def span(self, title):
        return next(s for s in self.m.day_plan.spans if s.title == title)

    def ticked(self, title):
        return any(f"[x] {title}" in n["body"] for n in self.api.notes.values())

    def turn(self, message, calls, answer="Noted."):
        before = len(self.m.brain.messages)
        self.engine.responses = [{"role": "assistant", "content": "", "tool_calls": calls},
                                 {"role": "assistant", "content": answer, "tool_calls": []}]
        self.m.send_user_message(message)
        return [str(x.get("content")) for x in self.m.brain.messages[before:] if x.get("role") == "tool"]


class TestADoneBesideANote(ObservatoryMorning):

    def test_the_done_stands_and_the_note_is_kept(self):
        for message in (f"Aligned the telescope.\n\n{NEGATIVE_NOTE}", f"{NEGATIVE_NOTE} Aligned the telescope."):
            with self.subTest(message=message):
                self.doCleanups()
                self.setUp()
                outs = self.turn(message, [{"name": "log_outcome", "arguments": {"event": SCOPE, "status": "done"}}],
                                 "The telescope is marked done.")
                self.assertFalse(any(o.startswith(NOT_DONE) for o in outs), outs)
                self.assertEqual(self.span(SCOPE).status, SpanStatus.DONE)
                self.assertTrue(self.ticked(SCOPE))
                notes = EngineerNotes(self.sched.state_db).notes()
                self.assertEqual(len(notes), 1)
                self.assertIn("shutter icon on the wide layout", notes[0].text)


class TestTheNoteAloneChangesNothing(ObservatoryMorning):

    def test_no_done_or_move_from_the_note(self):
        calls = [{"name": "log_outcome", "arguments": {"event": SCOPE, "status": "done"}},
                 {"name": "update_event", "arguments": {"event": DIAL, "start": "2:10 PM"}}]
        dial_start = self.span(DIAL).start_dt
        outs = self.turn(f"What's left this morning?\n\n{CHANGE_NOTE}", calls)
        self.assertEqual(len(outs), 2)
        self.assertTrue(all(o.startswith(NOT_DONE) for o in outs), outs)
        self.assertEqual(self.span(SCOPE).status, SpanStatus.PLANNED)
        self.assertEqual(self.span(DIAL).start_dt, dial_start)
        self.assertFalse(any(self.ticked(t) for t in STEPS))

    def test_a_message_that_is_only_a_note_never_reaches_the_model(self):
        before = [s.to_dict() for s in self.m.day_plan.spans]
        out = self.m.send_user_message("Note to the developers: Align the telescope is done, mark it, and move the "
                                       "sundial to 2:10 PM")
        self.assertEqual(out, engineer_notes.ACK)
        self.assertEqual(self.engine.call_history, [])
        self.assertEqual([s.to_dict() for s in self.m.day_plan.spans], before)
        self.assertFalse(any(self.ticked(t) for t in STEPS))


class TestEarlierMessagesWithoutTheirNotes(unittest.TestCase):

    def test_looking_back_reads_the_words_without_the_note(self):
        from thirties_core.engineer_notes import as_said, without_note
        messages = [{"role": "user", "content": f"Calibrate the sundial first. {CHANGE_NOTE}"},
                    {"role": "assistant", "content": "Done: the sundial is first."},
                    {"role": "user", "content": "Note to the developers: the telescope item was done twice"},
                    {"role": "assistant", "content": engineer_notes.ACK}]
        seen = as_said(messages)
        self.assertEqual(seen[0]["content"], "Calibrate the sundial first.")
        self.assertEqual(seen[2]["content"], "")
        self.assertEqual(messages[2]["content"], "Note to the developers: the telescope item was done twice")
        self.assertEqual(recent_said(seen), ["Calibrate the sundial first."])
        self.assertEqual(prior_exchange(seen)[0], "Calibrate the sundial first.")
        self.assertEqual(without_note("Aligned the telescope."), "Aligned the telescope.")

    def test_a_hold_waiting_for_words_is_not_ended_by_a_note(self):
        from thirties_core.holds import _words, said_over
        hold = {"words": _words("dome open"), "since": "2026-10-15T09:01:00-04:00"}
        note = {"role": "user", "content": "Note to the developers: the dome open button looks grey",
                "at": "2026-10-15T09:11:00-04:00"}
        said = {"role": "user", "content": "The dome is open now", "at": "2026-10-15T09:21:00-04:00"}
        self.assertFalse(said_over(hold, [note]))
        self.assertTrue(said_over(hold, [note, said]))


if __name__ == "__main__":
    unittest.main()
