"""Calendar synchronization engine using GNOME Online Accounts / Evolution Data Server (EDS).

Discovers calendars configured in the desktop environment, extracts events for
the diurnal cycle, maps them to discrete 30-minute blocks, and identifies
ambiguous commitments for conversational resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol
from zoneinfo import ZoneInfo

from thirties_core.config import CalendarConfig, ThirtiesConfig
from thirties_core.models import BlockKind, ThirtyBlock

logger = logging.getLogger(__name__)


@dataclass
class CalendarEvent:
    """Standardized calendar event representation."""
    id: str
    summary: str
    start_dt: datetime
    end_dt: datetime
    is_all_day: bool = False
    calendar_id: str = "primary"
    calendar_name: str = ""
    is_primary: bool = True
    attendees: List[str] = field(default_factory=list)
    response_status: str = "accepted"  # accepted, tentative, declined, needsAction
    is_ambiguous: bool = False

    def overlaps_interval(self, start: datetime, end: datetime) -> bool:
        """Return True if this event overlaps the [start, end) time interval."""
        return max(self.start_dt, start) < min(self.end_dt, end)


def map_events_to_blocks(
    blocks: List[ThirtyBlock],
    events: List[CalendarEvent],
) -> List[CalendarEvent]:
    """Overlay calendar events onto Thirty blocks.
    
    Returns the list of ambiguous events that require user clarification.
    """
    ambiguous_events: List[CalendarEvent] = []

    for event in events:
        if event.response_status == "declined":
            continue

        if event.is_ambiguous or event.response_status in ("tentative", "needsAction"):
            if event not in ambiguous_events:
                ambiguous_events.append(event)

            for block in blocks:
                if event.overlaps_interval(block.start_dt, block.end_dt):
                    # Only mark ambiguous if not already locked as busy
                    if block.kind != BlockKind.BUSY_CALENDAR:
                        block.kind = BlockKind.AMBIGUOUS_CALENDAR
                        block.label = event.summary
                        block.source_event_id = event.id
        else:
            # Hard calendar block
            for block in blocks:
                if event.overlaps_interval(block.start_dt, block.end_dt):
                    block.kind = BlockKind.BUSY_CALENDAR
                    block.label = event.summary
                    block.source_event_id = event.id
                    block.is_locked = True

    return ambiguous_events


class CalendarEngine(Protocol):
    """Protocol for calendar sources."""

    def fetch_events_for_day(self, target_date: date, tz: ZoneInfo) -> List[CalendarEvent]:
        ...


class MockCalendarEngine:
    """Mock calendar engine for testing and offline development."""

    def __init__(self, events: Optional[List[CalendarEvent]] = None) -> None:
        self.events: List[CalendarEvent] = list(events) if events else []

    def add_event(self, event: CalendarEvent) -> None:
        self.events.append(event)

    def clear(self) -> None:
        self.events.clear()

    def fetch_events_for_day(self, target_date: date, tz: ZoneInfo) -> List[CalendarEvent]:
        day_start = datetime.combine(target_date, time.min, tzinfo=tz)
        day_end = datetime.combine(target_date, time.max, tzinfo=tz)
        return [
            e for e in self.events
            if e.overlaps_interval(day_start, day_end)
        ]


class EDSCalendarEngine:
    """Evolution Data Server calendar engine leveraging GNOME Online Accounts."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.cal_config: CalendarConfig = self.config.calendar
        self._available = False
        self._init_eds()

    def _init_eds(self) -> None:
        try:
            import gi
            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            from gi.repository import ECal, EDataServer  # noqa: F401
            self._available = True
        except Exception as e:
            logger.info("Evolution Data Server (EDS) not available: %s", e)
            self._available = False

    def is_available(self) -> bool:
        return self._available

    def fetch_events_for_day(self, target_date: date, tz: ZoneInfo) -> List[CalendarEvent]:
        """Query EDS for events on target_date."""
        if not self._available:
            return []

        events: List[CalendarEvent] = []
        day_start = datetime.combine(target_date, time.min, tzinfo=tz)
        day_end = datetime.combine(target_date, time.max, tzinfo=tz)

        try:
            import gi
            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            from gi.repository import ECal, EDataServer, ICalGLib

            registry = EDataServer.SourceRegistry.new_sync(None)
            sources = registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)

            iso_start = day_start.isoformat()
            iso_end = day_end.isoformat()
            sexp = f"(occur-in-time-range? (make-time \"{iso_start}\") (make-time \"{iso_end}\"))"

            for source in sources:
                display_name = source.get_display_name()
                source_uid = source.get_uid()
                is_primary = (source_uid == self.cal_config.primary_calendar_id or "personal" in display_name.lower())

                try:
                    client = ECal.Client.connect_sync(
                        source,
                        ECal.ClientSourceType.EVENTS,
                        3,
                        None,
                    )
                    _, comps = client.get_object_list_as_comps_sync(sexp, None)
                    for comp in comps:
                        summary = comp.get_summary()
                        summary_text = summary.get_value() if summary else "Untitled Event"
                        uid = comp.get_uid() or "unknown-uid"

                        dtstart = comp.get_dtstart()
                        dtend = comp.get_dtend()

                        if not dtstart:
                            continue

                        # Convert ICalTime to datetime
                        start_py = self._ical_time_to_dt(dtstart, tz)
                        end_py = self._ical_time_to_dt(dtend, tz) if dtend else start_py + timedelta(minutes=30)

                        is_ambiguous = (not is_primary and not self.cal_config.default_ignore_secondary)

                        events.append(
                            CalendarEvent(
                                id=uid,
                                summary=summary_text,
                                start_dt=start_py,
                                end_dt=end_py,
                                calendar_id=source_uid,
                                calendar_name=display_name,
                                is_primary=is_primary,
                                is_ambiguous=is_ambiguous,
                            )
                        )
                except Exception as inner_e:
                    logger.debug("Could not query calendar source %s: %s", display_name, inner_e)

        except Exception as e:
            logger.warning("Error fetching events from EDS: %s", e)

        return events

    def _ical_time_to_dt(self, ical_prop: Any, tz: ZoneInfo) -> datetime:
        """Convert ICalGLib.Property or ICalGLib.Time to a timezone-aware datetime."""
        from gi.repository import ICalGLib
        time_val = ical_prop.get_value() if hasattr(ical_prop, "get_value") else ical_prop
        if isinstance(time_val, ICalGLib.Time):
            return datetime(
                year=time_val.get_year(),
                month=time_val.get_month(),
                day=time_val.get_day(),
                hour=time_val.get_hour(),
                minute=time_val.get_minute(),
                second=time_val.get_second(),
                tzinfo=timezone.utc,
            ).astimezone(tz)
        return datetime.now(tz)
