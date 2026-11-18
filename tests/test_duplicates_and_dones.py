"""An item is on the plan once, and a done the user clearly reports is recorded.

Seen in simulated days: starting an item by its list id added a second copy beside the one planned later, and the
user's "sent" for it was then refused as unclear; clearing time out of an item and stretching it back left a piece
of it inside itself, listed twice, and neither that piece nor a copy the user said was "on there twice" could be
taken off; "the trimming still isnt marked lol" was read as the trimming not having happened; an item named only by
the words it shares with another item was refused as unclear even when the reply had just listed it alone, and an
"ok" opening the message was read as the item only going fine. Now:

- doing_now for an item's list id moves its planned copy to now (duplicates.py);
- stretching an item over a copy of itself folds that copy in, and a copy inside another copy's time, or one the
  user says is there twice, can come off;
- "not marked" with chat words after it is about the record, not the thing (started.py);
- words that fit two items equally point to the one the last reply listed alone (done_reference.py), and a lone
  "ok" is no word about an item (write_guard.py).

What stays: going, running or fine is never a done; a second session at another time stays; an item the words
don't point to isn't marked. On invented items only: a late shift at a pottery studio and an evening paddle.
"""

import unittest

from tests.fixtures import at
from tests.test_conversation import ConversationTestCase, reply
from tests.test_done_guard import GuardCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE
from thirties_core.started import said_not_done
from thirties_core.write_guard import says_done, state_not_done

GLAZE = "Glaze Run: 4 test tile sets + firing sheet"
GLAZE_CALL = "Glaze Run - kiln tech call"
PORTAGE = "Portage the canoe"
WEDGE_ID = "4be0c1d2a3f49e77"
WEDGE = "Wedge clay for the night throw"


def item(tid, title):
    return TaskItem(id=tid, source_notebook="Tasks", title=title, is_checkbox_item=True, parent_note_title="Studio",
                    parent_note_id="n_studio", list_role="list")


class TestTheRecordIsNotTheThing(unittest.TestCase):
    """'not marked' is about the app's record, with chat words after it too."""

    def test_record_words_with_chat_after_them(self):
        others = ["Fire the kiln", PORTAGE]
        for message in ("also the trimming still isnt marked lol",
                        "trim the bowls still not marked tho?? finished them an hour ago, pls mark it done",
                        "the trimming isn't ticked again",
                        "trimming still not checked off for some reason"):
            self.assertFalse(said_not_done(message, "Trim the bowls", others), message)

    def test_saying_it_hasnt_happened_still_holds(self):
        for message in ("haven't trimmed the bowls yet lol", "didn't get to the trimming tho"):
            self.assertTrue(said_not_done(message, "Trim the bowls", ["Fire the kiln"]), message)


class TestSharedWordsAndAnOk(unittest.TestCase):

    def test_an_ok_opening_the_message_is_no_word_about_the_item(self):
        message = "shelves are restocked!! ok and quit showing the old stuff, glaze run + portage are DONE"
        self.assertEqual(state_not_done(message, GLAZE, [GLAZE_CALL, PORTAGE]), "")
        self.assertTrue(says_done(message, GLAZE))

    def test_going_or_fine_is_still_no_done(self):
        for message in ("glaze run is going ok", "glaze run's fine i guess", "ok the glaze run is firing"):
            self.assertTrue(state_not_done(message, GLAZE, [GLAZE_CALL, PORTAGE]), message)
        self.assertTrue(state_not_done("it's running", GLAZE, [GLAZE_CALL]))


class SharedNameCase(GuardCase):
    """Two items whose names open alike; the first was begun and stays open, and the call began after it."""

    def setUp(self):
        super().setUp()
        set_debug_time(at("15:30"))
        for title, start, length in ((GLAZE, "3:30 PM", "30 minutes"), (GLAZE_CALL, "4:00 PM", "45 minutes"),
                                     (PORTAGE, "5:00 PM", "30 minutes")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("18:40"))


class TestTheOneJustListed(SharedNameCase):

    def test_the_reply_listed_it_alone(self):
        shown = f"Still open:\n3:30 PM – {GLAZE}\n\n7:30 PM – Paddle out"
        out = self.done("glaze run is DONE, said so already. portage is done too, mark both", GLAZE, ("", shown))
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(GLAZE).status, SpanStatus.DONE)

    def test_the_ok_message_marks_both_it_names(self):
        shown = f"Still open:\n3:30 PM – {GLAZE}\n5:00 PM – {PORTAGE}"
        message = "shelves are restocked!! ok and quit showing the old stuff, glaze run + portage are DONE"
        for title in (GLAZE, PORTAGE):
            out = self.done(message, title, ("", shown))
            self.assertFalse(out.startswith(NOT_DONE), out)

    def test_listed_with_the_other_it_stays_unclear(self):
        shown = f"Still open:\n{GLAZE}\n{GLAZE_CALL}"
        out = self.done("glaze run is done", GLAZE, ("", shown))
        self.assertTrue(out.startswith(NOT_DONE), out)

    def test_going_fine_is_still_refused_when_listed(self):
        shown = f"Still open:\n3:30 PM – {GLAZE}"
        out = self.done("glaze run is going fine", GLAZE, ("", shown))
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(GLAZE).status, SpanStatus.PLANNED)


