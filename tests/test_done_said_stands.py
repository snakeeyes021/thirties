"""A done the user said is marked done; Python stops only what their words name as not done.

Someone says a whole round of work is finished, and names the few things in it that aren't, said on their
own. The model ticks the other items in that stretch: a correct reading. The done checks used to ask, for
each item, whether the done in that sentence was about it; the item wasn't named and the names said were other
items, so they refused it. Which items a done is for is the model's reading. Python keeps one gate (something the user
told this turn is a done: a done word, a report, a yes to a done question, an ask for a tick) and stops a tick only
of an item the words name as not done: left out of the done ("except", "apart from", "but not", "though ... are
still open", read with that part taken out of the rest), not yet, still going, told not to tick it, asked about
only in a question, or answered no to the app's question about it. What else the words say goes in the result as a
note.

Invented fixtures: a pottery studio's afternoon, with five short jobs planned from 2:40.
"""

import json
import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

WEDGE = "Wedge the stoneware"
GLAZE = "Run the glaze test"
LOG = "Fill in the kiln log"
SWEEP = "Sweep the wheel bay"
BATTS = "Order kiln batts"
WAX = "Mix the wax resist"
BLOCK = [(WEDGE, "2:40 PM", "t_wedge"), (GLAZE, "2:44 PM", "t_glaze"), (LOG, "2:48 PM", "t_log"),
         (SWEEP, "2:52 PM", "t_sweep"), (BATTS, "2:56 PM", "t_batts")]
LATER = [(WAX, "4:10 PM", "t_wax")]
OPEN_THREE = "Bisque round wrapped up. The glaze test, the kiln log and the batts order are still open."


class _Kept:
    """A database holding only the open items Thirties keeps from the lists."""

    def __init__(self, *titles):
        self.pool = json.dumps({t.lower(): {"title": t} for t in titles})

    def get_sync_meta(self, key):
        return self.pool if key == "open_pool" else None


class StudioBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        planned = BLOCK + LATER
        self.api.notes["n_errands"]["body"] = "".join(f"- [ ] {t}\n" for t, _, _ in planned)
        self.services.tasks[:] = [item(tid, t) for t, _, tid in planned]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in planned:
            minutes = "4 min" if (title, start, tid) in BLOCK else "20 min"
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": minutes, "task_id": tid})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("15:10"))

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

    def ticked(self, title):
        return f"[x] {title}" in self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.api.notes.values() if (n.get("title") or "").startswith("Done "))

    def assert_went(self, title, out):
        self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertTrue(self.ticked(title), out)

    def assert_held(self, title, out):
        self.assertTrue(out.startswith(NOT_DONE), f"{title}: {out}")
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertFalse(self.ticked(title), out)


class TestNamedAsLeftOutStops(StudioBase):
    """An item named in the part of a done that leaves things out is never ticked by that done."""

    def test_each_item_named_as_still_open_is_refused(self):
        for title in (GLAZE, LOG, BATTS):
            self.assert_held(title, self.say(OPEN_THREE, title))

    def test_the_joplin_tick_refuses_them_too(self):
        from thirties_core.write_guard import refused
        db = _Kept(*(t for t, _, _ in BLOCK))
        for title in (GLAZE, LOG, BATTS):
            self.assertTrue(refused({"op": "complete", "title": title}, f"[app] {OPEN_THREE}", db), title)

    def test_except_apart_from_and_but_not(self):
        for message, left in (("The bisque shelves came out finished, apart from the glaze test and the kiln log.",
                               (GLAZE, LOG)),
                              ("Wedging and sweeping finished, except for the batts order, which can wait.", (BATTS,)),
                              ("Wedged and swept, then the rest but the kiln log got finished on the sheet.", (LOG,)),
                              ("Stoneware wedged and the bay swept, but not the glaze test.", (GLAZE,))):
            for title in left:
                self.assert_held(title, self.say(message, title))

    def test_a_list_said_open_beside_a_done(self):
        message = "Swept the wheel bay, the glaze test and the kiln log are still open."
        for title in (GLAZE, LOG):
            self.assert_held(title, self.say(message, title))
        self.assert_went(SWEEP, self.say(message, SWEEP))

    def test_a_done_it_denies_leaves_nothing_out(self):
        self.assert_held(LOG, self.say("Bisque round: done, except the kiln log.", LOG))
        self.assert_went(LOG, self.say("Haven't done a thing on the bench save for the kiln log.", LOG))

    def test_the_app_still_showing_it_open_is_no_stop(self):
        self.assert_held(GLAZE, self.say(OPEN_THREE, GLAZE))
        self.assert_went(GLAZE, self.say("The glaze test still shows open on the list, I ran it at 2:44.", GLAZE))

    def test_a_question_leaves_nothing_out(self):
        from thirties_core.done_exceptions import left_out
        self.assertEqual(left_out("is the bisque round done except the kiln log?", LOG, [WEDGE, GLAZE]), "")
        self.assertTrue(left_out("the bisque round's done except the kiln log", LOG, [WEDGE, GLAZE]))


