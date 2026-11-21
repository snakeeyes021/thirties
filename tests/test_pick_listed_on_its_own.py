"""A bare done after a list ticks only an item that list showed on its own.

- **Inside a longer title isn't listed.** A short title ("Costumes") whose word sits inside a longer one the reply
  listed ("Steam the costumes") wasn't shown, so a bare "done!" never ticks it.
- **On its own, it stands.** The same short title listed as an entry of its own is the model's to pick.
- **A list line counts as it reads.** A line with its start time in front ("- 9:55 AM – Rig the follow spot") lists
  the item.

Through the real tool path on the mock Joplin API (no real calls). Fixtures are invented: a theatre crew's day.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TITLES, TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE

STEAM, COSTUMES, SPOT, MARKS = ("Steam the costumes", "Costumes", "Rig the follow spot", "Tape the stage marks")
ITEMS = ((STEAM, "8:45 AM", "t_steam"), (COSTUMES, "9:15 AM", "t_costumes"), (SPOT, "9:55 AM", "t_spot"),
         (MARKS, "10:25 AM", "t_marks"))
BARE = ("done!", "that one's sorted")


class CrewDay(TodayTestCase):
    """Four crew items from one list, each on the plan."""

    def setUp(self):
        super().setUp()
        TITLES["n_crew"] = "Crew call"
        self.addCleanup(TITLES.pop, "n_crew", None)
        self.api.notes["n_crew"] = {"id": "n_crew", "body": "".join(f"- [ ] {t}\n" for t, _, _ in ITEMS)}
        ListCatalog(self.sched.state_db).set_role("n_crew", "list", "Crew call")
        self.services.tasks[:] = [item(tid, title, "n_crew") for title, _, tid in ITEMS]
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


class TestListedOnItsOwn(CrewDay):

    def test_a_short_title_inside_a_longer_one_is_refused(self):
        shown = f"Still open: {STEAM}, {SPOT} and {MARKS}."
        for message in BARE:
            with self.subTest(message=message):
                self.fresh()
                out = self.say(message, COSTUMES, shown)
                self.assertTrue(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(COSTUMES).status, SpanStatus.PLANNED, out)

    def test_inside_a_longer_list_line_too(self):
        shown = f"Still open:\n- 8:45 AM – {STEAM}\n- 9:55 AM – {SPOT}\n- 10:25 AM – {MARKS}"
        out = self.say("done!", COSTUMES, shown)
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(COSTUMES).status, SpanStatus.PLANNED, out)

    def test_the_short_title_listed_on_its_own_stands(self):
        for shown in (f"Still open: {COSTUMES}, {SPOT} and {MARKS}.", f"Still open: {SPOT}, {MARKS} or {COSTUMES}"):
            for message in BARE:
                with self.subTest(shown=shown, message=message):
                    self.fresh()
                    out = self.say(message, COSTUMES, shown)
                    self.assertFalse(out.startswith(NOT_DONE), out)
                    self.assertEqual(self.span(COSTUMES).status, SpanStatus.DONE, out)

    def test_a_list_line_with_its_start_time_in_front(self):
        shown = f"Still open:\n- 9:15 AM – {COSTUMES}\n- 9:55 AM – {SPOT}\n- 10:25 AM – {MARKS}"
        for title in (COSTUMES, SPOT, MARKS):
            with self.subTest(item=title):
                self.fresh()
                out = self.say("done!", title, shown)
                self.assertFalse(out.startswith(NOT_DONE), out)
                self.assertEqual(self.span(title).status, SpanStatus.DONE, out)


if __name__ == "__main__":
    unittest.main()
