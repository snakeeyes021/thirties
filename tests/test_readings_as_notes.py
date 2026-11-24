"""Readings of what the user meant are the model's: a done goes on with a note, and stops only without a basis.

Words beside a done that say the item is under way or merely all right ("tapers dipped, the tallow's still warming")
used to refuse it, though the person had said it was done. Whether those words undo the done is the model's
reading: the done goes through, and its result says what else the words say. The done is still refused where the
words say plainly the item isn't finished ("not yet"), or say it's under way with no done said of it at all.

Words that fit another item better ("the tallow got strained" for Dip the tallow tapers, beside Strain the tallow
into the moulds) are the model's to read too: the done goes on, and its result names that item. It stops when the
words name the other item outright (all its words), or say nothing of this one. Which clauses carry the done is
read generously: every clause with a done word or a past in it ("got to finish the tallow").
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


STRAIN = "Strain the tallow into the moulds"
STRAINED = "Strain the tallow"


class TestAnotherItemThatFitsBetterIsANote(NotesBase):
    """Words that fit another item better, without naming it outright, used to refuse the done."""

    def planned(self, title):
        out = self.t.execute("add_event", {"title": title, "start": "1:50 PM", "duration": "20 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def listed(self, title, tid):
        self.api.notes["n_errands"]["body"] += f"- [ ] {title}\n"
        self.services.tasks.append(item(tid, title))
        self.services.tasks[-1].list_role = "list"

    def test_a_planned_item_that_fits_better_is_named_in_the_note(self):
        self.planned(STRAIN)
        out = self.say("the tallow got strained", TAPERS)
        self.assert_went(TAPERS, out)
        self.assertIn(f"may also mean {STRAIN}", out)

    def test_a_list_item_that_fits_better_is_named_in_the_note(self):
        self.listed(STRAIN, "t_strain")
        out = self.say("the tallow got strained", TAPERS)
        self.assert_went(TAPERS, out)
        self.assertIn(STRAIN, out)

    def test_a_word_of_its_own_needs_no_note(self):
        self.planned(STRAIN)
        out = self.say("strained the tallow and dipped the tapers", TAPERS)
        self.assert_went(TAPERS, out)
        self.assertNotIn("may also mean", out)

    def test_an_item_named_outright_still_stops_it(self):
        self.planned(STRAINED)
        self.assert_held(TAPERS, self.say("the tallow got strained", TAPERS))

    def test_a_done_saying_nothing_of_it_still_stops_it(self):
        self.assert_held(TAPERS, self.say("the hawser is laid out on the ropewalk", TAPERS))



class TestEveryClauseWithADoneWordCounts(NotesBase):
    """A clause putting the done in a way that read as not saying it ("got to finish the tallow") couldn't be
    the done's basis, so a done said there beside another clause's done was refused."""

    def setUp(self):
        super().setUp()
        out = self.t.execute("add_event", {"title": STRAINED, "start": "12:40 PM", "duration": "20 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.say("strained", STRAINED)
        self.assertEqual(self.span(STRAINED).status, SpanStatus.DONE)

    def test_a_done_put_any_way_is_the_models_to_read(self):
        out = self.say("the hawser got coiled, and finally got to finish the tallow", TAPERS)
        self.assert_went(TAPERS, out)

    def test_the_clauses_with_a_done_word(self):
        from thirties_core.plain_done import done_clauses
        self.assertEqual(done_clauses("the moulds can wait, got to finish the tallow"),
                         [" got to finish the tallow"])

    def test_a_clause_with_no_done_word_is_still_no_basis(self):
        self.assert_held(TAPERS, self.say("the tallow can wait, the hawser got coiled", TAPERS))


if __name__ == "__main__":
    unittest.main()