class TestTheModelsReadingStands(StudioBase):
    """Ticks the model chose stand; Python holds one back only where the words leave that item open."""

    def test_the_two_items_left_in_are_marked_done_on_the_plan_and_in_joplin(self):
        for title in (WEDGE, SWEEP):
            self.assert_went(title, self.say(OPEN_THREE, title))
        for title in (GLAZE, LOG, BATTS):
            self.assert_held(title, self.say(OPEN_THREE, title))

    def test_the_joplin_tick_goes_by_the_same_reading(self):
        from thirties_core.write_guard import refused
        db = _Kept(*(t for t, _, _ in BLOCK))
        for title in (WEDGE, SWEEP):
            self.assertEqual(refused({"op": "complete", "title": title}, f"[app] {OPEN_THREE}", db), "", title)
        for message in ("is the kiln log done?", "running a little behind on the wheel"):
            self.assertTrue(refused({"op": "complete", "title": WEDGE}, f"[app] {message}", db), message)
        # which item a done is for is the model's reading there too: words naming another item don't stop it
        self.assertEqual(refused({"op": "complete", "title": GLAZE}, "[app] the glaze samples are ordered",
                                 _Kept("Order glaze samples")), "")

    def test_a_done_naming_no_item_ticks_the_one_in_this_block_and_no_done_ticks_nothing(self):
        set_debug_time(at("16:25"))
        for message in ("running a little behind on the wheel", "the wax pot needs another stir",
                        "is the wax resist mixed?"):
            self.assert_held(WAX, self.say(message, WAX))
        self.assert_went(WAX, self.say("wrapped it up", WAX))

    def test_a_question_ticks_only_what_the_rest_says_is_done(self):
        message = "Is the kiln log filled in? Bisque round wrapped up; the glaze test still to do."
        self.assert_held(LOG, self.say(message, LOG))
        self.assert_held(GLAZE, self.say(message, GLAZE))
        self.assert_went(WEDGE, self.say(message, WEDGE))

    def test_not_yet_for_a_named_item_stops_it(self):
        self.assert_held(GLAZE, self.say("not yet", GLAZE, shown="Is the glaze test done?"))
        message = "Bisque round wrapped up. The glaze test isn't done yet."
        self.assert_held(GLAZE, self.say(message, GLAZE))
        self.assert_went(WEDGE, self.say(message, WEDGE))

    def test_still_going_on_a_named_item_stops_it(self):
        message = "Glaze test: still going. Rest of the bisque round: wrapped up."
        self.assert_held(GLAZE, self.say(message, GLAZE))
        self.assert_went(SWEEP, self.say(message, SWEEP))

    def test_a_dont_tick_naming_an_item_stops_it(self):
        message = "Bisque round wrapped up; don't tick the kiln log yet."
        self.assert_held(LOG, self.say(message, LOG))
        self.assert_went(WEDGE, self.say(message, WEDGE))

    def test_a_no_to_the_question_about_an_item_stops_it(self):
        shown = "Is the kiln log filled in?"
        self.assert_held(LOG, self.say("no", LOG, shown))
        self.assert_held(WEDGE, self.say("nope", WEDGE, shown))
        message = "no, bisque round wrapped up; the kiln log and the glaze test are still open"
        self.assert_held(LOG, self.say(message, LOG, shown))
        self.assert_went(SWEEP, self.say(message, SWEEP, shown))

    def test_words_naming_another_item_go_on_with_a_note_naming_it(self):
        samples = "Order glaze samples"
        self.api.notes["n_errands"]["body"] += f"- [ ] {samples}\n"
        self.services.tasks.append(item("t_samples", samples))
        self.services.tasks[-1].list_role = "list"
        out = self.say("the glaze samples are ordered", GLAZE)
        self.assert_went(GLAZE, out)
        self.assertIn(f"names {samples}", out)
        self.assertIn("t_samples", out)
        out = self.say("the kiln batts are ordered", LOG)
        self.assert_went(LOG, out)
        self.assertIn(BATTS, out)
        from thirties_core.internal_text import strip_internal
        self.assertEqual(strip_internal(out[out.index("(The user's"):]).strip(), "")


