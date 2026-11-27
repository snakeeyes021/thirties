"""An item's put-off count is worked out from what really happened (the user skipping or moving something that was on
a day's plan), never from a running counter: a proposal left out, a leftover the app rolled and a deadline the app moved
on don't count, and devices merge the records, not the larger number. Invented items (a bike co-op's workshop)."""

import json
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from tests.fixtures import DAY, FakeJoplinAPI, at, make_scheduler, temp_dir
from thirties_core.assistant_tools import Services, describe_task
from thirties_core.astronomy import set_debug_time
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.plan_log import model_call, tool_call
from thirties_core.plan_tools import PlanTools
from thirties_core.scheduler import StateDatabase

TASKS = [
    TaskItem(id="t_chains", source_notebook="1. Tasks", title="Re-grease the loaner chains", parent_note_title="Shop"),
    TaskItem(id="t_tubes", source_notebook="1. Tasks", title="Sort the spare tube bin", parent_note_title="Shop"),
]


def count(sched, task_id: str) -> int:
    return sched.state_db.deferred_counts().get(task_id, 0)


class PutOffCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("09:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        plan, pending = self.sched.build_day_plan(DAY)
        joplin = MagicMock()
        joplin.api_client = FakeJoplinAPI({"n_shop": {"id": "n_shop", "body": "# Shop\n- [ ] Sort the spare tube bin\n"}})
        self.services = Services.for_db(self.sched.state_db, list(TASKS), joplin)
        self.t = PlanTools(plan, pending, self.sched, services=self.services)

    def plan_item(self, title="Re-grease the loaner chains", task_id="t_chains", start="10:20 AM", day=None):
        args = {"title": title, "start": start, "duration": "1 hour", "task_id": task_id}
        if day:
            args["day"] = day
        out = self.t.execute("add_event", args)
        self.assertFalse(out.startswith("NOT_DONE"), out)
        plan = self.t.plan_for(day) if day else self.t.day_plan
        return next(s for s in reversed(plan.spans) if s.title == title)

    def user_says(self, message, tool, args):
        """The model's own call of a tool for the user's message."""
        self.t.user_message = message
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            self.t.user_message = None


class TestWhatCounts(PutOffCase):

    def test_a_proposal_left_out_isnt_a_put_off(self):
        with tool_call("propose_day"):  # the morning's proposal puts it on the plan
            span = self.plan_item()
        self.user_says("not doing that one today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(span.status, SpanStatus.SKIPPED)
        self.assertEqual(count(self.sched, "t_chains"), 0)

    def test_a_proposal_the_user_changed_and_then_skipped_is_a_put_off(self):
        with tool_call("propose_day"):
            span = self.plan_item()
        self.t.by_hand = True
        self.t.execute("update_event", {"event": span.id, "start": "11:00 AM"})
        self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(count(self.sched, "t_chains"), 1)

    def test_a_proposal_skipped_after_its_time_is_a_put_off(self):
        with tool_call("propose_day"):
            span = self.plan_item()
        set_debug_time(at("12:40"))  # its time came and went: it was the plan, and it didn't happen
        self.user_says("never got to it", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(count(self.sched, "t_chains"), 1)

    def test_the_users_skip_counts_once(self):
        span = self.plan_item()
        out = self.user_says("skip the chains today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(count(self.sched, "t_chains"), 1)
        self.assertNotIn("put off", out)  # one is no pattern: nothing said
        self.user_says("skip the chains today", "log_outcome", {"event": span.id, "status": "skipped"})
        self.assertEqual(count(self.sched, "t_chains"), 1)  # the same skip said twice is the same put-off

    def test_a_skip_from_the_app_controls_counts_and_the_answer_gives_the_true_number_from_two(self):
        self.t.by_hand = True
        for n in range(2):
            self.sched.state_db.record_put_off("t_chains", "skipped", span_id=f"earlier-{n}", from_day=date.today())
        span = self.plan_item()
        out = self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})
        self.assertIn("put off 3 times", out)

    def test_a_skip_by_the_app_itself_isnt_a_put_off(self):
        span = self.plan_item()
        # the rollover (or any app routine) skipping it, even inside a turn with the model talking
        self.t.user_message = "yes, roll the leftovers"
        with model_call(), tool_call("apply_rollover"):
            self.t.execute("log_outcome", {"event": span.id, "status": "skipped"})
        self.t.user_message = None
        self.assertEqual(count(self.sched, "t_chains"), 0)
        other = self.plan_item("Sort the spare tube bin", "t_tubes", "3:40 PM")
        self.t.execute("log_outcome", {"event": other.id, "status": "skipped"})  # nobody talking, no tap
        self.assertEqual(count(self.sched, "t_tubes"), 0)

    def test_an_item_on_no_plan_isnt_put_off(self):
        self.user_says("skip the tube bin", "log_outcome", {"event": "Sort the spare tube bin", "status": "skipped"})
        self.assertEqual(count(self.sched, "t_tubes"), 0)

    def test_the_users_move_to_a_later_day_counts_and_the_apps_own_move_doesnt(self):
        span = self.plan_item()
        tomorrow = (DAY + timedelta(days=1)).isoformat()
        with tool_call("redraw"):  # the app's own overflow rule moving it off a full day
            self.t._move_to_day(span, tomorrow, "10:20 AM", None, None, None)
        self.assertEqual(count(self.sched, "t_chains"), 0)
        other = self.plan_item("Sort the spare tube bin", "t_tubes", "3:40 PM")
        self.user_says("move the tube bin to the day after tomorrow", "update_event",
                       {"event": other.id, "to_day": (DAY + timedelta(days=2)).isoformat(), "start": "3:40 PM"})
        self.assertEqual(count(self.sched, "t_tubes"), 1)

    def test_a_move_within_the_day_isnt_a_put_off(self):
        span = self.plan_item()
        self.user_says("make the chains 11:40", "update_event", {"event": span.id, "start": "11:40 AM"})
        self.assertEqual(count(self.sched, "t_chains"), 0)


class TestTheCountIsDerived(PutOffCase):

    def test_an_old_inflated_counter_corrects_itself(self):
        db = self.sched.state_db
        db.record_task_allocation("t_chains", DAY)
        with sqlite3.connect(db.db_path) as conn:  # what the old code left behind
            conn.execute("UPDATE task_history SET deferred_count = 6 WHERE task_id = 't_chains'")
        item = TaskItem(id="t_chains", source_notebook="1. Tasks", title="Re-grease the loaner chains")
        self.sched.attach_history_to_tasks([item])
        self.assertEqual(item.deferred_count, 0)
        self.assertEqual(db.get_task_history("t_chains").deferred_count, 0)
        self.assertEqual([r.deferred_count for r in db.get_all_task_histories()], [0])

    def test_put_offs_outside_the_window_stop_counting(self):
        db = self.sched.state_db
        db.record_put_off("t_chains", "skipped", span_id="a", from_day=date.today() - timedelta(days=90))
        db.record_put_off("t_chains", "skipped", span_id="b", from_day=date.today() - timedelta(days=3))
        self.assertEqual(count(self.sched, "t_chains"), 1)

    def test_the_model_is_told_the_true_number_or_nothing_below_two(self):
        item = TaskItem(id="t_chains", source_notebook="1. Tasks", title="Re-grease the loaner chains", deferred_count=1)
        self.assertNotIn("deferred", describe_task(item))
        item.deferred_count = 3
        self.assertIn("deferred 3x", describe_task(item))


class TestDevicesMergeRecords(unittest.TestCase):

    def setUp(self) -> None:
        self.a = StateDatabase(Path(temp_dir("put_offs_a_")) / "a.sqlite")
        self.b = StateDatabase(Path(temp_dir("put_offs_b_")) / "b.sqlite")

    def test_two_devices_with_different_histories_merge_to_the_union(self):
        today = date.today()
        for n in (1, 2, 3):
            self.a.record_put_off("t_chains", "skipped", span_id=f"s{n}", from_day=today)
        self.b.record_put_off("t_chains", "skipped", span_id="s3", from_day=today)  # the same put-off, seen by both
        for n in range(5):
            self.b.record_put_off("t_chains", "moved", span_id=f"m{n}", from_day=today)
        self.a.merge_put_off_records(self.b.put_off_records())
        self.b.merge_put_off_records(self.a.put_off_records())
        self.assertEqual(self.a.deferred_counts()["t_chains"], 8)  # not the larger of 3 and 6
        self.assertEqual(self.b.deferred_counts()["t_chains"], 8)
        self.a.merge_put_off_records(self.b.put_off_records())  # merging again changes nothing
        self.assertEqual(self.a.deferred_counts()["t_chains"], 8)

    def test_another_devices_old_counter_is_never_read(self):
        from thirties_core.config import ThirtiesConfig
        from thirties_core.sync_engine import SYNC_SCHEMA, SyncEngine
        self.a.record_put_off("t_chains", "skipped", span_id="s1", from_day=date.today())
        payload = {"$schema": SYNC_SCHEMA, "device_id": "other", "timestamp": "2026-10-10T15:30:00Z", "version": 2,
                   "analytics": {"task_history": {"t_chains": {
                       "task_id": "t_chains", "first_scheduled_date": "2026-10-01", "last_scheduled_date": "2026-10-09",
                       "deferred_count": 8, "allocated_thirties": 1, "completed_date": None},
                       "t_tubes": {"task_id": "t_tubes", "first_scheduled_date": "2026-10-01",
                                   "last_scheduled_date": "2026-10-09", "deferred_count": 9,
                                   "allocated_thirties": 1, "completed_date": None}}}}
        SyncEngine(config=ThirtiesConfig(), state_db=self.a, joplin_engine=MagicMock()).merge_payload(
            json.loads(json.dumps(payload)))
        self.assertEqual(self.a.deferred_counts()["t_chains"], 1)
        self.assertEqual(self.a.deferred_counts()["t_tubes"], 0)

    def test_put_offs_travel_in_the_payload(self):
        from thirties_core.config import ThirtiesConfig
        from thirties_core.sync_engine import SyncEngine
        self.a.record_put_off("t_chains", "skipped", span_id="s1", from_day=date.today())
        payload = SyncEngine(config=ThirtiesConfig(), state_db=self.a, joplin_engine=MagicMock()).export_sync_payload()
        self.assertEqual([(r["task_id"], r["kind"]) for r in payload["analytics"]["put_offs"]],
                         [("t_chains", "skipped")])


if __name__ == "__main__":
    unittest.main()
