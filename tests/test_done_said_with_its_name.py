"""A done that names the item and says it's finished goes through, whatever else the message says.

- **A pointer beside the name.** "the saltmarsh shawl is done, tick it" after a reply listing several items: the
  "it" is the shawl the message just named, not one of the items listed. Which of the items its words fit is the
  model's reading. A bare "tick it" after such a list, with no word of any item, is still refused.
- **All right somewhere else.** "ok the list still shows it open. the shawl IS done": an "ok" or "fine" in another
  sentence doesn't make the done read as merely all right. Said of the item with no done anywhere, it still does,
  and so does a word for under way.
- **A "don't tick it" about another item.** "the heddles are done (and I found the lost shuttle). the tea towel
  weft is still going, don't tick it": the "it" is the weft its own sentence names, so the heddles are ticked and
  the weft isn't. Said in a sentence naming no other item ("the heddles are done, but don't tick it yet"), it still
  holds the tick.
- **Another item named in another clause.** "Someone shifted my selvedge swatch over to 1:10. Treadle: wrapped
  up.": the swatch fits more of those words, but "treadle" is a word of this item the swatch lacks, so the swatch
  is no rival in its place. Words that fit another item better and say nothing of this one beyond it are still
  refused.
- **A list sharing one done.** "bobbins and dusting and shuttle oiling + mordant are all wrapped up": the
  names ahead of the done are done too, so the afternoon dusting is ticked beside the shuttle oiling. A
  stretch that says something of its own ("the dusting can wait, ...") stays out of it, and a later dusting of
  the evening was never said.

Through the real tool path on the mock Joplin API (no real calls). Fixtures are invented: a weaving studio's day.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

SHAWL, SHAWL_CALL, BOBBINS = ("Saltmarsh shawl: finish the weft", "Saltmarsh shawl - buyer review call",
                              "Rewind the bobbins")


class StudioDay(TodayTestCase):
    """Studio items from one list, each on the plan at its own time."""

    ITEMS = ((SHAWL, "1:10 PM", "1 hour 30 min", "t_shawl"), (SHAWL_CALL, "3:10 PM", "40 min", "t_call"),
             (BOBBINS, "4:20 PM", "20 min", "t_bobbins"))
    NOW = "16:10"

    def setUp(self):
        super().setUp()
        TITLES["n_studio"] = "Studio"
        self.addCleanup(TITLES.pop, "n_studio", None)
        self.api.notes["n_studio"] = {"id": "n_studio", "body": "".join(f"- [ ] {t}\n" for t, *_ in self.ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_studio", "list", "Studio")
        self.services.tasks[:] = [item(tid, title, "n_studio") for title, _, _, tid in self.ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        set_debug_time(at("7:10"))
        for title, start, length, tid in self.ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length, "task_id": tid,
                                               "alongside": True})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        set_debug_time(at(self.NOW))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, title, shown, status="done"):
        """The model calls log_outcome during a turn of the user's: the plan and every Joplin write see their words."""
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", shown), None
        token = current_turn.set(f"[app] {message}")
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": status}))
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def assertTicked(self, title, out):
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.DONE, out)
        self.assertIn(f"[x] {title}", self.body("n_studio") + self.body("n_today"), out)

    def assertOpen(self, title, out):
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(title).status, SpanStatus.PLANNED, out)


SHOWN_ALL = f"You're in the 3:50 PM block.\n\nNot marked yet:\n{SHAWL}\n{SHAWL_CALL}\n{BOBBINS}"


class TestAPointerBesideTheName(StudioDay):

    def test_the_name_and_a_pointer_tick_the_item_the_model_read(self):
        for message in ("the saltmarsh shawl is done (hemmed at 2:50), tick it please",
                        "saltmarsh shawl IS done (cut it off at 2:50), mark that one"):
            with self.subTest(message=message):
                self.doCleanups()
                self.setUp()
                self.assertTicked(SHAWL, self.say(message, SHAWL, SHOWN_ALL))

    def test_a_bare_pointer_after_the_list_is_still_refused(self):
        out = self.say("tick it please", BOBBINS, SHOWN_ALL)
        self.assertIn("don't point to", out)
        self.assertOpen(BOBBINS, out)