class TestASkipIsReadTheSameWay(StudioBase):
    """Which items a skip is for is the model's reading; an item the words leave out of it, or keep, stays."""

    def skip(self, message, title):
        self.t.user_message, self.t.prior_exchange = message, None
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "skipped"})
        finally:
            self.t.user_message = None

    def test_the_items_left_in_are_skipped_and_the_one_left_out_stays(self):
        message = "Apart from the stoneware, not getting to the bisque round today."
        for title in (GLAZE, BATTS):
            out = self.skip(message, title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertEqual(self.span(title).status, SpanStatus.SKIPPED, out)
        out = self.skip(message, WEDGE)
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(WEDGE).status, SpanStatus.PLANNED, out)

    def test_kept_by_name_or_no_skip_said_stays(self):
        out = self.skip("The bisque round can go, but keep the kiln log.", LOG)
        self.assertTrue(out.startswith(NOT_DONE), out)
        out = self.skip("what's next after the wheel bay?", GLAZE)
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(GLAZE).status, SpanStatus.PLANNED)
        self.assertFalse(self.skip("no time for the bisque round after all", GLAZE).startswith(NOT_DONE))


class TestABareDoneAnswersTheQuestionAsked(StudioBase):
    """A bare done answers the app's question: about one item, it's that item's, whatever else the reply listed;
    asking which, it doesn't say which."""

    def test_a_question_about_one_item_and_one_asking_which(self):
        shown = f"Not marked yet: {GLAZE}, {LOG}. Did you finish the glaze test?"
        self.assert_went(GLAZE, self.say("I did, finally!", GLAZE, shown))
        self.assert_held(LOG, self.say("finished it!", LOG, f"Which did you finish: {LOG} or {BATTS}?"))


class TestAYesTicksTheListToo(unittest.TestCase):
    """A list item ticked outside the plan (a leftover from an earlier day) is judged on the words alone: a plain
    yes, answering the question before it, is a done there too; a no is none."""

    def test_a_yes_is_written_and_a_no_is_not(self):
        from thirties_core.write_guard import refused
        for words in ("yes", "yep, that one"):
            self.assertEqual(refused({"op": "complete", "title": LOG}, f"[app] {words}", _Kept(GLAZE)), "", words)
        self.assertTrue(refused({"op": "complete", "title": LOG}, "[app] no", _Kept(GLAZE)))


class TestTheRestIsReadWithoutThatPart(unittest.TestCase):
    """What the done is about is read from the sentence with its exception part taken out."""

    def test_the_part_and_a_clause_about_it_come_out(self):
        from thirties_core.done_exceptions import without_exceptions
        self.assertEqual(without_exceptions("The bisque round's done except the glaze test, which is still firing"),
                         "The bisque round's done")
        self.assertEqual(without_exceptions("the rest but the batts order got finished on the sheet"),
                         "the rest got finished on the sheet")
        self.assertEqual(without_exceptions("wrapped it up"), "wrapped it up")

    def test_a_clause_about_an_item_left_out_says_nothing_of_the_others(self):
        from thirties_core.write_guard import going_note, state_not_done
        message = "The bisque round's done except the glaze test, which is still firing"
        others = [GLAZE, LOG, SWEEP]
        self.assertEqual(state_not_done(message, WEDGE, others), "")
        self.assertEqual(going_note(message, WEDGE, others), "")
        self.assertTrue(state_not_done(message, GLAZE, [WEDGE, LOG, SWEEP]))


if __name__ == "__main__":
    unittest.main()
