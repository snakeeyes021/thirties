"""Readings of what the user meant are the model's: a done goes on with a note, and stops only without a basis.

Words beside a done that say the item is under way or merely all right ("tapers dipped, the tallow's still warming")
used to refuse it, though the person had said it was done. Whether those words undo the done is the model's
reading: the done goes through, and its result says what else the words say. The done is still refused where the
words say plainly the item isn't finished ("not yet"), or say it's under way with no done said of it at all.
Invented fixtures: a candle maker who also lays rope.
"""

import json
import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

TAPERS = "Dip the tallow tapers"
PILLARS = "Pour the beeswax pillars"
HAWSER = "Lay the hawser on the ropewalk"


class _Kept:
    """A database holding only the open items Thirties keeps from the lists."""

    def __init__(self, *titles):
        self.pool = json.dumps({t.lower(): {"title": t} for t in titles})

    def get_sync_meta(self, key):
        return self.pool if key == "open_pool" else None


class NotesBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "".join(f"- [ ] {t}\n" for t in (TAPERS, PILLARS, HAWSER))
        self.services.tasks[:] = [item("t_tapers", TAPERS), item("t_pillars", PILLARS), item("t_hawser", HAWSER)]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ((TAPERS, "1:10 PM", "t_tapers"), (PILLARS, "1:40 PM", "t_pillars"),
                                  (HAWSER, "2:10 PM", "t_hawser")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("14:40"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, title, shown=""):
        """The model calls log_outcome for the item during a turn of the user's: the plan and Joplin see the words."""
        self.t.user_message = message
        self.t.prior_exchange = ("", shown) if shown else None
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"})
        finally:
            current_turn.reset(token)
            self.t.user_message = None
            self.t.prior_exchange = None

    def assert_went(self, title, out):
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertIn(f"[x] {title}", self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.api.notes.values() if (n.get("title") or "").startswith("Done ")))

    def assert_held(self, title, out):
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertNotIn(f"[x] {title}", self.body("n_errands"), out)


class TestUnderWayBesideADoneIsANote(NotesBase):
    def test_the_done_stands_and_the_note_says_it_is_under_way(self):
        out = self.say("tapers dipped, the tallow's still warming", TAPERS)
        self.assert_went(TAPERS, out)
        self.assertIn(f"also says {TAPERS} is under way or fine", out)

    def test_the_note_never_reaches_the_user(self):
        from thirties_core.internal_text import strip_internal
        from thirties_core.write_guard import going_note
        note = going_note("tapers dipped, the tallow's still warming", TAPERS)
        self.assertTrue(note)
        self.assertEqual(strip_internal(f"Marked it done. {note}").strip(), "Marked it done.")

    def test_all_right_in_the_same_sentence_as_the_done_needs_no_note(self):
        out = self.say("the beeswax pillars are poured, all good", PILLARS)
        self.assert_went(PILLARS, out)
        self.assertNotIn("under way or fine", out)

    def test_the_joplin_tick_follows(self):
        from thirties_core.write_guard import refused, says_done
        words = "tapers dipped, the tallow's still warming"
        self.assertTrue(says_done(words, TAPERS))
        self.assertEqual(refused({"op": "complete", "title": TAPERS}, f"[app] {words}", _Kept(PILLARS)), "")


class TestWordsSayingItIsNotDoneStillStop(NotesBase):
    def test_said_plainly_not_finished(self):
        self.assert_held(TAPERS, self.say("the tallow tapers aren't finished yet", TAPERS))

    def test_a_reply_saying_not_yet(self):
        shown = f"Are the tallow tapers dipped? {TAPERS} was at 1:10."
        self.assert_held(TAPERS, self.say("not yet, still warming", TAPERS, shown))

    def test_under_way_with_no_done_said_of_it(self):
        self.assert_held(TAPERS, self.say("the tallow for the tapers is warming", TAPERS))

    def test_a_done_said_only_of_something_else(self):
        self.assert_held(PILLARS, self.say("the beeswax pillars are fine, the hawser is laid", PILLARS))


if __name__ == "__main__":
    unittest.main()
