"""Unit tests for thirties_core.astronomy."""

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from datetime import datetime
from thirties_core.astronomy import build_base_thirties, create_base_day_plan, get_diurnal_date, get_solar_phases
from thirties_core.models import BlockKind


class TestAstronomy(unittest.TestCase):

    def setUp(self) -> None:
        # Default test location: Arlington, VA (from design doc)
        self.lat = 38.8799
        self.lon = -77.1067
        self.tz = "America/New_York"
        self.test_date = date(2026, 10, 8)

    def test_solar_phases_chronology(self) -> None:
        phases = get_solar_phases(self.lat, self.lon, self.test_date, self.tz)
        
        self.assertIn("dawn", phases)
        self.assertIn("sunrise", phases)
        self.assertIn("solar_noon", phases)
        self.assertIn("sunset", phases)
        self.assertIn("dusk", phases)

        # Dawn < Sunrise < Solar Noon < Sunset < Dusk
        self.assertLess(phases["dawn"], phases["sunrise"])
        self.assertLess(phases["sunrise"], phases["solar_noon"])
        self.assertLess(phases["solar_noon"], phases["sunset"])
        self.assertLess(phases["sunset"], phases["dusk"])

        # Check reasonable hours for October in Virginia (EDT)
        self.assertEqual(phases["sunrise"].hour, 7)
        self.assertEqual(phases["sunset"].hour, 18)

    def test_build_base_thirties_grid(self) -> None:
        blocks = build_base_thirties(self.lat, self.lon, self.test_date, self.tz)
        
        # Must produce exactly 48 blocks
        self.assertEqual(len(blocks), 48)

        for i, block in enumerate(blocks):
            self.assertEqual(block.index, i)
            self.assertEqual(block.duration_seconds, 1800.0)
            if i > 0:
                self.assertEqual(block.start_dt, blocks[i - 1].end_dt)

        # Noon block (index 24: 12:00 - 12:30) must be sunlight
        noon_block = blocks[24]
        self.assertTrue(noon_block.is_sunlight)
        self.assertEqual(noon_block.kind, BlockKind.DAYLIGHT_DISCRETIONARY)

        # Midnight block (index 0: 00:00 - 00:30) must be dark
        midnight_block = blocks[0]
        self.assertFalse(midnight_block.is_sunlight)
        self.assertEqual(midnight_block.kind, BlockKind.DARK_DISCRETIONARY)

        # Late night block (index 47: 23:30 - 24:00) must be dark
        late_block = blocks[47]
        self.assertFalse(late_block.is_sunlight)
        self.assertEqual(late_block.kind, BlockKind.DARK_DISCRETIONARY)

    def test_seasonal_daylight_variation(self) -> None:
        summer_date = date(2026, 6, 21)
        winter_date = date(2026, 12, 21)

        summer_plan = create_base_day_plan(self.lat, self.lon, summer_date, self.tz)
        winter_plan = create_base_day_plan(self.lat, self.lon, winter_date, self.tz)

        # Both have 48 blocks total
        self.assertEqual(len(summer_plan.blocks), 48)
        self.assertEqual(len(winter_plan.blocks), 48)
        self.assertEqual(summer_plan.daylight_available_count + summer_plan.dark_available_count, 48)
        self.assertEqual(winter_plan.daylight_available_count + winter_plan.dark_available_count, 48)

        # Summer solstice has more daylight thirties than winter solstice
        self.assertGreater(summer_plan.daylight_available_count, winter_plan.daylight_available_count)
        # In Arlington: ~14.5 hours daylight in June (~29-30 blocks) vs ~9.5 hours in Dec (~19 blocks)
        self.assertGreaterEqual(summer_plan.daylight_available_count, 28)
        self.assertLessEqual(winter_plan.daylight_available_count, 20)

    def test_get_diurnal_date(self) -> None:
        # Pre-sunrise nocturnal time (e.g. 12:23 AM on Oct 10, 2026) anchors to yesterday (Oct 9)
        nocturnal_dt = datetime(2026, 10, 10, 0, 23, tzinfo=ZoneInfo(self.tz))
        self.assertEqual(get_diurnal_date(nocturnal_dt, self.lat, self.lon, self.tz), date(2026, 10, 9))

        # Post-sunrise daylight time (e.g. 10:00 AM on Oct 10, 2026) anchors to today (Oct 10)
        daylight_dt = datetime(2026, 10, 10, 10, 0, tzinfo=ZoneInfo(self.tz))
        self.assertEqual(get_diurnal_date(daylight_dt, self.lat, self.lon, self.tz), date(2026, 10, 10))

    def test_twilight_flagging(self) -> None:
        blocks = build_base_thirties(self.lat, self.lon, self.test_date, self.tz)
        twilight_blocks = [b for b in blocks if b.is_twilight]
        # There should be twilight blocks around dawn and dusk
        self.assertGreater(len(twilight_blocks), 0)
        for tb in twilight_blocks:
            # Twilight blocks are not fully midday
            self.assertFalse(tb.is_sunlight)


if __name__ == "__main__":
    unittest.main()
