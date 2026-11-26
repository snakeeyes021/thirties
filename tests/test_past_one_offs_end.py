"""A one-time thing whose day is over never rolls into tomorrow by itself (past_one_offs.py, carry.py rule 3).

Python gathers what each leftover carries and the morning model judges: a one-off the user put at a stated time on
a day now over gets no day from Python's first guess (it is "not marked yet", in the one question); the model can
end it (not carried, never ticked done), and an item offered on one morning is never offered or placed again by
itself. Data guard: an ended line that lives only on Today is filed in the month's Dropped note, never ticked; an
ended Today copy of a listed item comes off Today and its home list is untouched.

Invented fixtures: a bike co-op volunteer (stripping donated frames at 2:40, a tyre swap stall at 3:10).
"""

import json
import unittest
from datetime import timedelta

from tests.fixtures import DAY
from tests.test_today import TITLES, TodayTestCase
from thirties_core.carry import DROPPED, OpenPool, mark_asked, settle, sort_leftovers
from thirties_core.lists import ListCatalog
from thirties_core.models import TaskItem
from thirties_core.past_one_offs import gather, line, named_dates, past_time, record_offered, times_offered

YESTERDAY, TOMORROW = DAY - timedelta(days=1), DAY + timedelta(days=1)
FRAMES, STALL, WHEEL = "Strip the donated frames", "Run the tyre swap stall", "Re-spoke the demo wheel"
COOP = f"- [ ] {WHEEL}\n- [ ] Sort the brake pad bin\n"


class CoopCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        TITLES.setdefault("n_coop", "Co-op jobs")
        self.api.notes["n_coop"] = {"id": "n_coop", "body": COOP}
        ListCatalog(self.sched.state_db).set_role("n_coop", "list", "Co-op jobs")
        self.services.tasks.append(TaskItem(id="t_wheel", source_notebook="1. Tasks", title=WHEEL,
                                            is_checkbox_item=True, parent_note_title="Co-op jobs",
                                            parent_note_id="n_coop", list_role="list"))
        self.a = self.t.assistant
        self.db = self.sched.state_db

    def booked_yesterday(self, title, start, said=None, task_id=""):
        """Put on yesterday's plan at ``start``; with ``said``, the user gave that time."""
        self.t.user_message = said
        try:
            self.t.execute("add_event", {"title": title, "start": start, "duration": "30 min",
                                         "day": YESTERDAY.isoformat(), **({"task_id": task_id} if task_id else {})})
        finally:
            self.t.user_message = None

    def pool(self, title):
        return OpenPool(self.db).all()[title.lower()]

    def record(self):
        return json.loads(self.db.get_sync_meta(self.a._rollover_key()) or "{}")

    def dropped_note(self):
        return next((n for n in self.api.notes.values() if str(n.get("title", "")).startswith("Dropped ")), None)


