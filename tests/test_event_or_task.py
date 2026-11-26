"""A one-time event just ends; a task stays open (past_one_offs.py, carry.py rule 3).

Python gathers the facts that tell an event from a task (from a calendar, other people on it, a fixed time, on
a list the user keeps tasks in) and never reads the title; the morning model says which it is with propose_leftovers
``end`` and its ``kind``. Only kind "event" on a one-time item whose own time is over ends. A task, or a missing or
unsure kind, stays not marked yet. An item asked about and never answered is never dropped, filed or ended.

Invented fixtures: a pottery studio (a kiln tour with visitors at 2:40, trimming stoneware bowls at 9:10).
"""

import json
from datetime import datetime, timedelta

from tests.fixtures import DAY, TZ, at
from tests.test_today import TITLES, TodayTestCase
from thirties_core.assistant_tools import Services
from thirties_core.astronomy import set_debug_time
from thirties_core.carry import DROPPED, OpenPool
from thirties_core.lists import ListCatalog
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core.past_one_offs import _span_facts, ends, gather, line
from thirties_core.plan_tools import PlanTools

YESTERDAY = DAY - timedelta(days=1)
TOUR, TRIM, GLAZE = "Kiln tour for the visiting glaze group", "Trim the stoneware bowls", "Mix a celadon test batch"
TOUR_LINE, TRIM_LINE = f"2:40 {TOUR}", f"9:10 {TRIM}"
STUDIO = f"- [ ] {GLAZE}\n- [ ] Wedge the reclaim clay\n"


