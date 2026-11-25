"""A done the user said is marked done; Python stops only what their words name as not done.

Someone says a whole round of work is finished, and names the few things in it that aren't, said on their
own. The model ticks the other items in that stretch: a correct reading. An item named in the part of the
sentence that leaves things out ("except", "apart from", "but not", "though ... are still open") is never what the
done is about, so a tick of it stops; the rest of the sentence is read without that part.

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
