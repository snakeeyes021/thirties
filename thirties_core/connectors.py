"""The connector interfaces: where tasks, calendar events and synced state come from (AGENTS.md anti-pattern 1).

Joplin and Evolution Data Server are the first connectors, not the architecture.
The core talks to them only through these interfaces, so Todoist, CalDAV, Google
Tasks or a headless Joplin peer can slot in beside them:

- ``TaskSource``: the user's lists (``JoplinEngine``).
- ``CalendarSource``: calendar events for a day (``EDSCalendarEngine``,
  ``MockCalendarEngine``).
- ``SyncTransport``: where one device's exported state goes and where the other
  devices' state comes from (``sync_engine.FolderTransport``,
  ``sync_engine.JoplinNoteTransport``).

They're Protocols (structural typing): an implementation needs the methods, not
a base class, so the existing engines conform without inheriting anything.
Writing back (ticking items, adding lines) stays Joplin-specific in
``joplin_writeback`` until a second task source needs it; its shape will come
from that second source rather than be guessed now.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Protocol, Tuple, runtime_checkable
from zoneinfo import ZoneInfo

from thirties_core.models import TaskItem


@runtime_checkable
class TaskSource(Protocol):
    """A task backend: its checklist items as TaskItems, and its notes (lists) as plain dicts."""

    def fetch_tasks(self) -> List[TaskItem]:
        """Every open item the user's settings let Thirties read."""
        ...

    def fetch_active_notes(self) -> List[Dict[str, Any]]:
        """The lists themselves: {"id", "title", "body" (markdown), "source_notebook", ...}."""
        ...

    def list_notebooks(self) -> List[str]:
        """Top-level groupings the user can include or leave out in Preferences."""
        ...


@runtime_checkable
class CalendarSource(Protocol):
    """A calendar backend: the occurrences overlapping a calendar date (recurrences expanded)."""

    def fetch_events_for_day(self, target_date: date, tz: ZoneInfo) -> List[Any]:  # List[CalendarEvent]
        ...


@runtime_checkable
class SyncTransport(Protocol):
    """Where synced state travels. ``push`` stores this device's payload; ``pull`` returns the other devices'
    payloads (never this device's own), plus a message for the user. The merge itself is the SyncEngine's."""

    def push(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        ...

    def pull(self) -> Tuple[bool, str, List[Dict[str, Any]]]:
        ...


def calendars_of(source: Any) -> List[Dict[str, Any]]:
    """The calendars a source offers for per-calendar choices (name, uid, mine, mode); [] if it has no such idea."""
    lister = getattr(source, "list_calendars", None)
    return lister() if callable(lister) else []


__all__ = ["TaskSource", "CalendarSource", "SyncTransport", "calendars_of"]
