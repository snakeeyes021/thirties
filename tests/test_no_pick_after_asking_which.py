"""After the assistant asked which item, a bare done picks nothing.

- **A question listing them isn't a list to pick from.** "Which did you finish: A, B or C?", or any reply listing
  several open items with a question in it: a bare "done!" or "that one's sorted" ticks none of them, and the note
  tells the model to ask again which one, in one short question.
- **A done that names the item still stands.** "the atlases are reshelved" after the same question ticks that item.

Through the real tool path on the mock Joplin API (no real calls). Fixtures are invented: a library's day.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

ATLASES, LOANS, FOLIOS, CART = ("Reshelve the atlases", "Process the inter-library loans", "Mend the map folios",
                                "Empty the book drop")
ITEMS = ((ATLASES, "8:45 AM", "t_atlases"), (LOANS, "9:15 AM", "t_loans"), (FOLIOS, "9:55 AM", "t_folios"),
         (CART, "10:25 AM", "t_cart"))
LISTED = (ATLASES, LOANS, FOLIOS)
QUESTIONS = (f"Which did you finish: {ATLASES}, {LOANS} or {FOLIOS}?",
             f"Still open: {ATLASES}, {LOANS} and {FOLIOS}. Want me to move any of them?",
             f"Which one is done?\n- 8:45 AM – {ATLASES}\n- 9:15 AM – {LOANS}\n- 9:55 AM – {FOLIOS}")
BARE = ("done!", "that one's sorted", "finished it")


class LibraryDay(TodayTestCase):
    """Four library items from one list, each on the plan."""

    def setUp(self):
        super().setUp()
        TITLES["n_desk"] = "Front desk"
        self.addCleanup(TITLES.pop, "n_desk", None)
        self.api.notes["n_desk"] = {"id": "n_desk", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_desk", "list", "Front desk")
        self.services.tasks[:] = [item(tid, title, "n_desk") for title, _, tid in ITEMS]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, tid in ITEMS:
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "20 min", "task_id": tid})
            self.assertFalse(str(out).startswith(NOT_DONE), out)
        set_debug_time(at("10:50"))

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def say(self, message, title, shown):
        self.t.user_message, self.t.prior_exchange, self.t.done_asks = message, ("", shown), None
        token = current_turn.set(f"[app] {message}")
        try:
            return str(self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"}))
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def fresh(self):
        self.doCleanups()
        self.setUp()


class TestAfterAskingWhich(LibraryDay):

    def test_a_bare_done_picks_none_of_them(self):
        for shown in QUESTIONS:
            for message in BARE:
                for title in LISTED:
                    with self.subTest(shown=shown, message=message, item=title):
                        self.fresh()
                        out = self.say(message, title, shown)
                        self.assertTrue(out.startswith(NOT_DONE), out)
                        self.assertIn("Ask again which one, in one short question", out)
                        for each in LISTED:
                            self.assertEqual(self.span(each).status, SpanStatus.PLANNED, out)

    def test_a_done_naming_the_item_still_stands(self):
        for shown in QUESTIONS:
            with self.subTest(shown=shown):
                self.fresh()
                out = self.say("the atlases are reshelved", ATLASES, shown)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(ATLASES).status, SpanStatus.DONE, out)

    def test_the_same_list_without_a_question_is_still_picked_from(self):
        out = self.say("done!", LOANS, f"Still open: {ATLASES}, {LOANS} and {FOLIOS}.")
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(LOANS).status, SpanStatus.DONE, out)


if __name__ == "__main__":
    unittest.main()