class CopiesCase(GuardCase):

    def say(self, message, tool, args):
        self.t.user_message, self.t.prior_exchange = message, ("", "")
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message, self.t.prior_exchange = None, None

    def copies(self, title):
        return [s for s in self.t.day_plan.spans if s.title == title and s.status == SpanStatus.PLANNED]


class TestStretchedOverItsOwnCopy(CopiesCase):

    def test_clearing_then_stretching_back_leaves_one_item(self):
        self.t.execute("add_event", {"title": "Throw the cylinder series", "start": "5:00 PM", "duration": "3 hours"})
        out = self.say("free up 6 to 7 for the canoe", "clear_time", {"start": "6:00 PM", "end": "7:00 PM"})
        self.assertEqual(len(self.copies("Throw the cylinder series")), 2, out)
        first = min(self.copies("Throw the cylinder series"), key=lambda s: s.start_dt)
        out = self.say("never mind, cylinders 5 to 8 after all", "update_event",
                       {"event": first.id, "start": "5:00 PM", "end": "8:00 PM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        left = self.copies("Throw the cylinder series")
        self.assertEqual([(s.start_dt, s.end_dt) for s in left], [(at("17:00"), at("20:00"))])

    def test_a_second_session_at_another_time_stays(self):
        self.t.execute("add_event", {"title": "Throw the cylinder series", "start": "5:00 PM", "duration": "1 hour"})
        self.t.execute("add_event", {"title": "Throw the cylinder series", "start": "8:00 PM", "duration": "1 hour",
                                     "again": True})
        first = min(self.copies("Throw the cylinder series"), key=lambda s: s.start_dt)
        self.say("cylinders 5 to 6:30", "update_event", {"event": first.id, "end": "6:30 PM"})
        self.assertEqual(len(self.copies("Throw the cylinder series")), 2)


class TestTakingACopyOff(CopiesCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("08:00"))
        for title, start, length, again in ((GLAZE, "10:00 AM", "2 hours", False), (GLAZE, "1:00 PM", "2 hours", True),
                                            (GLAZE_CALL, "4:00 PM", "45 minutes", False)):
            self.t.execute("add_event", {"title": title, "start": start, "duration": length, "again": again})

    def test_a_copy_the_user_says_is_there_twice_comes_off(self):
        later = max(self.copies(GLAZE), key=lambda s: s.start_dt)
        out = self.say("glaze run is on there twice, just 10 to 12", "remove_event", {"event": later.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.copies(GLAZE)), 1)
        self.assertEqual(len(self.copies(GLAZE_CALL)), 1)

    def test_the_item_without_copies_isnt_taken_by_it(self):
        call = self.copies(GLAZE_CALL)[0]
        out = self.say("glaze run is on there twice, just 10 to 12", "remove_event", {"event": call.id})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.copies(GLAZE_CALL)), 1)

    def test_a_copy_inside_another_copys_time_comes_off(self):
        self.t.execute("add_event", {"title": GLAZE, "start": "11:00 AM", "duration": "30 minutes", "again": True})
        inner = next(s for s in self.copies(GLAZE) if s.start_dt == at("11:00"))
        out = self.say("the glazing is messed up, fix it", "remove_event", {"event": inner.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.copies(GLAZE)), 2)

    def test_a_second_session_apart_isnt_taken_off_unasked(self):
        later = max(self.copies(GLAZE), key=lambda s: s.start_dt)
        out = self.say("move the kiln tech call to 4:30", "remove_event", {"event": later.id})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(len(self.copies(GLAZE)), 2)


class TestStartedByItsListId(ConversationTestCase):
    """The model starts a planned list item by the id of its list line, then the user says it's done."""

    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(tasks=[item(WEDGE_ID, WEDGE)])
        set_debug_time(at("16:30"))
        out = self.manager.execute_tool("add_event", {"title": WEDGE, "start": "8:00 PM", "duration": "30 minutes",
                                                      "task_id": WEDGE_ID})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def planned(self):
        return [s for s in self.manager.day_plan.spans if s.title == WEDGE]

    def test_start_now_moves_the_planned_one_and_the_done_records(self):
        set_debug_time(at("18:00"))
        self.engine.set_next_response(reply(f'CALL doing_now {{"item": "{WEDGE_ID}"}}'))
        self.engine.set_next_response(reply("Wedging now."))
        self.manager.send_user_message("wedging the clay now instead of at 8, paddling after")
        spans = self.planned()
        self.assertEqual(len(spans), 1, [(s.start_dt, s.status) for s in spans])
        self.assertEqual(spans[0].start_dt, at("18:00"))
        set_debug_time(at("18:35"))
        self.engine.set_next_response(reply(f'CALL log_outcome {{"event": "{spans[0].id}", "status": "done"}}'))
        self.engine.set_next_response(reply("Marked."))
        self.manager.send_user_message("clay is wedged and bagged, finished")
        self.assertEqual([s.status for s in self.planned()], [SpanStatus.DONE])


if __name__ == "__main__":
    unittest.main()
