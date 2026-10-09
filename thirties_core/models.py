"""Core domain models for Thirties diurnal planning system."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional


class BlockKind(str, Enum):
    """Categorization of a 30-minute block interval."""
    SLEEP = "sleep"
    WORK = "work"
    BUSY_CALENDAR = "busy_calendar"
    AMBIGUOUS_CALENDAR = "ambiguous_calendar"
    DAYLIGHT_DISCRETIONARY = "daylight_discretionary"
    DARK_DISCRETIONARY = "dark_discretionary"
    ASSIGNED = "assigned"


@dataclass
class ThirtyBlock:
    """Represents a discrete 30-minute interval within a 24-hour cycle.
    
    Index runs from 0 (00:00 - 00:30) to 47 (23:30 - 24:00).
    """
    index: int
    start_dt: datetime
    end_dt: datetime
    is_sunlight: bool
    kind: BlockKind
    is_twilight: bool = False
    assigned_task_id: Optional[str] = None
    label: str = ""
    is_locked: bool = False
    source_event_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not (0 <= self.index <= 47):
            raise ValueError(f"Block index must be in range [0, 47], got {self.index}")
        if self.start_dt >= self.end_dt:
            raise ValueError(f"Block start_dt ({self.start_dt}) must be before end_dt ({self.end_dt})")
        if isinstance(self.kind, str) and not isinstance(self.kind, BlockKind):
            self.kind = BlockKind(self.kind)

    @property
    def duration_seconds(self) -> float:
        return (self.end_dt - self.start_dt).total_seconds()

    @property
    def is_discretionary(self) -> bool:
        return self.kind in (BlockKind.DAYLIGHT_DISCRETIONARY, BlockKind.DARK_DISCRETIONARY)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "start_dt": self.start_dt.isoformat(),
            "end_dt": self.end_dt.isoformat(),
            "is_sunlight": self.is_sunlight,
            "is_twilight": self.is_twilight,
            "kind": self.kind.value,
            "assigned_task_id": self.assigned_task_id,
            "label": self.label,
            "is_locked": self.is_locked,
            "source_event_id": self.source_event_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ThirtyBlock:
        return cls(
            index=data["index"],
            start_dt=datetime.fromisoformat(data["start_dt"]),
            end_dt=datetime.fromisoformat(data["end_dt"]),
            is_sunlight=data["is_sunlight"],
            kind=BlockKind(data["kind"]),
            is_twilight=data.get("is_twilight", False),
            assigned_task_id=data.get("assigned_task_id"),
            label=data.get("label", ""),
            is_locked=data.get("is_locked", False),
            source_event_id=data.get("source_event_id"),
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TaskItem:
        return cls(**data)


@dataclass
class DayPlan:
    """The diurnal plan for a single calendar date."""
    target_date: date
    blocks: List[ThirtyBlock]
    sunrise: datetime
    sunset: datetime
    daylight_available_count: int = 0
    dark_available_count: int = 0
    is_finalized: bool = False
    notes: str = ""

    def get_logical_block(self, logical_index: int) -> ThirtyBlock:
        """Retrieve block by 1-based user-logical day index (1 to 48, starting at Sunrise)."""
        if not (1 <= logical_index <= 48):
            raise IndexError(f"Logical block index must be between 1 and 48, got {logical_index}")
        sunrise_idx = next((b.index for b in self.blocks if b.is_sunlight), 14)
        clock_idx = (sunrise_idx + logical_index - 1) % 48
        return self.get_block(clock_idx)

    def get_logical_index(self, block: ThirtyBlock) -> int:
        """Return 1-based logical index (1 to 48) for a block relative to Sunrise."""
        sunrise_idx = next((b.index for b in self.blocks if b.is_sunlight), 14)
        return (block.index - sunrise_idx) % 48 + 1

    def get_block(self, index: int) -> ThirtyBlock:
        if not (0 <= index <= 47):
            raise IndexError(f"Block index out of bounds: {index}")
        if index < len(self.blocks) and self.blocks[index].index == index:
            return self.blocks[index]
        for b in self.blocks:
            if b.index == index:
                return b
        raise IndexError(f"Block {index} not found in DayPlan")

    def time_str_to_logical_block(self, time_str: str, is_end: bool = False) -> Optional[int]:
        """Convert a natural time string (e.g. '7am', '07:00', '3pm', '15:30') to a 1-based logical block."""
        clean = time_str.strip().lower()
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", clean)
        if not m:
            return None
        h = int(m.group(1))
        mins = int(m.group(2) or 0)
        meridiem = m.group(3)

        if meridiem == "pm" and h < 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        elif not meridiem and h < 7:  # heuristic: times like 3 without am/pm are usually afternoon
            h += 12

        target_dt = datetime.combine(self.target_date, time(h, mins), tzinfo=self.sunrise.tzinfo)
        for b in self.blocks:
            if is_end:
                if b.start_dt < target_dt <= b.end_dt:
                    return self.get_logical_index(b)
            else:
                if b.start_dt <= target_dt < b.end_dt:
                    return self.get_logical_index(b)
        return None

    @property
    def daylight_discretionary_total(self) -> int:
        """Total daylight blocks not occupied by work or sleep."""
        return sum(1 for b in self.blocks if b.is_sunlight and b.kind not in (BlockKind.WORK, BlockKind.SLEEP))

    @property
    def dark_discretionary_total(self) -> int:
        """Total dark blocks not occupied by work or sleep."""
        return sum(1 for b in self.blocks if not b.is_sunlight and b.kind not in (BlockKind.WORK, BlockKind.SLEEP))

    def recalculate_counts(self) -> None:
        """Deterministically tally available discretionary blocks."""
        daylight = 0
        dark = 0
        for block in self.blocks:
            if block.kind == BlockKind.DAYLIGHT_DISCRETIONARY:
                daylight += 1
            elif block.kind == BlockKind.DARK_DISCRETIONARY:
                dark += 1
        self.daylight_available_count = daylight
        self.dark_available_count = dark

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_date": self.target_date.isoformat(),
            "blocks": [b.to_dict() for b in self.blocks],
            "sunrise": self.sunrise.isoformat(),
            "sunset": self.sunset.isoformat(),
            "daylight_available_count": self.daylight_available_count,
            "dark_available_count": self.dark_available_count,
            "is_finalized": self.is_finalized,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DayPlan:
        return cls(
            target_date=date.fromisoformat(data["target_date"]),
            blocks=[ThirtyBlock.from_dict(b) for b in data["blocks"]],
            sunrise=datetime.fromisoformat(data["sunrise"]),
            sunset=datetime.fromisoformat(data["sunset"]),
            daylight_available_count=data.get("daylight_available_count", 0),
            dark_available_count=data.get("dark_available_count", 0),
            is_finalized=data.get("is_finalized", False),
            notes=data.get("notes", ""),
        )