class AtATimeTheUserGave(CoopCase):
    """The failure: two one-offs at times the user gave, unmarked, were said to be set for tomorrow."""

    def setUp(self):
        super().setUp()
        self.booked_yesterday(FRAMES, "2:40 PM", said="frames get stripped at 2:40")
        self.booked_yesterday(STALL, "3:10 PM", said="the tyre swap stall goes at 3:10")

    def test_the_time_is_kept_as_the_users(self):
        span = next(s for s in self.t.plan_for(YESTERDAY.isoformat()).spans if s.title == FRAMES)
        self.assertIsNotNone(span.given_start)

    def test_neither_gets_a_day_by_itself(self):
        out = settle(self.a, DAY)
        self.assertIn(FRAMES, out["pooled"])
        for title in (FRAMES, STALL):
            rec = self.pool(title)
            self.assertFalse(rec.get("start"), rec)
            self.assertTrue(rec.get("unknown"))
            self.assertNotEqual(rec.get("when"), "tomorrow")

    def test_the_morning_asks_once_and_never_says_tomorrow(self):
        text = self.t.execute("rollover_proposal", {})
        self.assertNotIn("tomorrow", text.lower())
        self.assertIn("Not marked yet:", text)
        self.assertIn("Did they happen?", text)
        self.assertEqual(text.count("?"), 1, text)
        self.assertEqual([s.title for s in self.t.day_plan.spans if s.title in (FRAMES, STALL)], [])

    def test_the_model_reads_what_each_one_carries(self):
        self.t.execute("rollover_proposal", {})
        brief = self.a.leftover_brief()
        self.assertIn(f"{FRAMES} (NOT MARKED YET", brief)
        self.assertIn("on yesterday's plan at 2:40 PM (the user's own time)", brief)
        self.assertIn("its slot has passed", brief)
        self.assertIn("lives only on the Today note", brief)
        self.assertIn("never offered before", brief)
        self.assertIn("is moot unless the user said it moved", brief)

    def test_the_model_ends_them_not_done_and_not_carried(self):
        self.t.execute("rollover_proposal", {})
        out = self.t.execute("propose_leftovers", {"keep": [], "end": [{"item": FRAMES}, {"item": STALL}]})
        self.assertIn("Ended (its day is over; not carried, not done):", out)
        self.assertIn("What each one carries:", out)
        for title in (FRAMES, STALL):
            rec = self.pool(title)
            self.assertEqual(rec["horizon"], DROPPED)
            self.assertTrue(rec.get("ended"))
        done = self.db.completed_since(YESTERDAY)
        self.assertEqual(done, set())
        self.assertNotIn(FRAMES.lower(), {k for k in OpenPool(self.db).all(DAY)})  # never offered again
        self.assertNotIn(FRAMES, sort_leftovers(self.a, TOMORROW)["ask"])
        self.assertIn("Ended, their day is over:", self.a.leftover_notice())

    def test_one_the_model_cant_tell_about_stays_unknown(self):
        self.t.execute("rollover_proposal", {})
        self.t.execute("propose_leftovers", {"keep": [{"item": STALL, "when": "saturday"}],
                                             "unknown": [{"item": FRAMES}]})
        self.assertFalse(self.pool(FRAMES).get("start"))
        self.assertIn("Did it happen?", self.a.leftover_notice())

    def test_ended_can_not_be_something_due_today(self):
        self.t.execute("rollover_proposal", {})
        self.services.tasks.append(TaskItem(id="t_frames", source_notebook="1. Tasks", title=FRAMES,
                                            is_checkbox_item=True, parent_note_title="Co-op jobs",
                                            parent_note_id="n_coop", list_role="list", due=DAY.isoformat()))
        data = self.record()
        for k in data["leftovers"]["keep"]:
            if k["title"] == FRAMES:
                k["task_id"], k["due"] = "t_frames", DAY.isoformat()
        self.db.set_sync_meta(self.a._rollover_key(), json.dumps(data))
        out = self.t.execute("propose_leftovers", {"keep": [{"item": STALL}], "end": [{"item": FRAMES}]})
        self.assertIn("due today: keep it today, not ended", out)
        self.assertNotEqual(self.pool(FRAMES).get("horizon"), DROPPED)

    def test_a_task_still_doable_can_not_end_and_stays_unfiled(self):
        self.t.execute("rollover_proposal", {})
        data = self.record()
        for k in data["leftovers"]["keep"]:
            if k["title"] == FRAMES:  # no time the user gave, no date in its name: a plain task, still doable
                k["facts"] = {**(k.get("facts") or {}), "time_by_user": False, "dates": [], "planned_day": ""}
        self.db.set_sync_meta(self.a._rollover_key(), json.dumps(data))
        writes = len(self.api.updates) if hasattr(self, "api") else None
        out = self.t.execute("propose_leftovers", {"keep": [{"item": STALL}], "end": [{"item": FRAMES}]})
        self.assertIn("can't end here: put it in unknown", out)
        self.assertNotEqual(self.pool(FRAMES).get("horizon"), DROPPED)
        self.assertFalse(self.pool(FRAMES).get("filed_key"))
        if writes is not None:
            self.assertEqual(len(self.api.updates), writes)  # nothing filed in Dropped, nothing taken off Today


class TimeThirtiesPicked(CoopCase):
    def test_a_time_nobody_gave_keeps_the_usual_first_guess(self):
        self.booked_yesterday(WHEEL, "2:40 PM", task_id="t_wheel")
        settle(self.a, DAY)
        rec = self.pool(WHEEL)
        self.assertTrue(rec.get("start"))
        self.assertFalse(rec.get("unknown"))
        f = gather(self.a, WHEEL, "t_wheel", DAY)
        self.assertFalse(f["time_by_user"])
        self.assertIn("on yesterday's plan at 3:00 PM (a slot Thirties chose)", "; ".join(line(f, DAY)))
        self.assertIn("home list Co-op jobs", line(f, DAY))


