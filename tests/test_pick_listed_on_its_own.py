"""A bare done after a list is the model's to read; an item the list showed on its own is one of its picks.

- **Inside a longer title isn't listed.** A short title ("Squeegees") whose word sits inside a longer one the reply
  listed ("Degrease the squeegees") wasn't shown as an entry; a bare "done!" the model reads for it still goes on.
- **On its own, it stands.** The same short title listed as an entry of its own is the model's to pick.
- **A list line counts as it reads.** A line with its start time in front ("- 9:55 AM – Flood the platen") lists
  the item.

Through the real tool path on the mock Joplin API (no real calls). Fixtures are invented: a screen-printing shop's
day.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

DEGREASE, SQUEEGEES, PLATEN, RACKS = ("Degrease the squeegees", "Squeegees", "Flood the platen", "Stack the drying racks")
ITEMS = ((DEGREASE, "8:45 AM", "t_degrease"), (SQUEEGEES, "9:15 AM", "t_squeegees"), (PLATEN, "9:55 AM", "t_platen"),
         (RACKS, "10:25 AM", "t_racks"))
BARE = ("done!", "that one's sorted")


class ShopDay(TodayTestCase):
    """Four shop items from one list, each on the plan."""

    def setUp(self):
        super().setUp()
        TITLES["n_shop"] = "Shop floor"
        self.addCleanup(TITLES.pop, "n_shop", None)
        self.api.notes["n_shop"] = {"id": "n_shop", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_shop", "list", "Shop floor")
        self.services.tasks[:] = [item(tid, title, "n_shop") for title, _, tid in ITEMS]
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


class TestListedOnItsOwn(ShopDay):

    def test_a_short_title_inside_a_longer_one_is_the_models_reading(self):
        shown = f"Still open: {DEGREASE}, {PLATEN} and {RACKS}."
        for message in BARE:
            with self.subTest(message=message):
                self.fresh()
                self.t.picked_dones = set()
                out = self.say(message, SQUEEGEES, shown)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(SQUEEGEES).status, SpanStatus.DONE, out)
                self.assertEqual(self.t.picked_dones, set())  # not one of the items the reply listed

    def test_inside_a_longer_list_line_too(self):
        shown = f"Still open:\n- 8:45 AM – {DEGREASE}\n- 9:55 AM – {PLATEN}\n- 10:25 AM – {RACKS}"
        self.t.picked_dones = set()
        out = self.say("done!", SQUEEGEES, shown)
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.t.picked_dones, set())

    def test_the_short_title_listed_on_its_own_stands(self):
        for shown in (f"Still open: {SQUEEGEES}, {PLATEN} and {RACKS}.", f"Still open: {PLATEN}, {RACKS} or {SQUEEGEES}"):
            for message in BARE:
                with self.subTest(shown=shown, message=message):
                    self.fresh()
                    out = self.say(message, SQUEEGEES, shown)
                    self.assertFalse(out.startswith(NOT_DONE), out)
                    self.assertEqual(self.span(SQUEEGEES).status, SpanStatus.DONE, out)

    def test_a_list_line_with_its_start_time_in_front(self):
        shown = f"Still open:\n- 9:15 AM – {SQUEEGEES}\n- 9:55 AM – {PLATEN}\n- 10:25 AM – {RACKS}"
        for title in (SQUEEGEES, PLATEN, RACKS):
            with self.subTest(item=title):
                self.fresh()
                out = self.say("done!", title, shown)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(title).status, SpanStatus.DONE, out)


if __name__ == "__main__":
    unittest.main()
