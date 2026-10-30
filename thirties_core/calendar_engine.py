"""Calendar synchronization engine using GNOME Online Accounts / Evolution Data Server (EDS).

Discovers calendars configured in the desktop environment, extracts events for
the diurnal cycle, turns them into event spans, and identifies invites that
still need the user's decision.

Two kinds of standing choice keep other people's events from coming back (O1:
"advanced acting is not mine. That's my wife's", asked again every week):

- **Per calendar** (Preferences): mine, show only, or ignore. Unset, a calendar
  the user owns is "mine" and anyone else's asks about its events.
- **Per recurring series**: a decision on one occurrence of someone else's
  recurring event covers the whole series (``series_key``), so the wife's weekly
  rehearsal is asked about once, not every week.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from thirties_core.config import CalendarConfig, ThirtiesConfig
from thirties_core.connectors import CalendarSource
from thirties_core.models import EventSpan, SpanCategory

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
    series_id: str = ""  # the recurring series it belongs to (the iCalendar UID); empty: series_of(id)

    @property
    def series(self) -> str:
        return self.series_id or series_of(self.id)

    def overlaps_interval(self, start: datetime, end: datetime) -> bool:
        """Return True if this event overlaps the [start, end) time interval."""
        return max(self.start_dt, start) < min(self.end_dt, end)


# Per-calendar choices (Preferences, keyed by the calendar's display name, lowercased: EDS uids differ between
# computers, names don't). Unset: "mine" for the user's own calendars, ask about each event (once per series)
# for anyone else's.
CALENDAR_MODES = ("mine", "show", "ignore")

# What "not mine" on a recurring series does to the rest of it: dropped from the day entirely. "show" would keep
# the events visible on the Agenda as someone else's, taking no time (the per-calendar "show only"). Dropped is
# the default because the friction was these events coming back at all (O1).
NOT_MINE_SERIES_BECOMES = "ignore"


def series_of(event_id: str) -> str:
    """The recurring series an occurrence id belongs to. EDS occurrence ids are "<uid>@<epoch>" (Google uids have
    their own "@google.com"), so the series is everything before a final all-digit "@..."; anything else is its
    own series."""
    head, sep, tail = (event_id or "").rpartition("@")
    return head if sep and tail.isdigit() and head else event_id


def series_key(event: "CalendarEvent | str") -> str:
    """The decisions-table key for a whole series (it shares the per-occurrence table, so it syncs the same way)."""
    return "series:" + (event.series if isinstance(event, CalendarEvent) else series_of(event))


def calendar_mode(event: CalendarEvent, modes: Optional[Dict[str, str]]) -> str:
    """The user's choice for the event's calendar: "mine", "show", "ignore", or "" when unset."""
    if not modes:
        return ""
    for name in (event.calendar_name, event.calendar_id):
        mode = modes.get((name or "").strip().lower())
        if mode in CALENDAR_MODES:
            return mode
    return ""


def events_to_spans(
    events: List[CalendarEvent],
    decisions: Optional[Dict[str, str]] = None,
    modes: Optional[Dict[str, str]] = None,
) -> Tuple[List[EventSpan], List[CalendarEvent]]:
    """Turn calendar events into spans, applying the user's choices.

    Returns (spans, pending): accepted events become firm spans; invites the user
    has not decided on become tentative spans and are also returned as pending.
    Declined events (in the calendar or by decision, for the occurrence or its
    whole series) and events on ignored calendars are dropped. Events on "show
    only" calendars (or a series marked "not mine", if NOT_MINE_SERIES_BECOMES
    is "show") become show-only spans: on the Agenda, taking no time, never
    asked about. An occurrence's own decision wins over its series'.
    """
    decisions = decisions or {}
    spans: List[EventSpan] = []
    pending: List[CalendarEvent] = []

    for event in events:
        mode = calendar_mode(event, modes)
        decision = decisions.get(event.id) or decisions.get(series_key(event))
        if decision == "not_mine":
            decision = None if NOT_MINE_SERIES_BECOMES == "show" else "decline"
            mode = "show" if NOT_MINE_SERIES_BECOMES == "show" else mode
        if mode == "ignore" or event.response_status == "declined" or decision == "decline":
            continue
        shown_only = mode == "show" and decision != "attend"
        if mode == "mine":  # the user said the calendar is theirs: only a real RSVP question makes it an invite
            is_invite = event.response_status in ("tentative", "needsAction")
        else:
            is_invite = event.is_ambiguous or event.response_status in ("tentative", "needsAction")
        tentative = is_invite and decision != "attend" and not shown_only
        if tentative:
            pending.append(event)
        spans.append(EventSpan(
            id=f"cal:{event.id}",
            title=event.summary,
            start_dt=event.start_dt,
            end_dt=event.end_dt,
            category=SpanCategory.CALENDAR,
            source="calendar",
            source_event_id=event.id,
            is_firm=not tentative and not shown_only,
            tentative=tentative,
            shown_only=shown_only,
        ))

    return spans, pending


CalendarEngine = CalendarSource  # the interface every calendar connector implements (connectors.py)


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
    """A ``CalendarSource``: calendars from Evolution Data Server, which is where GNOME Online Accounts puts Google calendars.

    Recurring events are expanded into their real occurrences, times are read in
    each event's own time zone, all-day events are skipped (they don't occupy
    time), and the user's RSVP decides whether an invite is accepted, declined or
    still pending. Events on calendars the user doesn't own (e.g. a partner's
    shared calendar) need a decision unless the user has accepted them.
    """

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.cal_config: CalendarConfig = self.config.calendar
        self._available = False
        self._clients: Dict[str, Any] = {}
        self._init_eds()

    def _init_eds(self) -> None:
        try:
            import gi
            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            gi.require_version("ICalGLib", "3.0")
            from gi.repository import ECal, EDataServer, ICalGLib  # noqa: F401
            self._available = True
        except Exception as e:
            logger.info("Evolution Data Server (EDS) not available: %s", e)
            self._available = False

    def is_available(self) -> bool:
        return self._available

    # --- sources ---

    def _mode(self, source) -> str:
        """The user's per-calendar choice (Preferences), or "" when unset."""
        modes = getattr(self.cal_config, "modes", None) or {}
        for name in (source.get_display_name(), source.get_uid()):
            mode = modes.get((name or "").strip().lower())
            if mode in CALENDAR_MODES:
                return mode
        return ""

    def _sources(self, registry, with_ignored: bool = False) -> List[Any]:
        from gi.repository import EDataServer
        include = {c.lower() for c in self.cal_config.include_calendars}
        exclude = {c.lower() for c in self.cal_config.exclude_calendars}
        chosen = []
        for source in registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR):
            names = {source.get_display_name().lower(), source.get_uid().lower()}
            if not source.get_enabled() or names & exclude:
                continue
            if include and not names & include:
                continue
            if self._mode(source) == "ignore" and not with_ignored:
                continue  # never read: nothing from it reaches the day
            chosen.append(source)
        return chosen

    def list_calendars(self) -> List[Dict[str, Any]]:
        """The calendars the user can choose for, for Preferences: name, uid, whether they own it, and the
        current choice ("" when unset)."""
        if not self._available:
            return []
        try:
            from gi.repository import EDataServer
            registry = EDataServer.SourceRegistry.new_sync(None)
            emails = self._my_emails(registry)
            return [{"name": s.get_display_name(), "uid": s.get_uid(), "mine": self._owned(s, emails),
                     "mode": self._mode(s)} for s in self._sources(registry, with_ignored=True)]
        except Exception as e:
            logger.warning("Couldn't list calendars: %s", e)
            return []

    def _my_emails(self, registry) -> set:
        from gi.repository import EDataServer
        emails = {e.lower() for e in self.cal_config.my_emails}
        for source in registry.list_sources(EDataServer.SOURCE_EXTENSION_COLLECTION):
            try:
                identity = source.get_extension(EDataServer.SOURCE_EXTENSION_COLLECTION).get_identity()
                if identity and "@" in identity:
                    emails.add(identity.lower())
            except Exception:
                pass
        return emails

    def _owned(self, source, emails: set) -> bool:
        if source.get_uid() == self.cal_config.primary_calendar_id:
            return True
        backend = source.get_extension("Calendar").get_backend_name() if source.has_extension("Calendar") else ""
        return backend == "local" or source.get_display_name().lower() in emails

    def _is_mine(self, source, registry, emails: set) -> bool:
        """Owned, or chosen as "mine" in Preferences; "show only" is never mine."""
        mode = self._mode(source)
        return mode == "mine" or (mode == "" and self._owned(source, emails))

    def _client(self, source):
        from gi.repository import ECal
        uid = source.get_uid()
        if uid not in self._clients:
            self._clients[uid] = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 5, None)
        return self._clients[uid]

    # --- events ---

    @staticmethod
    def _my_partstat(icomp, emails: set) -> Optional[str]:
        """'accepted', 'declined', 'tentative', 'needsAction', or None if the user isn't an attendee."""
        from gi.repository import ICalGLib
        prop = icomp.get_first_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)
        while prop is not None:
            who = (prop.get_attendee() or "").lower().replace("mailto:", "")
            if who in emails:
                param = prop.get_first_parameter(ICalGLib.ParameterKind.PARTSTAT_PARAMETER)
                if param is None:
                    return "needsAction"
                stat = param.get_partstat()
                return {
                    ICalGLib.ParameterPartstat.ACCEPTED: "accepted",
                    ICalGLib.ParameterPartstat.DECLINED: "declined",
                    ICalGLib.ParameterPartstat.TENTATIVE: "tentative",
                }.get(stat, "needsAction")
            prop = icomp.get_next_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)
        return None

    @staticmethod
    def _to_datetime(itime, tz: ZoneInfo) -> datetime:
        zone = itime.get_timezone()
        epoch = itime.as_timet_with_zone(zone) if zone is not None else itime.as_timet()
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone(tz)

    def fetch_events_for_day(self, target_date: date, tz: ZoneInfo) -> List[CalendarEvent]:
        """Occurrences overlapping the calendar date target_date (recurrences expanded)."""
        if not self._available:
            return []
        start = datetime.combine(target_date, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        events: List[CalendarEvent] = []
        try:
            from gi.repository import EDataServer
            registry = EDataServer.SourceRegistry.new_sync(None)
            emails = self._my_emails(registry)
            for source in self._sources(registry):
                mine = self._is_mine(source, registry, emails)
                if not mine and self._mode(source) == "" and self.cal_config.default_ignore_secondary:
                    continue
                try:
                    events.extend(self._source_events(source, mine, emails, start, end, tz))
                except Exception as inner_e:
                    logger.debug("Could not read calendar %s: %s", source.get_display_name(), inner_e)
        except Exception as e:
            logger.warning("Error fetching events from EDS: %s", e)
        return events

    def _source_events(self, source, mine: bool, emails: set, start: datetime, end: datetime,
                       tz: ZoneInfo) -> List[CalendarEvent]:
        client = self._client(source)
        found: List[CalendarEvent] = []

        def on_instance(icomp, instance_start, instance_end, *_args) -> bool:
            if instance_start.is_date():
                return True  # all-day: a note about the day, not time it occupies
            summary = icomp.get_summary() or "Untitled event"
            begin = self._to_datetime(instance_start, tz)
            finish = self._to_datetime(instance_end, tz) if instance_end else begin + timedelta(minutes=30)
            if finish <= begin:
                finish = begin + timedelta(minutes=30)
            partstat = self._my_partstat(icomp, emails)
            status = partstat or "accepted"
            ambiguous = (status in ("needsAction", "tentative")) or (not mine and partstat is None)
            found.append(CalendarEvent(
                id=f"{icomp.get_uid()}@{int(begin.timestamp())}",
                summary=summary,
                start_dt=begin,
                end_dt=finish,
                calendar_id=source.get_uid(),
                calendar_name=source.get_display_name(),
                is_primary=mine,
                response_status=status,
                is_ambiguous=ambiguous,
                series_id=icomp.get_uid() or "",
            ))
            return True

        client.generate_instances_sync(int(start.timestamp()), int(end.timestamp()), None, on_instance, None)
        return found
