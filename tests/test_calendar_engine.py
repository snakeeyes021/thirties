"""Unit tests for thirties_core.calendar_engine."""

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from thirties_core.calendar_engine import (
    CalendarEvent,
    EDSCalendarEngine,
    MockCalendarEngine,
    events_to_spans,
)
from thirties_core.models import SpanCategory


class TestCalendarEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.tz = ZoneInfo("America/New_York")
        self.target_date = date(2026, 10, 8)

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

    def _event(self, eid, start_h, end_h, **kw) -> CalendarEvent:
        return CalendarEvent(
            id=eid, summary=kw.pop("summary", eid),
            start_dt=datetime(2026, 10, 8, *start_h, tzinfo=self.tz),
            end_dt=datetime(2026, 10, 8, *end_h, tzinfo=self.tz), **kw,
        )

    def test_accepted_event_becomes_firm_span(self) -> None:
        spans, pending = events_to_spans([self._event("e_work", (10, 0), (11, 30), summary="Architecture Sync")])
        self.assertEqual(pending, [])
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.category, SpanCategory.CALENDAR)
        self.assertEqual(span.title, "Architecture Sync")
        self.assertEqual(span.source_event_id, "e_work")
        self.assertTrue(span.is_firm)
        self.assertFalse(span.tentative)

    def test_declined_event_ignored(self) -> None:
        spans, pending = events_to_spans([self._event("e_declined", (14, 0), (15, 0), response_status="declined")])
        self.assertEqual((spans, pending), ([], []))

    def test_invite_is_tentative_until_decided(self) -> None:
        invite = self._event("e_doc", (11, 30), (12, 0), is_ambiguous=True, response_status="needsAction")
        spans, pending = events_to_spans([invite])
        self.assertEqual([e.id for e in pending], ["e_doc"])
        self.assertTrue(spans[0].tentative)
        self.assertFalse(spans[0].is_firm)

        spans, pending = events_to_spans([invite], {"e_doc": "attend"})
        self.assertEqual(pending, [])
        self.assertTrue(spans[0].is_firm)

        spans, pending = events_to_spans([invite], {"e_doc": "decline"})
        self.assertEqual((spans, pending), ([], []))

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
