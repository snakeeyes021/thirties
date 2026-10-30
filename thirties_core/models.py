"""Core domain models for the Thirties diurnal planning system.

Two layers, kept strictly apart (AGENTS.md, Anti-Pattern 3):

- The **solar canvas**: 48 ``ThirtyBlock``s for a diurnal day, starting at the
  thirty that contains sunrise. Blocks carry only physical facts (clock span,
  daylight or dark) and never change because of what the user does.
- **Event spans**: everything the user does or commits to (work, sleep,
  appointments, tasks, activities) is an ``EventSpan`` in real clock time,
  placed on top of the canvas. Spans may cross midnight and may extend past
  the canvas edges.

Occupancy, open-time budgets and the grouped views in clients are all derived
from spans; nothing about user activity is stored on blocks.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class SpanCategory(str, Enum):
    """What kind of thing an event span is."""
    WORK = "work"
    SLEEP = "sleep"
    CALENDAR = "calendar"  # comes from a calendar connector
    TASK = "task"          # linked to a backlog task
    ACTIVITY = "activity"  # anything else the user plans (lunch, gym, travel)


class SpanStatus(str, Enum):
    """What actually happened to a planned span."""
    PLANNED = "planned"
    DONE = "done"
    PARTIAL = "partial"
    SKIPPED = "skipped"


def new_span_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ThirtyBlock:
    """One 30-minute interval of the solar canvas.

    ``index`` is the position within the diurnal day: 0 is the thirty that
    contains sunrise, 47 is the last thirty before the next day's canvas.
    """
    index: int
    start_dt: datetime
    end_dt: datetime
    is_sunlight: bool
    is_twilight: bool = False

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 47):
            raise ValueError(f"Block index must be in range [0, 47], got {self.index}")
        # Compare instants: same-zone comparison uses wall time and gets the DST fall-back hour wrong.
        if self.start_dt.timestamp() >= self.end_dt.timestamp():
            raise ValueError(f"Block start_dt ({self.start_dt}) must be before end_dt ({self.end_dt})")

    @property
    def number(self) -> int:
        """1-based position shown to people (#01 is the sunrise thirty)."""
        return self.index + 1

    @property
    def duration_seconds(self) -> float:
        return self.end_dt.timestamp() - self.start_dt.timestamp()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "start_dt": self.start_dt.isoformat(),
            "end_dt": self.end_dt.isoformat(),
            "is_sunlight": self.is_sunlight,
            "is_twilight": self.is_twilight,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ThirtyBlock:
        return cls(
            index=data["index"],
            start_dt=datetime.fromisoformat(data["start_dt"]),
            end_dt=datetime.fromisoformat(data["end_dt"]),
            is_sunlight=data["is_sunlight"],
            is_twilight=data.get("is_twilight", False),
        )


@dataclass
class EventSpan:
    """Something placed on the canvas, in real clock time."""
    id: str
    title: str
    start_dt: datetime
    end_dt: datetime
    category: SpanCategory
    source: str = "user"  # "user", "routine" (defaults from config), or "calendar"
    task_id: Optional[str] = None
    source_event_id: Optional[str] = None
    is_firm: bool = False    # a hard commitment (accepted calendar event): never moved or overlapped without force
    tentative: bool = False  # an unconfirmed calendar invite awaiting the user's decision
    status: SpanStatus = SpanStatus.PLANNED
    notes: str = ""
    # What actually happened, once known. time_source says how reliable the times are:
    # "reported" (the user said), "checkin" (inferred from a check-in), "estimated" (the
    # assistant's best guess), or "planned" (no information beyond the plan).
    actual_start: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    time_source: str = ""
    # The original plan, kept when an outcome moves the span to when it really happened.
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.start_dt.timestamp() >= self.end_dt.timestamp():
            raise ValueError(f"Span '{self.title}' must start before it ends ({self.start_dt} >= {self.end_dt})")
        if isinstance(self.category, str) and not isinstance(self.category, SpanCategory):
            self.category = SpanCategory(self.category)
        if isinstance(self.status, str) and not isinstance(self.status, SpanStatus):
            self.status = SpanStatus(self.status)

    @property
    def duration_minutes(self) -> int:
        return int((self.end_dt.timestamp() - self.start_dt.timestamp()) // 60)

    @property
    def is_envelope(self) -> bool:
        """Work and sleep are long backdrops that other spans may sit on top of."""
        return self.category in (SpanCategory.WORK, SpanCategory.SLEEP)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start_dt < end and start < self.end_dt

    def covers(self, t: datetime) -> bool:
        return self.start_dt <= t < self.end_dt

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_dt": self.start_dt.isoformat(),
            "end_dt": self.end_dt.isoformat(),
            "category": self.category.value,
            "source": self.source,
            "task_id": self.task_id,
            "source_event_id": self.source_event_id,
            "is_firm": self.is_firm,
            "tentative": self.tentative,
            "status": self.status.value,
            "notes": self.notes,
            "actual_start": self.actual_start.isoformat() if self.actual_start else None,
            "actual_end": self.actual_end.isoformat() if self.actual_end else None,
            "time_source": self.time_source,
            "planned_start": self.planned_start.isoformat() if self.planned_start else None,
            "planned_end": self.planned_end.isoformat() if self.planned_end else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EventSpan:
        return cls(
            id=data["id"],
            title=data["title"],
            start_dt=datetime.fromisoformat(data["start_dt"]),
            end_dt=datetime.fromisoformat(data["end_dt"]),
            category=SpanCategory(data["category"]),
            source=data.get("source", "user"),
            task_id=data.get("task_id"),
            source_event_id=data.get("source_event_id"),
            is_firm=data.get("is_firm", False),
            tentative=data.get("tentative", False),
            status=SpanStatus(data.get("status", "planned")),
            notes=data.get("notes", ""),
            actual_start=datetime.fromisoformat(data["actual_start"]) if data.get("actual_start") else None,
            actual_end=datetime.fromisoformat(data["actual_end"]) if data.get("actual_end") else None,
            time_source=data.get("time_source", ""),
            planned_start=datetime.fromisoformat(data["planned_start"]) if data.get("planned_start") else None,
            planned_end=datetime.fromisoformat(data["planned_end"]) if data.get("planned_end") else None,
        )


@dataclass
class TaskItem:
    """A task candidate ingested from Joplin or created in conversation."""
    id: str
    source_notebook: str
    title: str
    description: str = ""
    is_checkbox_item: bool = False
    parent_note_title: Optional[str] = None
    parent_note_id: Optional[str] = None
    estimated_thirties: int = 1
    deferred_count: int = 0
    tags: List[str] = field(default_factory=list)
    section: str = ""                     # nearest heading above the item inside its note
    parent_task_id: Optional[str] = None  # the checklist item this one is indented under
    someday: bool = False                 # listed under a "someday/maybe" heading: visible, never pushed
    list_role: str = ""                   # what its note is for (lists.ROLES), once known
    urgency: str = ""                     # now / soon / whenever / someday (lists.URGENCIES), once known
    due: str = ""                         # ISO date it's due by, if the user gave one
    horizon: str = ""                     # day / week / month / year / someday (horizons.HORIZONS), once known
    occurrence: int = 0                   # which copy of the same text in its note (0 = the first open one)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskItem:
        return cls(**data)


# Which span is shown for a thirty when several overlap: the most specific wins.
_DISPLAY_PRIORITY = {
    SpanCategory.CALENDAR: 0,
    SpanCategory.TASK: 1,
    SpanCategory.ACTIVITY: 1,
    SpanCategory.WORK: 2,
    SpanCategory.SLEEP: 3,
}


@dataclass
class DayPlan:
    """One diurnal day: the solar canvas plus the spans that touch it."""
    target_date: date
    blocks: List[ThirtyBlock]
    sunrise: datetime
    sunset: datetime
    spans: List[EventSpan] = field(default_factory=list)
    is_finalized: bool = False
    notes: str = ""

    # --- canvas ---

    @property
    def day_start(self) -> datetime:
        return self.blocks[0].start_dt

    @property
    def day_end(self) -> datetime:
        return self.blocks[-1].end_dt

    def get_block(self, index: int) -> ThirtyBlock:
        if not (0 <= index < len(self.blocks)):
            raise IndexError(f"Block index out of bounds: {index}")
        return self.blocks[index]

    def block_at(self, t: datetime) -> Optional[ThirtyBlock]:
        return next((b for b in self.blocks if b.start_dt <= t < b.end_dt), None)

    # --- spans ---

    def get_span(self, span_id: str) -> Optional[EventSpan]:
        return next((s for s in self.spans if s.id == span_id), None)

    def active_spans(self) -> List[EventSpan]:
        """Spans that occupy time (unconfirmed invites do not)."""
        return [s for s in self.spans if not s.tentative]

    def spans_overlapping(self, start: datetime, end: datetime, include_tentative: bool = False) -> List[EventSpan]:
        pool = self.spans if include_tentative else self.active_spans()
        return sorted((s for s in pool if s.overlaps(start, end)), key=lambda s: s.start_dt)

    def spans_in_view(self) -> List[EventSpan]:
        """All spans touching this day's canvas, in start order."""
        return self.spans_overlapping(self.day_start, self.day_end, include_tentative=True)

    def display_span(self, block: ThirtyBlock) -> Optional[EventSpan]:
        """The single span to show for a thirty: activities and appointments sit on top of work and sleep."""
        occupants = self.spans_overlapping(block.start_dt, block.end_dt)
        if not occupants:
            return None
        return min(occupants, key=lambda s: (_DISPLAY_PRIORITY[s.category], s.start_dt))

    def display_title(self, block: ThirtyBlock) -> Optional[str]:
        """Title for a thirty, joining things done at the same time ("Dinner + Audiobook")."""
        occupants = self.spans_overlapping(block.start_dt, block.end_dt)
        if not occupants:
            return None
        foreground = [s for s in occupants if not s.is_envelope]
        if not foreground:
            return self.display_span(block).title
        in_order = sorted(foreground, key=lambda s: s.start_dt)
        if len(in_order) > 1 and all(b.start_dt >= a.end_dt for a, b in zip(in_order, in_order[1:])):
            return "\n".join(s.title for s in in_order)  # one after another inside the thirty: a line each
        titles: List[str] = []
        for s in sorted(foreground, key=lambda s: (_DISPLAY_PRIORITY[s.category], s.start_dt)):
            if s.title not in titles:
                titles.append(s.title)
        return " + ".join(titles)

    def is_open(self, block: ThirtyBlock) -> bool:
        return not self.spans_overlapping(block.start_dt, block.end_dt)

    def _in_envelope(self, block: ThirtyBlock) -> bool:
        return any(s.is_envelope for s in self.spans_overlapping(block.start_dt, block.end_dt))

    # --- budgets (derived, never stored) ---

    def availability(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """Open and schedulable thirties that haven't ended yet, split by daylight and dark.

        "total" counts thirties outside work and sleep; "open" those of them with nothing
        planned. With ``now``, thirties that have already ended are left out of both, so
        "Daylight 1/5" means one open daylight thirty among the five still ahead.
        """
        counts = {"daylight_open": 0, "daylight_total": 0, "dark_open": 0, "dark_total": 0}
        for b in self.blocks:
            if now is not None and b.end_dt.timestamp() <= now.timestamp():
                continue
            if self._in_envelope(b):
                continue
            key = "daylight" if b.is_sunlight else "dark"
            counts[f"{key}_total"] += 1
            if self.is_open(b):
                counts[f"{key}_open"] += 1
        return counts

    @property
    def daylight_available_count(self) -> int:
        return sum(1 for b in self.blocks if b.is_sunlight and self.is_open(b))

    @property
    def dark_available_count(self) -> int:
        return sum(1 for b in self.blocks if not b.is_sunlight and self.is_open(b))

    @property
    def daylight_discretionary_total(self) -> int:
        """Daylight thirties outside work and sleep."""
        return sum(1 for b in self.blocks if b.is_sunlight and not self._in_envelope(b))

    @property
    def dark_discretionary_total(self) -> int:
        """Dark thirties outside work and sleep."""
        return sum(1 for b in self.blocks if not b.is_sunlight and not self._in_envelope(b))

    # --- serialization ---

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": 2,
            "target_date": self.target_date.isoformat(),
            "blocks": [b.to_dict() for b in self.blocks],
            "sunrise": self.sunrise.isoformat(),
            "sunset": self.sunset.isoformat(),
            "spans": [s.to_dict() for s in self.spans],
            "is_finalized": self.is_finalized,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DayPlan:
        if "spans" not in data:
            from thirties_core.legacy import plan_from_legacy_dict
            return plan_from_legacy_dict(data)
        return cls(
            target_date=date.fromisoformat(data["target_date"]),
            blocks=[ThirtyBlock.from_dict(b) for b in data["blocks"]],
            sunrise=datetime.fromisoformat(data["sunrise"]),
            sunset=datetime.fromisoformat(data["sunset"]),
            spans=[EventSpan.from_dict(s) for s in data.get("spans", [])],
            is_finalized=data.get("is_finalized", False),
            notes=data.get("notes", ""),
        )
