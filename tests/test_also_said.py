"""Words that may name something besides the item, or name another item, are the model's to read, with a note.

A done whose words share a word with the item but also say a word it lacks ("wrenhollow files sent!!" for Export
Wrenhollow marionette patterns) was refused as naming something else, though the person meant the item. Whether
such a word fits is the model's reading: the done goes on, and its result says what else the words say, so the
model can ask or put it right. When those words name another planned or listed item (all that item's words, or
its head noun), the done goes on too, with the same note; the Joplin tick follows the same reading.
Invented fixtures: a marionette maker who also keeps a falcon.
"""

import json
import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

PATTERNS = "Export Wrenhollow marionette patterns"
SWATCH = "Gesso swatch for the marionette"
HANDS = "Carve the marionette hands"
ORDER = "Send the gesso order"
PERCH = "Sand the falcon perch"


class _Kept:
    """A database holding only the open items Thirties keeps from the lists."""

    def __init__(self, *titles):
        self.pool = json.dumps({t.lower(): {"title": t} for t in titles})

    def get_sync_meta(self, key):
        return self.pool if key == "open_pool" else None


class AlsoSaidBase(TodayTestCase):
    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = "".join(f"- [ ] {t}\n" for t in (PATTERNS, SWATCH, HANDS))
        self.services.tasks[:] = [item("t_patterns", PATTERNS), item("t_swatch", SWATCH), item("t_hands", HANDS)]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ((PATTERNS, "1:10 PM", "t_patterns"), (SWATCH, "1:40 PM", "t_swatch"),
                                  (HANDS, "2:10 PM", "t_hands")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("14:40"))

    def listed(self, title, tid):
        self.api.notes["n_errands"]["body"] += f"- [ ] {title}\n"
        self.services.tasks.append(item(tid, title))
        self.services.tasks[-1].list_role = "list"

    def planned(self, title):
        out = self.t.execute("add_event", {"title": title, "start": "3:20 PM", "duration": "20 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, title):
        """The model calls log_outcome for the item during a turn of the user's: the plan and Joplin see the words."""
        self.t.user_message = message
        token = current_turn.set(f"[app] {message}")
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"})
        finally:
            current_turn.reset(token)
            self.t.user_message = None

    def assert_went(self, title, out):
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertIn(f"[x] {title}", self.body("n_errands") + "".join(
            n.get("body") or "" for n in self.api.notes.values() if (n.get("title") or "").startswith("Done ")))

    def assert_held(self, title, out, named):
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(named, out)
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)
        self.assertNotIn(f"[x] {title}", self.body("n_errands"), out)


class TestOneExtraWordIsTheModelsToRead(AlsoSaidBase):
    def test_the_project_and_a_noun_of_its_own_go_on_with_a_note(self):
        out = self.say("wrenhollow files sent!!", PATTERNS)
        self.assert_went(PATTERNS, out)
        self.assertIn('also says "wrenhollow files"', out)
        self.assertIn("ask which, or correct it", out)

    def test_the_note_never_reaches_the_user(self):
        from thirties_core.done_fit import also_said
        from thirties_core.internal_text import strip_internal
        note = also_said("wrenhollow files sent!!", PATTERNS)
        self.assertTrue(note)
        self.assertEqual(strip_internal(f"Marked it done. {note}").strip(), "Marked it done.")

    def test_words_that_fit_the_item_carry_no_note(self):
        out = self.say("the wrenhollow marionette patterns are exported", PATTERNS)
        self.assert_went(PATTERNS, out)
        self.assertNotIn("also says", out)

    def test_a_head_noun_no_other_item_has_goes_on_with_a_note(self):
        out = self.say("the marionette perch is carved", HANDS)
        self.assert_went(HANDS, out)
        self.assertIn('"marionette perch"', out)


class TestWordsNamingAnotherItemAreANote(AlsoSaidBase):
    def test_all_of_a_listed_items_words(self):
        self.listed(ORDER, "t_order")
        out = self.say("the gesso order is sent", SWATCH)
        self.assert_went(SWATCH, out)
        self.assertIn('also says "gesso order"', out)

    def test_all_of_a_planned_items_words(self):
        self.planned(ORDER)
        out = self.say("the gesso order is sent", SWATCH)
        self.assert_went(SWATCH, out)
        self.assertIn(ORDER, out)

    def test_an_extra_noun_that_is_a_listed_items_head(self):
        self.listed(PERCH, "t_perch")
        out = self.say("the marionette perch is carved", HANDS)
        self.assert_went(HANDS, out)
        self.assertIn('also says "marionette perch"', out)

    def test_a_noun_of_its_own_still_goes_on_beside_another_listed_item(self):
        self.listed(PERCH, "t_perch")
        self.assert_went(PATTERNS, self.say("wrenhollow files sent!!", PATTERNS))


class TestWhatNamesAnotherItem(unittest.TestCase):
    def test_named_instead(self):
        from thirties_core.done_fit import named_instead
        self.assertEqual(named_instead("the gesso order is sent", SWATCH, listed=[ORDER, PERCH]), [ORDER])
        self.assertEqual(named_instead("the marionette perch is carved", HANDS, listed=[ORDER, PERCH]), [PERCH])
        self.assertEqual(named_instead("the gesso order is sent", SWATCH, listed=[PERCH]), [])
        self.assertEqual(named_instead("wrenhollow files sent!!", PATTERNS, listed=[ORDER, PERCH]), [])
        self.assertEqual(named_instead("brushed the marionette's gesso swatch", SWATCH, listed=[ORDER]), [])

    def test_the_joplin_tick_follows(self):
        from thirties_core.write_guard import refused

        def tick(title, words, db):
            return refused({"op": "complete", "title": title}, f"[app] {words}", db)
        self.assertEqual(tick(SWATCH, "the gesso order is sent", _Kept(ORDER)), "")  # the model's reading
        self.assertEqual(tick(HANDS, "the marionette perch is carved", _Kept(PERCH)), "")
        self.assertEqual(tick(SWATCH, "the gesso order is sent", None), "")
        self.assertTrue(tick(SWATCH, "the gesso order goes out tomorrow", _Kept(ORDER)))  # no done said
        self.assertEqual(tick(PATTERNS, "wrenhollow files sent!!", _Kept(ORDER, PERCH)), "")


if __name__ == "__main__":
    unittest.main()
