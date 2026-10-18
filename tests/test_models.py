"""Unit tests for thirties_core.models."""

import unittest
from datetime import datetime, timedelta

from tests.fixtures import DAY, TZ, at, build_day
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus, TaskItem, ThirtyBlock


class TestThirtyBlock(unittest.TestCase):

    def test_valid_block(self) -> None:
        b = ThirtyBlock(index=0, start_dt=at("07:00"), end_dt=at("07:30"), is_sunlight=False, is_twilight=True)
        self.assertEqual(b.number, 1)
        self.assertEqual(b.duration_seconds, 1800.0)

    def test_invalid_index_and_times(self) -> None:
        with self.assertRaises(ValueError):
            ThirtyBlock(index=48, start_dt=at("07:00"), end_dt=at("07:30"), is_sunlight=True)
        with self.assertRaises(ValueError):
            ThirtyBlock(index=1, start_dt=at("07:30"), end_dt=at("07:00"), is_sunlight=True)

    def test_dict_roundtrip(self) -> None:
        b = ThirtyBlock(index=3, start_dt=at("08:30"), end_dt=at("09:00"), is_sunlight=True)
        self.assertEqual(ThirtyBlock.from_dict(b.to_dict()), b)


class TestEventSpan(unittest.TestCase):

    def test_overlap_and_cover(self) -> None:
        s = EventSpan("x", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY)
        self.assertTrue(s.overlaps(at("17:30"), at("19:00")))
        self.assertFalse(s.overlaps(at("18:00"), at("19:00")))  # touching is not overlapping
        self.assertTrue(s.covers(at("17:00")))
        self.assertFalse(s.covers(at("18:00")))
        self.assertEqual(s.duration_minutes, 60)

    def test_cross_midnight_and_roundtrip(self) -> None:
        s = EventSpan("s", "Sleep", at("23:00"), at("07:00", 1), "sleep", status="done")
        self.assertEqual(s.category, SpanCategory.SLEEP)
        self.assertEqual(s.status, SpanStatus.DONE)
        self.assertTrue(s.is_envelope)
        self.assertEqual(s.duration_minutes, 480)
        self.assertEqual(EventSpan.from_dict(s.to_dict()), s)

    def test_rejects_backwards_span(self) -> None:
        with self.assertRaises(ValueError):
            EventSpan("x", "Bad", at("18:00"), at("17:00"), SpanCategory.ACTIVITY)


class TestDayPlan(unittest.TestCase):

    def test_display_priority_and_budgets(self) -> None:
        _, plan, _ = build_day()
        work = next(s for s in plan.spans if s.category == SpanCategory.WORK)
        gym = EventSpan("g", "Gym", at("17:00"), at("18:00"), SpanCategory.ACTIVITY)
        meeting = EventSpan("m", "Standup", at("09:00"), at("09:30"), SpanCategory.ACTIVITY)
        open_daylight_before = plan.daylight_available_count
        plan.spans += [gym, meeting]

        # An activity inside work is what is shown for that thirty
        self.assertEqual(plan.display_span(plan.block_at(at("09:00"))).title, "Standup")
        self.assertEqual(plan.display_span(plan.block_at(at("10:00"))).id, work.id)
        self.assertIsNone(plan.display_span(plan.block_at(at("16:30"))))
        # Gym took two open daylight thirties (5:00-6:00 PM, sunset ~6:31); the standup sits inside work
        self.assertEqual(plan.daylight_available_count, open_daylight_before - 2)

    def test_tentative_spans_do_not_occupy_time(self) -> None:
        _, plan, _ = build_day()
        plan.spans.append(EventSpan("i", "Invite", at("19:00"), at("20:00"), SpanCategory.CALENDAR,
                                    source="calendar", tentative=True))
        self.assertTrue(plan.is_open(plan.block_at(at("19:00"))))
        self.assertIn("Invite", [s.title for s in plan.spans_in_view()])

    def test_dict_roundtrip(self) -> None:
        _, plan, _ = build_day()
        again = DayPlan.from_dict(plan.to_dict())
        self.assertEqual(again.target_date, DAY)
        self.assertEqual(again.spans, plan.spans)
        self.assertEqual(again.blocks, plan.blocks)


class TestTaskItem(unittest.TestCase):

    def test_roundtrip(self) -> None:
        t = TaskItem(id="t1", source_notebook="1. Tasks", title="Taxes", deferred_count=2, tags=["admin"])
        self.assertEqual(TaskItem.from_dict(t.to_dict()), t)


if __name__ == "__main__":
    unittest.main()


class TestAvailability(unittest.TestCase):

    def test_only_future_thirties_count(self) -> None:
        _, plan, _ = build_day()
        plan.spans.append(EventSpan("a", "Dinner", at("17:00"), at("18:00"), SpanCategory.ACTIVITY))
        whole_day = plan.availability()
        later = plan.availability(at("16:40"))
        # From 4:40 PM the daylight left outside work is 4:30-6:30 PM (4 thirties), two of them planned
        self.assertEqual((later["daylight_total"], later["daylight_open"]), (4, 2))
        self.assertGreater(whole_day["daylight_total"], later["daylight_total"])
        # The 7:00-7:30 AM thirty (before sunrise, so dark) has already passed
        self.assertEqual(later["dark_total"], whole_day["dark_total"] - 1)
