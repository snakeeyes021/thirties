"""Unit tests for thirties_core.models."""

import unittest
from datetime import date, datetime, timedelta, timezone

from thirties_core.models import BlockKind, DayPlan, TaskItem, ThirtyBlock


class TestModels(unittest.TestCase):

    def setUp(self) -> None:
        self.tz = timezone.utc
        self.now = datetime(2026, 10, 8, 8, 0, tzinfo=self.tz)
        self.end = self.now + timedelta(minutes=30)

    def test_thirty_block_valid(self) -> None:
        block = ThirtyBlock(
            index=16,
            start_dt=self.now,
            end_dt=self.end,
            is_sunlight=True,
            kind=BlockKind.DAYLIGHT_DISCRETIONARY,
            label="Focus Time",
        )
        self.assertEqual(block.index, 16)
        self.assertEqual(block.duration_seconds, 1800.0)
        self.assertTrue(block.is_sunlight)
        self.assertTrue(block.is_discretionary)
        self.assertEqual(block.kind, BlockKind.DAYLIGHT_DISCRETIONARY)

    def test_thirty_block_invalid_index(self) -> None:
        with self.assertRaises(ValueError):
            ThirtyBlock(
                index=-1,
                start_dt=self.now,
                end_dt=self.end,
                is_sunlight=False,
                kind=BlockKind.DARK_DISCRETIONARY,
            )
        with self.assertRaises(ValueError):
            ThirtyBlock(
                index=48,
                start_dt=self.now,
                end_dt=self.end,
                is_sunlight=False,
                kind=BlockKind.DARK_DISCRETIONARY,
            )

    def test_thirty_block_invalid_dates(self) -> None:
        with self.assertRaises(ValueError):
            ThirtyBlock(
                index=0,
                start_dt=self.end,
                end_dt=self.now,  # start after end
                is_sunlight=False,
                kind=BlockKind.SLEEP,
            )

    def test_thirty_block_dict_roundtrip(self) -> None:
        original = ThirtyBlock(
            index=20,
            start_dt=self.now,
            end_dt=self.end,
            is_sunlight=True,
            is_twilight=False,
            kind=BlockKind.ASSIGNED,
            assigned_task_id="task-123",
            label="Dorico Compose",
            is_locked=True,
            source_event_id="cal-abc",
        )
        data = original.to_dict()
        restored = ThirtyBlock.from_dict(data)
        self.assertEqual(original, restored)

    def test_task_item(self) -> None:
        task = TaskItem(
            id="note-abc",
            source_notebook="3. Creative",
            title="Compose bridge in Dorico",
            description="Work on 8 bars of development",
            is_checkbox_item=True,
            parent_note_title="Composition Sketchbook",
            estimated_thirties=2,
            deferred_count=1,
            tags=["music", "creative"],
        )
        self.assertEqual(task.estimated_thirties, 2)
        self.assertEqual(task.deferred_count, 1)
        self.assertTrue(task.is_checkbox_item)

        data = task.to_dict()
        restored = TaskItem.from_dict(data)
        self.assertEqual(task, restored)

    def test_day_plan_counts_and_lookup(self) -> None:
        base_time = datetime(2026, 10, 8, 0, 0, tzinfo=self.tz)
        blocks = []
        for i in range(48):
            b_start = base_time + timedelta(minutes=30 * i)
            b_end = b_start + timedelta(minutes=30)
            is_sun = 14 <= i <= 34  # arbitrary mock daytime
            if i < 14:
                kind = BlockKind.SLEEP if i < 14 else BlockKind.DARK_DISCRETIONARY
            elif 14 <= i < 17:
                kind = BlockKind.DAYLIGHT_DISCRETIONARY
            elif 17 <= i <= 31:
                kind = BlockKind.WORK
            elif 32 <= i <= 34:
                kind = BlockKind.DAYLIGHT_DISCRETIONARY
            elif 35 <= i <= 45:
                kind = BlockKind.DARK_DISCRETIONARY
            else:
                kind = BlockKind.SLEEP
            blocks.append(ThirtyBlock(
                index=i,
                start_dt=b_start,
                end_dt=b_end,
                is_sunlight=is_sun,
                kind=kind,
            ))

        plan = DayPlan(
            target_date=date(2026, 10, 8),
            blocks=blocks,
            sunrise=base_time + timedelta(hours=7),
            sunset=base_time + timedelta(hours=18),
        )
        plan.recalculate_counts()

        # Daylight discretionary: 14, 15, 16 (3 blocks) + 32, 33, 34 (3 blocks) = 6 blocks
        self.assertEqual(plan.daylight_available_count, 6)
        # Dark discretionary: 35 through 45 = 11 blocks
        self.assertEqual(plan.dark_available_count, 11)

        self.assertEqual(plan.get_block(0).kind, BlockKind.SLEEP)
        self.assertEqual(plan.get_block(20).kind, BlockKind.WORK)
        self.assertEqual(plan.get_block(32).kind, BlockKind.DAYLIGHT_DISCRETIONARY)

        # Roundtrip serialization
        plan_dict = plan.to_dict()
        restored_plan = DayPlan.from_dict(plan_dict)
        self.assertEqual(plan.target_date, restored_plan.target_date)
        self.assertEqual(plan.daylight_available_count, restored_plan.daylight_available_count)
        self.assertEqual(plan.dark_available_count, restored_plan.dark_available_count)
        self.assertEqual(len(restored_plan.blocks), 48)


if __name__ == "__main__":
    unittest.main()
