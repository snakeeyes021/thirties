"""Unit tests for thirties_core.scheduler: the span store and the day builder."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.fixtures import DAY, at, build_day, event, make_scheduler, occupant
from thirties_core.models import EventSpan, SpanCategory, TaskItem
from thirties_core.scheduler import ROUTINE_TIMESTAMP, DayMeta, StateDatabase


class TestStateDatabase(unittest.TestCase):

    def setUp(self) -> None:
        self.db = StateDatabase(Path(tempfile.mkdtemp()) / "state.sqlite")

    def test_task_history_and_decomposition(self) -> None:
        self.db.record_task_allocation("t1", DAY, count=2)
        rec = self.db.get_task_history("t1")
        self.assertEqual((rec.allocated_thirties, rec.deferred_count), (2, 0))
        for expected in (1, 2):
            self.assertEqual(self.db.record_task_deferred("t1"), expected)
        self.assertFalse(self.db.get_task_history("t1").needs_decomposition)
        self.assertEqual(self.db.record_task_deferred("t1"), 3)
        self.assertTrue(self.db.get_task_history("t1").needs_decomposition)

    def test_span_store_queries_and_soft_delete(self) -> None:
        sleep = EventSpan("s", "Sleep", at("23:00"), at("07:00", 1), SpanCategory.SLEEP)
        gym = EventSpan("g", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY)
        self.db.upsert_span(sleep)
        self.db.upsert_span(gym)
        # A span crossing midnight is found from either calendar date
        self.assertEqual([s.id for s in self.db.get_spans_between(at("00:00", 1), at("06:00", 1))], ["s"])
        self.assertEqual([s.id for s in self.db.get_spans_between(at("16:00"), at("23:30"))], ["g", "s"])
        self.db.delete_span("g")
        self.assertEqual([s.id for s in self.db.get_spans_between(at("16:00"), at("18:00"))], [])
        self.assertTrue(next(r for r in self.db.get_span_records() if r["id"] == "g")["deleted"])

    def test_span_merge_is_last_write_wins(self) -> None:
        gym = EventSpan("g", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY)
        self.db.upsert_span(gym, updated_at="2026-10-15T12:00:00+00:00")
        older = {"id": "g", "data": {**gym.to_dict(), "title": "Old"}, "updated_at": "2026-10-15T11:00:00+00:00", "deleted": False}
        newer = {"id": "g", "data": {**gym.to_dict(), "title": "New"}, "updated_at": "2026-10-15T13:00:00+00:00", "deleted": False}
        self.assertFalse(self.db.merge_span_record(older))
        self.assertEqual(self.db.get_spans_between(at("17:00"), at("18:00"))[0].title, "Gym")
        self.assertTrue(self.db.merge_span_record(newer))
        self.assertEqual(self.db.get_spans_between(at("17:00"), at("18:00"))[0].title, "New")

    def test_day_meta_and_decisions(self) -> None:
        self.assertFalse(self.db.get_day_meta(DAY).is_finalized)
        self.db.save_day_meta(DayMeta(DAY.isoformat(), is_finalized=True, notes="good day"))
        self.assertEqual(self.db.get_day_meta(DAY).notes, "good day")
        self.db.set_event_decision("e1", "attend")
        self.assertEqual(self.db.get_event_decisions(), {"e1": "attend"})

    def test_legacy_snapshots_migrate_once(self) -> None:
        legacy = _legacy_plan_dict()
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO day_snapshots VALUES (?, ?, 1)", (DAY.isoformat(), json.dumps(legacy)))
        self.db.set_sync_meta("legacy_snapshots_migrated", "0")
        self.assertEqual(self.db.migrate_legacy_snapshots(), 1)
        self.assertEqual(self.db.migrate_legacy_snapshots(), 0)

        spans = {s.title: s for s in self.db.get_spans_between(at("07:00"), at("07:00", 1))}
        self.assertEqual((spans["Work"].start_dt, spans["Work"].end_dt), (at("08:30"), at("16:00")))
        # Old after-midnight blocks carried the wrong date; sleep now runs into the next morning
        self.assertEqual((spans["Sleep"].start_dt, spans["Sleep"].end_dt), (at("23:00"), at("07:00", 1)))
        self.assertEqual((spans["Composing"].start_dt, spans["Composing"].end_dt), (at("17:00"), at("18:00")))
        self.assertEqual(self.db.get_event_decisions(), {"dentist": "attend"})
        self.assertTrue(self.db.get_day_meta(DAY).is_finalized)


def _legacy_plan_dict() -> dict:
    """A pre-span plan: midnight-to-midnight blocks with kinds and labels."""
    blocks = []
    for i in range(48):
        start = at("00:00") + timedelta(minutes=30 * i)
        kind, label, event_id = "daylight_discretionary" if 15 <= i <= 36 else "dark_discretionary", "", None
        if i >= 46 or i < 14:
            kind, label = "sleep", "Sleep"
        elif 17 <= i <= 31:
            kind, label = "work", "Work"
        if i in (34, 35):
            label = "Composing"
        if i == 30:
            kind, label, event_id = "busy_calendar", "Dentist", "dentist"
        blocks.append({"index": i, "start_dt": start.isoformat(), "end_dt": (start + timedelta(minutes=30)).isoformat(),
                       "is_sunlight": 15 <= i <= 36, "kind": kind, "label": label, "source_event_id": event_id})
    return {"target_date": DAY.isoformat(), "blocks": blocks, "sunrise": at("07:17").isoformat(),
            "sunset": at("18:31").isoformat(), "is_finalized": True, "notes": ""}


class TestDeterministicScheduler(unittest.TestCase):

    def test_default_routines(self) -> None:
        _, plan, pending = build_day()
        self.assertEqual(pending, [])
        self.assertEqual(plan.day_start, at("07:00"))
        self.assertEqual(plan.day_end, at("07:00", 1))
        self.assertEqual(occupant(plan, "08:30"), "Work")
        self.assertEqual(occupant(plan, "15:30"), "Work")
        self.assertIsNone(occupant(plan, "16:00"))
        self.assertEqual(occupant(plan, "23:00"), "Sleep")
        self.assertEqual(occupant(plan, "06:30", 1), "Sleep")
        self.assertIsNone(occupant(plan, "07:00"))

    def test_routines_are_materialized_once_and_edits_stick(self) -> None:
        sched, plan, _ = build_day()
        work = next(s for s in plan.spans if s.category == SpanCategory.WORK)
        self.assertEqual(next(r for r in sched.state_db.get_span_records() if r["id"] == work.id)["updated_at"],
                         ROUTINE_TIMESTAMP)
        sched.state_db.delete_span(work.id)  # day off
        plan2, _ = sched.build_day_plan(DAY)
        self.assertNotIn(SpanCategory.WORK, [s.category for s in plan2.spans])

    def test_routine_ids_are_deterministic_across_devices(self) -> None:
        _, plan_a, _ = build_day()
        _, plan_b, _ = build_day()
        self.assertEqual(sorted(s.id for s in plan_a.spans), sorted(s.id for s in plan_b.spans))

    def test_calendar_spans_are_fresh_and_decisions_persist(self) -> None:
        sched = make_scheduler([event("dentist", "15:00", "16:00", "Dentist"),
                                event("dinner", "19:00", "21:00", "Team dinner", invite=True)])
        plan, pending = sched.build_day_plan(DAY)
        self.assertEqual([e.id for e in pending], ["dinner"])
        self.assertEqual(occupant(plan, "15:00"), "Dentist")
        self.assertIsNone(occupant(plan, "19:00"))  # unconfirmed invites don't occupy time
        sched.state_db.set_event_decision("dinner", "attend")
        plan2, pending2 = sched.build_day_plan(DAY)
        self.assertEqual(pending2, [])
        self.assertEqual(occupant(plan2, "19:00"), "Team dinner")

    def test_late_night_calendar_event_on_next_date_is_included(self) -> None:
        late = event("flight", "01:00", "02:00", "Red-eye")
        late.start_dt += timedelta(days=1)
        late.end_dt += timedelta(days=1)
        sched = make_scheduler([late])
        plan, _ = sched.build_day_plan(DAY)
        self.assertEqual(occupant(plan, "01:00", 1), "Red-eye")

    def test_finalize_records_task_allocation(self) -> None:
        sched, plan, _ = build_day()
        plan.spans.append(EventSpan("t", "Taxes", at("17:00"), at("18:00"), SpanCategory.TASK, task_id="t_taxes"))
        sched.finalize_day_plan(plan, notes="locked")
        self.assertTrue(sched.state_db.get_day_meta(DAY).is_finalized)
        self.assertEqual(sched.state_db.get_task_history("t_taxes").allocated_thirties, 2)
        self.assertTrue(sched.build_day_plan(DAY)[0].is_finalized)

    def test_rolling_schedule_and_task_history(self) -> None:
        sched = make_scheduler()
        plans = sched.build_rolling_schedule(DAY, days=3)
        self.assertEqual([p.target_date for p in plans], [DAY + timedelta(days=i) for i in range(3)])
        sched.state_db.record_task_deferred("t1")
        tasks = [TaskItem(id="t1", source_notebook="1. Tasks", title="Taxes")]
        sched.attach_history_to_tasks(tasks)
        self.assertEqual(tasks[0].deferred_count, 1)


if __name__ == "__main__":
    unittest.main()