class TestAllRightSomewhereElse(StudioDay):

    def test_an_ok_in_another_sentence_leaves_the_done_standing(self):
        message = ("hi!! ok the list still has saltmarsh as not marked. "
                   "saltmarsh shawl IS done (hemmed at 2:50), tick that please.")
        self.assertTicked(SHAWL, self.say(message, SHAWL, SHOWN_ALL))

    def test_merely_all_right_or_under_way_with_no_done_is_still_refused(self):
        for message in ("ok the saltmarsh shawl is fine. the bobbins are done", "the saltmarsh shawl is going ok"):
            with self.subTest(message=message):
                out = self.say(message, SHAWL, SHOWN_ALL)
                self.assertOpen(SHAWL, out)


HEDDLES, TOWELS = "Restring the heddles", "Tea towel weft"


class TestDontTickAnotherItem(StudioDay):

    ITEMS = ((TOWELS, "9:20 AM", "40 min", "t_towels"), (HEDDLES, "10:10 AM", "30 min", "t_heddles"),
             (BOBBINS, "11:40 AM", "20 min", "t_bobbins"))
    NOW = "10:50"
    SAID = ("the heddles are done (and I found the lost shuttle lol). the tea towel weft is still going, don't tick "
            "it. off to the bobbins")
    SHOWN = f"Now: {HEDDLES}\n\nNot marked yet:\n{TOWELS}"

    def test_the_item_named_done_is_ticked(self):
        self.assertTicked(HEDDLES, self.say(self.SAID, HEDDLES, self.SHOWN))

    def test_the_item_the_pointer_is_about_stays_open(self):
        self.assertOpen(TOWELS, self.say(self.SAID, TOWELS, self.SHOWN))

    def test_dont_tick_it_in_the_items_own_sentence_is_the_models_to_read(self):
        out = self.say("the heddles are done, but don't tick it yet", HEDDLES, self.SHOWN)
        self.assertTicked(HEDDLES, out)
        self.assertIn("asks not to tick something it doesn't name", out)
        self.assertIn(f"may mean {HEDDLES}", out)


TREADLE, LATE_TREADLE, SWATCH, OILING_LOG = ("Morning treadle oiling", "Evening treadle check", "Selvedge swatch",
                                            "Treadle oiling log")


class TestAnotherItemInAnotherClause(StudioDay):

    ITEMS = ((TREADLE, "11:40 AM", "20 min", "t_treadle"), (SWATCH, "1:10 PM", "30 min", "t_swatch"),
             (OILING_LOG, "2:40 PM", "10 min", "t_log"), (LATE_TREADLE, "8:10 PM", "20 min", "t_late"))
    NOW = "10:50"
    SHOWN = f"Moved along:\n11:40 AM – {TREADLE}\n1:10 PM – {SWATCH}\n8:10 PM – {LATE_TREADLE}"

    def test_the_item_said_done_is_ticked(self):
        out = self.say("Someone shifted my selvedge swatch over to 1:10. Treadle: wrapped up.", TREADLE,
                       self.SHOWN)
        self.assertTicked(TREADLE, out)

    def test_words_that_fit_another_better_and_nothing_of_this_one_beyond_it_are_refused(self):
        out = self.say("the treadle oiling log is done", TREADLE, self.SHOWN)
        self.assertOpen(TREADLE, out)


DUSTING, SHUTTLE, LATE_DUSTING = "Afternoon dusting + skein mordant", "Shuttle oiling + mordant", "Evening dusting"


class TestAListSharingOneDone(StudioDay):

    ITEMS = ((DUSTING, "1:10 PM", "20 min", "t_dusting"), (BOBBINS, "1:40 PM", "20 min", "t_bobbins"),
             (SHUTTLE, "2:10 PM", "1 hour", "t_shuttle"), (LATE_DUSTING, "8:40 PM", "20 min", "t_late"))
    NOW = "14:50"
    LIST = "bobbins and dusting and shuttle oiling + mordant are all wrapped up"
    SHOWN = f"Now: {SHUTTLE}\n\nNot marked yet:\n{DUSTING}\n{BOBBINS}"

    def test_each_name_in_the_list_is_ticked(self):
        for title in (DUSTING, SHUTTLE, BOBBINS):
            with self.subTest(item=title):
                self.assertTicked(title, self.say(self.LIST, title, self.SHOWN))

    def test_a_stretch_saying_something_of_its_own_is_no_entry(self):
        out = self.say("the dusting can wait, shuttle oiling + mordant done", DUSTING, self.SHOWN)
        self.assertOpen(DUSTING, out)

    def test_the_later_copy_of_a_name_is_not_what_was_said(self):
        out = self.say(self.LIST, LATE_DUSTING, self.SHOWN)
        self.assertOpen(LATE_DUSTING, out)


if __name__ == "__main__":
    unittest.main()
