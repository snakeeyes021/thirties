"""A done that names the item and says it's finished goes through, whatever else the message says.

- **A pointer beside the name.** "the saltmarsh shawl is done, tick it" after a reply listing several items: the
  "it" is the shawl the message just named, not one of the items listed. Which of the items its words fit is the
  model's reading. A bare "tick it" after such a list, with no word of any item, is still refused.

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


if __name__ == "__main__":
    unittest.main()
