"""The DST fall-back hour (Nov 1 2026, 1:00-2:00 happens twice).

Python compares two datetimes in the same zone by wall time and ignores ``fold``, so 1:30 PDT and 1:30 PST
(an hour apart) compare equal. Everything that asks "does this overlap / contain that" must compare instants.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from thirties_core.astronomy import build_base_thirties
from thirties_core.models import DayPlan, EventSpan, SpanCategory

TZ = ZoneInfo("America/Los_Angeles")
LAT, LON = 34.05, -118.24


def first(h: int, m: int) -> datetime:
    """The first (PDT) pass through the repeated hour."""
    return datetime(2026, 11, 1, h, m, tzinfo=TZ)


def second(h: int, m: int) -> datetime:
    """The second (PST) pass, an hour later in real time."""
    return datetime(2026, 11, 1, h, m, fold=1, tzinfo=TZ)


class FallBackHourTests(unittest.TestCase):
    def test_wall_time_comparison_is_the_trap(self):
        # The reason for these tests: same-zone comparison ignores fold.
        self.assertEqual(first(1, 30), second(1, 30))
        self.assertLess(first(1, 30).timestamp(), second(1, 30).timestamp())

    def test_spans_in_different_passes_do_not_overlap(self):
        early = EventSpan("a", "Early", first(1, 0), first(1, 30), SpanCategory.ACTIVITY)
        self.assertFalse(early.overlaps(second(1, 0), second(1, 30)))
        late = EventSpan("b", "Late", second(1, 0), second(1, 30), SpanCategory.ACTIVITY)
        self.assertFalse(late.overlaps(first(1, 0), first(1, 30)))

    def test_span_across_the_change_overlaps_both_passes(self):
        across = EventSpan("c", "Across", first(1, 30), second(1, 30), SpanCategory.ACTIVITY)
        self.assertEqual(across.duration_minutes, 60)
        self.assertTrue(across.overlaps(first(1, 45), first(1, 50)))
        self.assertTrue(across.overlaps(second(1, 0), second(1, 15)))

    def test_covers_uses_the_instant(self):
        early = EventSpan("a", "Early", first(1, 0), first(1, 30), SpanCategory.ACTIVITY)
        self.assertTrue(early.covers(first(1, 10)))
        self.assertFalse(early.covers(second(1, 10)))

    def test_block_at_finds_the_right_pass(self):
        blocks = build_base_thirties(LAT, LON, date(2026, 10, 31), TZ)
        plan = DayPlan(date(2026, 10, 31), blocks, blocks[0].start_dt, blocks[20].start_dt)
        b1 = plan.block_at(first(1, 10))
        b2 = plan.block_at(second(1, 10))
        self.assertIsNotNone(b1)
        self.assertIsNotNone(b2)
        self.assertEqual(b2.index, b1.index + 2)
        self.assertEqual(b2.start_dt.fold, 1)

    def test_spans_overlapping_and_is_open_see_the_second_pass(self):
        blocks = build_base_thirties(LAT, LON, date(2026, 10, 31), TZ)
        plan = DayPlan(date(2026, 10, 31), blocks, blocks[0].start_dt, blocks[20].start_dt)
        plan.spans.append(EventSpan("a", "Early", first(1, 0), first(1, 30), SpanCategory.ACTIVITY))
        late_block = plan.block_at(second(1, 10))
        self.assertTrue(plan.is_open(late_block))
        self.assertFalse(plan.is_open(plan.block_at(first(1, 10))))

    def test_order_follows_real_time(self):
        late = EventSpan("b", "Late", second(1, 0), second(1, 20), SpanCategory.ACTIVITY)
        early = EventSpan("a", "Early", first(1, 10), first(1, 20), SpanCategory.ACTIVITY)
        blocks = build_base_thirties(LAT, LON, date(2026, 10, 31), TZ)
        plan = DayPlan(date(2026, 10, 31), blocks, blocks[0].start_dt, blocks[20].start_dt, spans=[late, early])
        self.assertEqual([s.id for s in plan.spans_in_view()], ["a", "b"])


class PlanToolsFallBackTests(unittest.TestCase):
    def _tools(self, *spans):
        from thirties_core.plan_tools import PlanTools
        blocks = build_base_thirties(LAT, LON, date(2026, 10, 31), TZ)
        plan = DayPlan(date(2026, 10, 31), blocks, blocks[0].start_dt, blocks[20].start_dt, spans=list(spans))
        return PlanTools(plan, [], None)

    def test_clearing_the_second_pass_trims_instead_of_deleting(self):
        across = EventSpan("c", "Across", first(1, 0), second(1, 30), SpanCategory.ACTIVITY)
        tools = self._tools(across)
        out = tools._carve(across, second(1, 0), second(1, 30))
        self.assertTrue(out.startswith("trimmed"), out)
        self.assertEqual(across.end_dt.timestamp(), second(1, 0).timestamp())

    def test_behind_uses_the_instant(self):
        late = EventSpan("b", "Late", second(1, 0), second(1, 20), SpanCategory.ACTIVITY)
        tools = self._tools(late)
        self.assertEqual(tools._behind(first(1, 25)), [])  # an hour before it, though the clock reads later
        self.assertEqual([s.id for s in tools._behind(second(1, 25))], ["b"])


class CanvasAcrossDstTests(unittest.TestCase):
    def _check(self, day: date) -> None:
        blocks = build_base_thirties(LAT, LON, day, TZ)
        self.assertEqual(len(blocks), 48)
        self.assertTrue(all(b.duration_seconds == 1800 for b in blocks))
        self.assertTrue(all(a.end_dt.timestamp() == b.start_dt.timestamp() for a, b in zip(blocks, blocks[1:])))
        self.assertEqual(blocks[-1].end_dt.timestamp() - blocks[0].start_dt.timestamp(), 24 * 3600)

    def test_fall_back_night(self):
        self._check(date(2026, 10, 31))  # the canvas that holds the repeated hour
        self._check(date(2026, 11, 1))

    def test_spring_forward_night(self):
        self._check(date(2026, 3, 7))
        self._check(date(2026, 3, 8))

    def test_utc_spans_compare_with_local_ones(self):
        early = EventSpan("a", "Early", first(1, 0), first(1, 30), SpanCategory.ACTIVITY)
        late_utc = second(1, 0).astimezone(timezone.utc)
        self.assertFalse(early.overlaps(late_utc, late_utc + timedelta(minutes=30)))


if __name__ == "__main__":
    unittest.main()
