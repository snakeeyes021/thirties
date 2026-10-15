"""Shared, model-agnostic schedule tool API.

Every brain (see ``thirties_core.brains``) changes the day plan only through
``PlanTools``. The tools own all block arithmetic, invariants and persistence,
so no brain ever has to do schedule math itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from thirties_core.calendar_engine import CalendarEvent
from thirties_core.models import BlockKind, DayPlan, ThirtyBlock
from thirties_core.scheduler import DeterministicScheduler

logger = logging.getLogger(__name__)


@dataclass
class BlockState:
    kind: BlockKind
    label: str
    assigned_task_id: Optional[str]
    is_locked: bool
    source_event_id: Optional[str]


@dataclass
class PlanSnapshot:
    blocks: dict[int, BlockState]
    ambiguous_event_ids: set[str]
    is_finalized: bool


@dataclass
class StateDelta:
    modified_blocks: list[int]
    resolved_events: list[str]
    finalized_changed: bool

    @property
    def total_changes(self) -> int:
        return len(self.modified_blocks) + len(self.resolved_events) + (1 if self.finalized_changed else 0)


PLANNING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "modify_blocks",
            "description": "Universal schedule mutation primitive: set block kinds (WORK, SLEEP, DISCRETIONARY), assign tasks/labels, or set lock state across a logical block range (1 to 48) or clock times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48, "description": "Start logical block (1 to 48)"},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48, "description": "End logical block (1 to 48)"},
                    "start_time": {"type": "string", "description": "Start clock time e.g. '10:00 AM'"},
                    "end_time": {"type": "string", "description": "End clock time e.g. '4:00 PM'"},
                    "kind": {"type": "string", "enum": ["WORK", "SLEEP", "DISCRETIONARY"], "description": "Diurnal container kind"},
                    "label": {"type": "string", "description": "Activity or task label for these blocks"},
                    "task_id": {"type": "string", "description": "Joplin task ID if assigning a task"},
                    "is_locked": {"type": "boolean", "description": "True if locked in card envelope"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_blocks",
            "description": "Clear or open blocks (e.g. opening work blocks for a day off, or unassigning scheduled tasks), returning them to open discretionary time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "start_time": {"type": "string", "description": "Start clock time e.g. '07:00 AM'"},
                    "end_time": {"type": "string", "description": "End clock time e.g. '03:00 PM'"},
                    "clear_kind": {"type": "string", "enum": ["WORK", "SLEEP", "ALL", "TASKS", "EVENTS"]},
                    "clear_all_work": {"type": "boolean", "description": "True to clear all work blocks for today"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_event",
            "description": "Mark an ambiguous calendar event as attended (hard block) or declined (open discretionary).",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The unique event ID"},
                    "action": {"type": "string", "enum": ["attend", "decline"], "description": "'attend' to lock as busy calendar block, 'decline' to open as discretionary"}
                },
                "required": ["event_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_blocks",
            "description": "Inspect schedule occupancy, envelope kinds, labels, and times for a block range or clock times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "start_time": {"type": "string", "description": "Start clock time e.g. '07:00 AM'"},
                    "end_time": {"type": "string", "description": "End clock time e.g. '03:00 PM'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_day_plan",
            "description": "Lock in the day's schedule once the user is satisfied.",
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {"type": "string", "description": "Optional notes or reflections for the day"}
                }
            }
        }
    }
]


class PlanTools:
    """Executes schedule tool calls against a DayPlan and persists the result."""

    def __init__(
        self,
        day_plan: DayPlan,
        ambiguous_events: List[CalendarEvent],
        scheduler: Optional[DeterministicScheduler],
    ) -> None:
        self.day_plan = day_plan
        self.ambiguous_events = list(ambiguous_events)
        self.scheduler = scheduler

    def modify_blocks(
        self,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        kind: Optional[str] = None,
        label: Optional[str] = None,
        task_id: Optional[str] = None,
        is_locked: Optional[bool] = None,
        clear_existing_envelope: Optional[bool] = None,
        force_calendar: bool = False,
        **kwargs: Any,
    ) -> str:
        """Universal schedule primitive: modify block kinds, assigned tasks, or lock states."""
        if start_time and not start_block:
            start_block = self.day_plan.time_str_to_logical_block(str(start_time))
        if end_time and not end_block:
            end_block = self.day_plan.time_str_to_logical_block(str(end_time), is_end=True)

        norm_kind = kind.upper() if kind else None
        if clear_existing_envelope is None:
            clear_existing_envelope = (norm_kind in ("WORK", "SLEEP"))

        if start_block is None and end_block is None:
            if norm_kind == "WORK":
                work_start = self.scheduler.config.general.work_start_thirty
                work_end = self.scheduler.config.general.work_end_thirty
                start_block = self.day_plan.get_logical_index(self.day_plan.get_block(work_start))
                end_block = self.day_plan.get_logical_index(self.day_plan.get_block(work_end))
            elif norm_kind == "SLEEP":
                start_block = 33
                end_block = 48
            else:
                return "No blocks specified to modify."

        if start_block is not None and end_block is None:
            end_block = start_block

        if end_block >= start_block:
            indices_range = list(range(start_block, end_block + 1))
        else:
            indices_range = list(range(start_block, 49)) + list(range(1, end_block + 1))

        target_blocks: list[ThirtyBlock] = []
        target_indices: set[int] = set()
        for idx in indices_range:
            if 1 <= idx <= 48:
                b = self.day_plan.get_logical_block(idx)
                target_blocks.append(b)
                target_indices.add(idx)

        # Check calendar lock invariant
        skipped_calendar: list[int] = []
        mutable_blocks: list[ThirtyBlock] = []
        for b in target_blocks:
            log_idx = self.day_plan.get_logical_index(b)
            if (b.kind == BlockKind.BUSY_CALENDAR or b.source_event_id) and b.is_locked and not force_calendar:
                skipped_calendar.append(log_idx)
            else:
                mutable_blocks.append(b)

        if not mutable_blocks and skipped_calendar:
            return f"Cannot modify block(s) {skipped_calendar}: locked by calendar event. Use force_calendar=True to override."

        preserved_tasks: list[str] = []

        if norm_kind == "WORK":
            if clear_existing_envelope:
                # Clear old WORK blocks outside target_indices back to discretionary
                for b in self.day_plan.blocks:
                    log_idx = self.day_plan.get_logical_index(b)
                    if b.kind == BlockKind.WORK and log_idx not in target_indices:
                        b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                        b.is_locked = False
                        if not b.assigned_task_id and b.label == "Work":
                            b.label = ""

            # Assign mutable blocks to WORK
            for b in mutable_blocks:
                log_idx = self.day_plan.get_logical_index(b)
                if b.label and b.label != "Work":
                    preserved_tasks.append(f"Block {log_idx} ('{b.label}')")
                else:
                    b.label = label or "Work"
                    b.assigned_task_id = task_id
                b.kind = BlockKind.WORK
                b.is_locked = True if is_locked is None else is_locked

        elif norm_kind == "SLEEP":
            if clear_existing_envelope:
                # Clear old SLEEP blocks outside target_indices
                for b in self.day_plan.blocks:
                    log_idx = self.day_plan.get_logical_index(b)
                    if b.kind == BlockKind.SLEEP and log_idx not in target_indices:
                        b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                        b.is_locked = False
                        if b.label == "Sleep":
                            b.label = ""

            # Assign mutable blocks to SLEEP
            for b in mutable_blocks:
                b.kind = BlockKind.SLEEP
                b.label = label or "Sleep"
                b.is_locked = True if is_locked is None else is_locked
                b.assigned_task_id = None

        elif norm_kind == "DISCRETIONARY":
            for b in mutable_blocks:
                b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                b.is_locked = False if is_locked is None else is_locked
                b.label = label or ""
                b.assigned_task_id = task_id

        else:
            # Assign task / label while preserving diurnal container kind
            for b in mutable_blocks:
                if label:
                    b.label = label
                if task_id:
                    b.assigned_task_id = task_id
                if is_locked is not None:
                    b.is_locked = is_locked

        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)

        count = len(mutable_blocks)
        chunk_word = "chunk" if count == 1 else "chunks"
        count_str = f"{count} {chunk_word}"

        if mutable_blocks:
            first_log = self.day_plan.get_logical_index(mutable_blocks[0])
            last_log = self.day_plan.get_logical_index(mutable_blocks[-1])
            start_clk = mutable_blocks[0].start_dt.strftime('%I:%M %p').lstrip('0')
            end_clk = mutable_blocks[-1].end_dt.strftime('%I:%M %p').lstrip('0')
            span_str = f"from {start_clk} to {end_clk} (Block {first_log})" if first_log == last_log else f"from {start_clk} to {end_clk} (Blocks {first_log}–{last_log})"
        else:
            span_str = "requested blocks"

        conflict_note = f" (skipped locked calendar blocks {skipped_calendar})" if skipped_calendar else ""

        if norm_kind == "WORK":
            if preserved_tasks:
                return f"Work is now scheduled {span_str} for {count_str}, keeping your existing {', '.join(preserved_tasks)} intact.{conflict_note}"
            return f"Work is now scheduled {span_str} for {count_str}.{conflict_note}"
        elif norm_kind == "SLEEP":
            return f"Sleep window is now scheduled {span_str} for {count_str}.{conflict_note}"
        else:
            lbl_desc = f"'{label}'" if label else "scheduled"
            if len(mutable_blocks) == 1:
                return f"Allocated block {first_log} from {start_clk} to {end_clk} (1 chunk) to {lbl_desc}.{conflict_note}"
            else:
                return f"Allocated blocks {first_log} through {last_log} from {start_clk} to {end_clk} ({count_str}) to {lbl_desc}.{conflict_note}"

    def clear_blocks(
        self,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        clear_kind: Optional[str] = None,
        clear_all_work: bool = False,
        force_calendar: bool = False,
        **kwargs: Any,
    ) -> str:
        """Clear blocks back to open discretionary time."""
        if start_time and not start_block:
            start_block = self.day_plan.time_str_to_logical_block(str(start_time))
        if end_time and not end_block:
            end_block = self.day_plan.time_str_to_logical_block(str(end_time), is_end=True)

        target_blocks: list[ThirtyBlock] = []
        norm_kind = clear_kind.upper() if clear_kind else None

        if norm_kind == "WORK" or clear_all_work:
            target_blocks = [b for b in self.day_plan.blocks if b.kind == BlockKind.WORK]
        elif norm_kind == "SLEEP":
            target_blocks = [b for b in self.day_plan.blocks if b.kind == BlockKind.SLEEP]
        elif norm_kind in ("ALL", "TASKS", "EVENTS"):
            target_blocks = [
                b for b in self.day_plan.blocks
                if b.kind not in (BlockKind.WORK, BlockKind.SLEEP) or b.assigned_task_id or (b.label and b.label not in ("Work", "Sleep"))
            ]
        elif start_block is not None:
            if end_block is None:
                end_block = start_block
            for b_idx in range(start_block, end_block + 1):
                if 1 <= b_idx <= 48:
                    target_blocks.append(self.day_plan.get_logical_block(b_idx))

        cleared_count = 0
        cleared_blocks_list: list[ThirtyBlock] = []
        for b in target_blocks:
            if (b.kind == BlockKind.BUSY_CALENDAR or b.source_event_id) and b.is_locked and not force_calendar:
                continue
            if norm_kind in ("ALL", "TASKS", "EVENTS") and b.kind in (BlockKind.WORK, BlockKind.SLEEP):
                b.label = "Work" if b.kind == BlockKind.WORK else "Sleep"
                b.assigned_task_id = None
            else:
                b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                b.is_locked = False
                b.label = ""
                b.assigned_task_id = None
                b.source_event_id = None
            cleared_count += 1
            cleared_blocks_list.append(b)

        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)

        count_str = f"{cleared_count} block" if cleared_count == 1 else f"{cleared_count} blocks"
        if norm_kind in ("ALL", "TASKS", "EVENTS"):
            return f"Cleared all scheduled tasks and custom events for today ({count_str} cleared)."
        if cleared_blocks_list and not norm_kind and not clear_all_work:
            first_log = self.day_plan.get_logical_index(cleared_blocks_list[0])
            last_log = self.day_plan.get_logical_index(cleared_blocks_list[-1])
            s_clk = cleared_blocks_list[0].start_dt.strftime('%I:%M %p').lstrip('0')
            e_clk = cleared_blocks_list[-1].end_dt.strftime('%I:%M %p').lstrip('0')
            span_str = f"from {s_clk} to {e_clk} (Block {first_log})" if first_log == last_log else f"from {s_clk} to {e_clk} (Blocks {first_log}–{last_log})"
            return f"Cleared and opened {count_str} {span_str} as discretionary time."
        return f"Cleared and opened {count_str} as discretionary time."

    def resolve_event(
        self,
        event_id: str,
        action: str = "attend",
        attending: Optional[bool] = None,
        **kwargs: Any,
    ) -> str:
        """Mark an ambiguous calendar event as attended (hard block) or declined (open discretionary)."""
        if attending is None:
            is_attending = (str(action).lower() in ("attend", "attending", "confirm", "accept", "true"))
        else:
            is_attending = bool(attending)

        target_event_id = event_id
        if event_id in ("unconfirmed", "*"):
            if self.ambiguous_events:
                target_event_id = self.ambiguous_events[0].id

        for block in self.day_plan.blocks:
            if block.source_event_id == target_event_id:
                if is_attending:
                    block.kind = BlockKind.BUSY_CALENDAR
                    block.is_locked = True
                else:
                    block.kind = BlockKind.DAYLIGHT_DISCRETIONARY if block.is_sunlight else BlockKind.DARK_DISCRETIONARY
                    block.label = ""
                    block.source_event_id = None
                    block.is_locked = False

        self.ambiguous_events = [e for e in self.ambiguous_events if e.id != target_event_id]
        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)
        action_desc = "locked as busy calendar block" if is_attending else "declined and opened as discretionary"
        return f"Event {target_event_id} resolved: {action_desc}."

    def inspect_blocks(
        self,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Inspect schedule occupancy, envelope kinds, labels, and times for a block range or clock times."""
        if start_time and not start_block:
            start_block = self.day_plan.time_str_to_logical_block(str(start_time))
        if end_time and not end_block:
            end_block = self.day_plan.time_str_to_logical_block(str(end_time), is_end=True)
        if start_block is None:
            start_block = 1
        if end_block is None:
            end_block = start_block
        lines = []
        for idx in range(start_block, end_block + 1):
            if 1 <= idx <= 48:
                b = self.day_plan.get_logical_block(idx)
                s_clk = b.start_dt.strftime('%I:%M %p').lstrip('0')
                e_clk = b.end_dt.strftime('%I:%M %p').lstrip('0')
                lines.append(f"Block {idx} ({s_clk}–{e_clk}): {b.kind.name} | label='{b.label}' | locked={b.is_locked}")
        return "\n".join(lines) if lines else "No blocks found in range."

    def capture_snapshot(self) -> PlanSnapshot:
        """Capture complete immutable state snapshot of DayPlan before/after turn."""
        b_map = {}
        for b in self.day_plan.blocks:
            b_map[b.index] = BlockState(
                kind=b.kind,
                label=b.label or "",
                assigned_task_id=b.assigned_task_id,
                is_locked=b.is_locked,
                source_event_id=b.source_event_id,
            )
        return PlanSnapshot(
            blocks=b_map,
            ambiguous_event_ids={e.id for e in self.ambiguous_events},
            is_finalized=bool(self.day_plan.is_finalized),
        )

    @staticmethod
    def compute_delta(before: PlanSnapshot, after: PlanSnapshot) -> StateDelta:
        """Compute factual delta between two state snapshots."""
        modified: list[int] = []
        for idx, old_b in before.blocks.items():
            new_b = after.blocks.get(idx)
            if new_b and (
                old_b.kind != new_b.kind
                or old_b.label != new_b.label
                or old_b.assigned_task_id != new_b.assigned_task_id
                or old_b.is_locked != new_b.is_locked
                or old_b.source_event_id != new_b.source_event_id
            ):
                modified.append(idx)
        resolved = list(before.ambiguous_event_ids - after.ambiguous_event_ids)
        finalized_changed = (before.is_finalized != after.is_finalized)
        return StateDelta(
            modified_blocks=modified,
            resolved_events=resolved,
            finalized_changed=finalized_changed,
        )

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """Dispatch a tool call by name and mutate the DayPlan."""
        if name == "modify_blocks":
            return self.modify_blocks(**arguments)
        elif name == "clear_blocks":
            return self.clear_blocks(**arguments)
        elif name == "resolve_event":
            return self.resolve_event(**arguments)
        elif name == "inspect_blocks":
            return self.inspect_blocks(**arguments)
        elif name == "finalize_day_plan":
            self.day_plan.is_finalized = True
            notes = arguments.get("notes", "")
            if notes:
                self.day_plan.notes = notes
            if self.scheduler and self.scheduler.state_db:
                self.scheduler.state_db.save_day_snapshot(self.day_plan)
            return "Day plan finalized and locked in."
        return f"Unknown tool: {name}"