class StudioCase(TodayTestCase):
    """Today holds two lines with typed clock times from an earlier day; a studio list holds tasks."""

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_studio", "Studio jobs")
        self.api.notes["n_studio"] = {"id": "n_studio", "body": STUDIO}
        ListCatalog(self.sched.state_db).set_role("n_studio", "list", "Studio jobs")
        self.api.notes["n_today"]["body"] = f"- [ ] {TOUR_LINE}\n- [ ] {TRIM_LINE}\n"
        self.services.tasks.extend([
            TaskItem(id="t_glaze", source_notebook="1. Tasks", title=GLAZE, is_checkbox_item=True,
                     parent_note_title="Studio jobs", parent_note_id="n_studio", list_role="list"),
            TaskItem(id="td_tour", source_notebook="1. Tasks", title=TOUR_LINE, is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today"),
            TaskItem(id="td_trim", source_notebook="1. Tasks", title=TRIM_LINE, is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today")])
        self.a = self.t.assistant
        self.db = self.sched.state_db

    def pool(self, title):
        return OpenPool(self.db).all().get(title.lower())

    def record(self):
        return json.loads(self.db.get_sync_meta(self.a._rollover_key()) or "{}")

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped ")), None)

    def propose(self, end):
        self.t.execute("rollover_proposal", {})
        named = {e["item"] for e in end}
        keep = [{"item": k["title"], "when": "this week"} for k in self.record()["leftovers"]["keep"]
                if k["title"] not in named]
        return self.t.execute("propose_leftovers", {"keep": keep, "end": end})


class TheModelSaysWhichItIs(StudioCase):

    def test_a_past_event_ends_not_carried_and_never_done(self):
        out = self.propose([{"item": TOUR_LINE, "kind": "event", "why": "the visitors came and went"}])
        self.assertIn("Ended (its day is over; not carried, not done):", out)
        rec = self.pool(TOUR_LINE)
        self.assertEqual(rec["horizon"], DROPPED)
        self.assertTrue(rec.get("ended"))
        self.assertNotIn(TOUR, self.body("n_today"))  # filed, never ticked
        self.assertNotIn(f"- [x] {TOUR_LINE}", self.body("n_today"))
        self.assertIn(TOUR, self.dropped_note()["body"])
        self.assertEqual(self.db.completed_since(YESTERDAY), set())
        self.assertEqual(self.body("n_studio"), STUDIO)  # the home list is untouched

    def test_a_past_timed_task_stays_open(self):
        out = self.propose([{"item": TRIM_LINE, "kind": "task"}])
        self.assertNotIn("Ended (", out)
        rec = self.pool(TRIM_LINE)
        self.assertNotEqual(rec.get("horizon"), DROPPED)
        self.assertFalse(rec.get("ended"))
        self.assertTrue(rec.get("unknown"))
        self.assertIn(f"- [ ] {TRIM_LINE}", self.body("n_today"))
        self.assertIsNone(self.dropped_note())

    def test_a_missing_or_unsure_kind_stays_unknown(self):
        for kind in (None, "unsure", "maybe"):
            with self.subTest(kind=kind):
                self.setUp()
                end = {"item": TOUR_LINE, **({"kind": kind} if kind else {})}
                out = self.propose([end])
                self.assertIn("Not marked yet (no day", out)
                rec = self.pool(TOUR_LINE)
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertFalse(rec.get("ended"))
                self.assertFalse(rec.get("start"))
                self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))
                self.assertIsNone(self.dropped_note())

    def test_an_event_with_no_time_of_its_own_over_does_not_end(self):
        self.t.execute("rollover_proposal", {})
        data = self.record()
        for k in data["leftovers"]["keep"]:
            if k["title"] == TOUR_LINE:
                k["facts"] = {**(k.get("facts") or {}), "time_by_user": False, "dates": [], "planned_day": ""}
        self.db.set_sync_meta(self.a._rollover_key(), json.dumps(data))
        keep = [{"item": k["title"]} for k in data["leftovers"]["keep"] if k["title"] != TOUR_LINE]
        self.t.execute("propose_leftovers", {"keep": keep, "end": [{"item": TOUR_LINE, "kind": "event"}]})
        self.assertFalse(self.pool(TOUR_LINE).get("ended"))
        self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))

    def test_the_check_is_structure_only(self):
        past = {"time_by_user": True, "planned_day": YESTERDAY.isoformat(), "kind": "once"}
        self.assertTrue(ends(past, DAY, "event"))
        self.assertTrue(ends(past, DAY, " Event "))
        for kind in ("task", "unsure", "", None):
            self.assertFalse(ends(past, DAY, kind))
        self.assertFalse(ends({**past, "kind": "ongoing"}, DAY, "event"))
        self.assertFalse(ends({**past, "kind": "daily"}, DAY, "event"))
        self.assertFalse(ends({**past, "time_by_user": False}, DAY, "event"))

    def test_the_rule_and_the_tool_say_kind(self):
        from thirties_core.assistant_tools import ASSISTANT_TOOL_SPECS
        from thirties_core.past_one_offs import RULE
        spec = next(s for s in ASSISTANT_TOOL_SPECS if s["name"] == "propose_leftovers")
        self.assertIn("kind is required", spec["parameters"]["end"])
        self.assertIn("When unsure, unknown", RULE)
        self.assertIn("never a deadline", RULE)
        self.t.execute("rollover_proposal", {})
        self.assertIn("When unsure, unknown", self.a.leftover_brief())


class TheFactsThatTellThemApart(StudioCase):

    def test_a_list_the_user_keeps_tasks_in(self):
        f = gather(self.a, GLAZE, "t_glaze", DAY)
        self.assertTrue(f["task_list"])
        self.assertIn("on a list the user keeps tasks in", line(f, DAY))
        self.assertFalse(gather(self.a, TRIM_LINE, "td_trim", DAY)["task_list"])  # Today is not such a list

    def test_from_a_calendar_with_someone_else_and_fixed(self):
        span = EventSpan(id="c1", title="Open studio with the glaze group", category=SpanCategory.CALENDAR,
                         start_dt=datetime.combine(YESTERDAY, datetime.min.time(), TZ) + timedelta(hours=14, minutes=40),
                         end_dt=datetime.combine(YESTERDAY, datetime.min.time(), TZ) + timedelta(hours=15, minutes=40),
                         source="calendar", whose="Robin", shared=True)
        f = {"planned_day": "", "home": "", "kind": "once", "offered": 0, "dates": [], **_span_facts(self.a, span)}
        said = line(f, DAY)
        self.assertIn("from a calendar", said)
        self.assertIn("shared with Robin", said)
        self.assertIn("a fixed time", said)
        plain = EventSpan(id="p1", title=TRIM, category=SpanCategory.TASK, start_dt=span.start_dt, end_dt=span.end_dt)
        f = _span_facts(self.a, plain)
        self.assertEqual((f["calendar"], f["others"], f["fixed"]), (False, "", False))