class TodayOnlyLines(CoopCase):
    """A Today line typed with its own time, left from an earlier day."""

    def setUp(self):
        super().setUp()
        self.api.notes["n_today"]["body"] = f"- [ ] 3:10 {STALL}\n- [ ] {WHEEL}\n"
        self.services.tasks.extend([
            TaskItem(id="td_stall", source_notebook="1. Tasks", title=f"3:10 {STALL}", is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today"),
            TaskItem(id="td_wheel", source_notebook="1. Tasks", title=WHEEL, is_checkbox_item=True,
                     parent_note_title="Today To-Do", parent_note_id="n_today", list_role="today")])
        from thirties_core.today import TodayLinks
        TodayLinks(self.db).link(WHEEL, next(t for t in self.services.tasks if t.id == "t_wheel"))

    def test_its_own_time_makes_it_not_marked_yet(self):
        out = sort_leftovers(self.a, DAY)
        item = next(i for i in out["pool_items"] if i["title"] == f"3:10 {STALL}")
        self.assertTrue(item["facts"]["time_by_user"])
        self.assertTrue(past_time(item["facts"], DAY))
        settle(self.a, DAY)
        self.assertFalse(self.pool(f"3:10 {STALL}").get("start"))

    def test_ended_it_is_filed_in_dropped_never_ticked_and_the_home_list_is_untouched(self):
        self.t.execute("rollover_proposal", {})
        keep = [{"item": k["title"], "when": "this week"} for k in self.record()["leftovers"]["keep"]
                if k["title"] != f"3:10 {STALL}"]
        self.t.execute("propose_leftovers", {"keep": keep, "end": [{"item": f"3:10 {STALL}"}]})
        self.assertNotIn(STALL, self.body("n_today"))
        self.assertIn(STALL, self.dropped_note()["body"])
        self.assertNotIn(f"- [x] 3:10 {STALL}", self.body("n_today"))
        self.assertEqual(self.body("n_coop"), COOP)


class NeverTwice(CoopCase):
    """Rule 3's never again: a second morning leaves an earlier offer where it was."""

    def test_an_item_offered_before_is_not_placed_again(self):
        self.booked_yesterday(WHEEL, "2:40 PM", task_id="t_wheel")
        before = YESTERDAY - timedelta(days=1)
        mark_asked(self.db, [WHEEL], before)
        record_offered(self.db, [WHEEL], before)
        OpenPool(self.db).add([{"title": WHEEL, "task_id": "t_wheel", "horizon": "week", "when": "friday",
                                "start": DAY.isoformat(), "end": DAY.isoformat()}], before)
        out = settle(self.a, DAY)
        self.assertNotIn(WHEEL, out["ask"])
        self.assertIn(WHEEL, out["offered_before"])
        self.assertEqual(self.pool(WHEEL)["start"], DAY.isoformat())  # where it was: not given a new day
        self.assertEqual(times_offered(self.db, WHEEL, DAY), 1)
        self.assertNotIn(WHEEL, self.t.execute("rollover_proposal", {}))


class Dates(unittest.TestCase):
    def test_the_dates_a_name_gives(self):
        self.assertEqual(named_dates("Swap meet table, Oct 14", DAY), ["2026-10-14"])
        self.assertEqual(named_dates("Tune-up clinic 10/22", DAY), ["2026-10-22"])
        self.assertEqual(named_dates("Grease the chains on friday", DAY), [])
        self.assertTrue(past_time({"dates": ["2026-10-14"]}, DAY))
        self.assertFalse(past_time({"dates": ["2026-10-22"]}, DAY))

    def test_facts_line_counts_mornings(self):
        said = line({"planned_day": "", "home": "", "kind": "once", "offered": 2, "dates": []}, DAY)
        self.assertEqual(said, ["lives only on the Today note", "kind once", "offered 2 mornings before"])


if __name__ == "__main__":
    unittest.main()
