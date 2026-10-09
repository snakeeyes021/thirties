"""Unit tests for thirties_core.calendar_engine."""

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from thirties_core.astronomy import build_base_thirties
from thirties_core.calendar_engine import (
    CalendarEvent,
    EDSCalendarEngine,
    MockCalendarEngine,
    map_events_to_blocks,
)
from thirties_core.models import BlockKind


class TestCalendarEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.tz = ZoneInfo("America/New_York")
        self.target_date = date(2026, 10, 8)
        self.blocks = build_base_thirties(38.8799, -77.1067, self.target_date, self.tz)

    def test_overlap_calculation(self) -> None:
        start = datetime(2026, 10, 8, 10, 0, tzinfo=self.tz)
        end = datetime(2026, 10, 8, 11, 0, tzinfo=self.tz)
        event = CalendarEvent(id="e1", summary="Meeting", start_dt=start, end_dt=end)

        # Overlaps 10:15 - 10:45
        self.assertTrue(event.overlaps_interval(
            datetime(2026, 10, 8, 10, 15, tzinfo=self.tz),
            datetime(2026, 10, 8, 10, 45, tzinfo=self.tz),
        ))
        # Adjacent before (09:00 - 10:00) does not overlap
        self.assertFalse(event.overlaps_interval(
            datetime(2026, 10, 8, 9, 0, tzinfo=self.tz),
            datetime(2026, 10, 8, 10, 0, tzinfo=self.tz),
        ))
        # Adjacent after (11:00 - 12:00) does not overlap
        self.assertFalse(event.overlaps_interval(
            datetime(2026, 10, 8, 11, 0, tzinfo=self.tz),
            datetime(2026, 10, 8, 12, 0, tzinfo=self.tz),
        ))

    def test_map_busy_calendar_event(self) -> None:
        # Event from 10:00 AM to 11:30 AM (blocks 20, 21, 22)
        start = datetime(2026, 10, 8, 10, 0, tzinfo=self.tz)
        end = datetime(2026, 10, 8, 11, 30, tzinfo=self.tz)
        event = CalendarEvent(
            id="e_work",
            summary="Architecture Sync",
            start_dt=start,
            end_dt=end,
            response_status="accepted",
        )

        ambiguous = map_events_to_blocks(self.blocks, [event])
        self.assertEqual(len(ambiguous), 0)

        # Check blocks 20, 21, 22
        for idx in [20, 21, 22]:
            b = self.blocks[idx]
            self.assertEqual(b.kind, BlockKind.BUSY_CALENDAR)
            self.assertEqual(b.label, "Architecture Sync")
            self.assertEqual(b.source_event_id, "e_work")
            self.assertTrue(b.is_locked)

        # Preceding block 19 and succeeding block 23 are unaffected
        self.assertNotEqual(self.blocks[19].kind, BlockKind.BUSY_CALENDAR)
        self.assertNotEqual(self.blocks[23].kind, BlockKind.BUSY_CALENDAR)

    def test_map_declined_event_ignored(self) -> None:
        start = datetime(2026, 10, 8, 14, 0, tzinfo=self.tz)
        end = datetime(2026, 10, 8, 15, 0, tzinfo=self.tz)
        event = CalendarEvent(
            id="e_declined",
            summary="Skipped Webinar",
            start_dt=start,
            end_dt=end,
            response_status="declined",
        )

        ambiguous = map_events_to_blocks(self.blocks, [event])
        self.assertEqual(len(ambiguous), 0)
        # Block 28 (14:00 - 14:30) must remain discretionary
        self.assertEqual(self.blocks[28].kind, BlockKind.DAYLIGHT_DISCRETIONARY)

    def test_map_ambiguous_event(self) -> None:
        # Spouse / family event at 11:30 AM (block 23)
        start = datetime(2026, 10, 8, 11, 30, tzinfo=self.tz)
        end = datetime(2026, 10, 8, 12, 0, tzinfo=self.tz)
        event = CalendarEvent(
            id="e_doc",
            summary="Doctor Appointment",
            start_dt=start,
            end_dt=end,
            is_primary=False,
            is_ambiguous=True,
            response_status="needsAction",
        )

        ambiguous = map_events_to_blocks(self.blocks, [event])
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0].id, "e_doc")

        b23 = self.blocks[23]
        self.assertEqual(b23.kind, BlockKind.AMBIGUOUS_CALENDAR)
        self.assertEqual(b23.label, "Doctor Appointment")
        self.assertFalse(b23.is_locked)

    def test_mock_calendar_engine(self) -> None:
        engine = MockCalendarEngine()
        ev1 = CalendarEvent(
            id="ev1",
            summary="Dentist",
            start_dt=datetime(2026, 10, 8, 9, 0, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 8, 10, 0, tzinfo=self.tz),
        )
        ev2 = CalendarEvent(
            id="ev2",
            summary="Tomorrow Workshop",
            start_dt=datetime(2026, 10, 9, 9, 0, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 9, 10, 0, tzinfo=self.tz),
        )
        engine.add_event(ev1)
        engine.add_event(ev2)

        today_events = engine.fetch_events_for_day(self.target_date, self.tz)
        self.assertEqual(len(today_events), 1)
        self.assertEqual(today_events[0].id, "ev1")

    def test_eds_safe_fallback_when_headless(self) -> None:
        engine = EDSCalendarEngine()
        # In a headless/sandbox test without D-Bus session, fetch_events_for_day must return cleanly
        events = engine.fetch_events_for_day(self.target_date, self.tz)
        self.assertIsInstance(events, list)


if __name__ == "__main__":
    unittest.main()