class KeptOnlyAsATask(StudioCase):
    """Keep holds a one-off with a past slot only under kind "task"."""

    def keep_all(self, tour_kind=None):
        self.t.execute("rollover_proposal", {})
        keep = [{"item": k["title"], "when": "tomorrow", "kind": "task"} for k in self.record()["leftovers"]["keep"]
                if k["title"] != TOUR_LINE]
        keep.append({"item": TOUR_LINE, "when": "tomorrow", **({"kind": tour_kind} if tour_kind else {})})
        return self.t.execute("propose_leftovers", {"keep": keep})

    def test_a_past_timed_one_off_kept_with_no_kind_stays_unknown_with_no_day(self):
        for kind in (None, "event", "unsure"):
            with self.subTest(kind=kind):
                self.setUp()
                out = self.keep_all(kind)
                self.assertIn("Not marked yet (no day", out)
                rec = self.pool(TOUR_LINE)
                self.assertTrue(rec.get("unknown"))
                self.assertFalse(rec.get("start"))
                self.assertNotEqual(rec.get("when"), "tomorrow")
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertIn(f"- [ ] {TOUR_LINE}", self.body("n_today"))

    def test_with_kind_task_it_is_kept(self):
        self.keep_all("task")
        rec = self.pool(TOUR_LINE)
        self.assertFalse(rec.get("unknown"))
        self.assertTrue(rec.get("start"))
        self.assertTrue(self.pool(TRIM_LINE).get("start"))  # the other one, kept as a task

    def test_the_check_is_structure_only(self):
        from thirties_core.past_one_offs import keeps
        past = {"time_by_user": True, "planned_day": YESTERDAY.isoformat(), "kind": "once"}
        self.assertTrue(keeps(past, DAY, "task"))
        for kind in ("event", "unsure", "", None):
            self.assertFalse(keeps(past, DAY, kind))
        self.assertTrue(keeps({**past, "kind": "ongoing"}, DAY, None))
        self.assertTrue(keeps({**past, "time_by_user": False}, DAY, None))  # nothing over: kept as before


class NeverVanish(StudioCase):
    """Asked about, never answered: two more mornings go by and the item is still there."""

    def test_two_mornings_with_no_answer(self):
        self.a.start_the_day()
        self.t.execute("rollover_proposal", {})
        first = dict(self.pool(TRIM_LINE))
        self.assertTrue(first.get("unknown"))
        for offset in (1, 2):
            set_debug_time(at("08:10", offset))
            plan, pending = self.sched.build_day_plan(DAY + timedelta(days=offset))
            t = PlanTools(plan, pending, self.sched, services=Services.for_db(self.db, self.services.tasks,
                                                                              self.services.joplin))
            t.assistant.start_the_day()
            t.execute("rollover_proposal", {})
            for title in (TRIM_LINE, TOUR_LINE):
                rec = OpenPool(self.db).all().get(title.lower())
                self.assertIsNotNone(rec, (offset, title))
                self.assertNotEqual(rec.get("horizon"), DROPPED)
                self.assertFalse(rec.get("ended"))
                self.assertFalse(rec.get("pending_drop"))
                self.assertIn(f"- [ ] {title}", self.body("n_today"))
            rec = OpenPool(self.db).all()[TRIM_LINE.lower()]
            for key in ("horizon", "when", "start", "end", "unknown"):
                self.assertEqual(rec.get(key), first.get(key), (offset, key))
        self.assertIsNone(self.dropped_note())
        self.assertEqual(self.body("n_studio"), STUDIO)


if __name__ == "__main__":
    import unittest
    unittest.main()
