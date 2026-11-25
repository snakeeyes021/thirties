""""Every 9:10 thing on the bench is wrapped up" ticks every item planned to start in the thirty it names.

A bulk word followed by an hour picks out the items by their planned start. An item whose title starts another item's
title ("Mix the grout" beside a later "Mix the grout for the pavers") is one of them like any other, and the later
item is never its rival. A done the model reads for an item planned at another time, or with nothing planned in the
thirty the words name, goes on as its reading, and its result says which thirty the words name.

Invented: someone setting smalti and pavers, with two short grout jobs in the morning."""

import unittest
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.done_reference import BULK_AT_RE
from thirties_core.lists import ListCatalog
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_tools import NOT_DONE, PlanTools

PREP = ["Grease the nippers", "Mix the grout"]
SPONGES, LATER = "Rinse the sponges", "Mix the grout for the pavers"
TITLES = {"n_today": "Today To-Do", "n_bench": "Grout bench", "n_yard": "Yard jobs"}
SHOWN = (f"Still to do at 9:10: Grease the nippers, then Mix the grout. Later: {SPONGES} (10:40) and {LATER} "
         "(3:40 PM).")


class GroutCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:50"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.api = FakeJoplinAPI({
            "n_today": {"id": "n_today", "body": ""},
            "n_bench": {"id": "n_bench", "body": "".join(f"- [ ] {s}\n" for s in PREP)},
            "n_yard": {"id": "n_yard", "body": f"- [ ] {SPONGES}\n- [ ] {LATER}\n"},
        })
        joplin = MagicMock()
        joplin.api_client = self.api
        joplin.config.resolve_joplin_db_path.return_value = None
        joplin.fetch_active_notes.side_effect = lambda: [
            {"id": nid, "title": TITLES[nid], "source_notebook": "1. Tasks", "body": n["body"]}
            for nid, n in self.api.notes.items()]
        catalog = ListCatalog(self.sched.state_db)
        for nid, role in (("n_today", "today"), ("n_bench", "list"), ("n_yard", "list")):
            catalog.set_role(nid, role, TITLES[nid])
        items = [TaskItem(id=tid, source_notebook="1. Tasks", title=title, is_checkbox_item=True,
                          parent_note_title=TITLES[nid], parent_note_id=nid)
                 for tid, title, nid in (("t_grease", PREP[0], "n_bench"), ("t_mix", PREP[1], "n_bench"),
                                         ("t_sponges", SPONGES, "n_yard"), ("t_later", LATER, "n_yard"))]
        self.services = Services.for_db(self.sched.state_db, catalog.apply(items), joplin)
        plan, pending = self.sched.build_day_plan(DAY)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)
        for tid, title, start in (("t_grease", PREP[0], "9:10 AM"), ("t_mix", PREP[1], "9:20 AM"),
                                  ("t_sponges", SPONGES, "10:40 AM"), ("t_later", LATER, "3:40 PM")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": "10 minutes",
                                               "task_id": tid, "category": "task"})
            self.assertFalse(out.startswith(NOT_DONE), out)
        for title in PREP:  # both start in the 9:00 thirty
            self.assertEqual((self.span(title).start_dt.hour, self.span(title).start_dt.minute // 30), (9, 0), title)

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def done(self, message, title, shown=SHOWN):
        self.t.user_message, self.t.prior_exchange = message, ("", shown)
        try:
            return self.t.execute("log_outcome", {"event": self.span(title).id, "status": "done"})
        finally:
            self.t.user_message, self.t.prior_exchange = None, None


class TestAllAtATime(GroutCase):

    def test_every_item_at_that_time_is_ticked_with_one_whose_title_starts_a_later_one(self):
        for title in PREP:
            out = self.done("every 9:10 thing on the bench is wrapped up", title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertEqual(self.span(title).status, SpanStatus.DONE, title)
        self.assertEqual(self.span(LATER).status, SpanStatus.PLANNED)

    def test_said_another_way_too(self):
        for title in PREP:
            out = self.done("all of my 9:10 am jobs are done", title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")

    def test_an_item_at_another_time_carries_a_note(self):
        for title in (SPONGES, LATER):
            out = self.done("every 9:10 thing on the bench is wrapped up", title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertIn(f"names the items planned at 9:00 AM, and {title} isn't planned then", out)

    def test_with_nothing_planned_at_that_time_it_carries_a_note(self):
        for title in PREP + [SPONGES]:
            out = self.done("every 8:40 thing on the bench is wrapped up", title)
            self.assertFalse(out.startswith(NOT_DONE), f"{title}: {out}")
            self.assertIn("8:30 AM", out)

    def test_a_time_it_was_done_at_is_not_which_items(self):
        self.assertIsNone(BULK_AT_RE.search("did it all at 10:40"))
        self.assertIsNone(BULK_AT_RE.search("all 3 done"))
        self.assertIsNotNone(BULK_AT_RE.search("every 9:10 thing on the bench is wrapped up"))

    def test_a_count_is_not_a_time(self):
        shown = f"Still to do: Grease the nippers, Mix the grout, {SPONGES}."
        out = self.done("all 3 done", SPONGES, shown=shown)  # a count of the items just shown, not 3 o'clock
        self.assertFalse(out.startswith(NOT_DONE), out)


if __name__ == "__main__":
    unittest.main()
