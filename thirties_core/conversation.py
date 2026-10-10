"""Conversational planning state machine and tool definitions for Thirties.

Binds on-device SLM inference with the deterministic scheduling engine,
allowing the user to negotiate their daily plan through natural language.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.astronomy import get_current_time, set_debug_time
from thirties_core.calendar_engine import CalendarEvent
from thirties_core.inference import InferenceEngine, MockInferenceEngine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.models import BlockKind, DayPlan, TaskItem
from thirties_core.scheduler import DeterministicScheduler

logger = logging.getLogger(__name__)

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
                    "clear_kind": {"type": "string", "enum": ["WORK", "SLEEP", "ALL"]},
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
            "description": "Inspect schedule occupancy, envelope kinds, labels, and times for a block range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48}
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


class ConversationManager:
    """Orchestrates multi-turn planning dialog and executes tool dispatches."""

    def __init__(
        self,
        day_plan: DayPlan,
        tasks: List[TaskItem],
        ambiguous_events: List[CalendarEvent],
        scheduler: DeterministicScheduler,
        joplin_engine: Optional[JoplinEngine] = None,
        inference_engine: Optional[InferenceEngine] = None,
    ) -> None:
        self.day_plan = day_plan
        self.tasks = tasks
        self.ambiguous_events = list(ambiguous_events)
        self.scheduler = scheduler
        self.joplin_engine = joplin_engine
        self.inference_engine = inference_engine or MockInferenceEngine()

        self.messages: List[Dict[str, Any]] = []
        self._init_conversation()

    def _build_system_prompt(self) -> str:
        sunrise_str = self.day_plan.sunrise.strftime("%I:%M %p")
        sunset_str = self.day_plan.sunset.strftime("%I:%M %p")

        # Compute index anchors (clock 0-47)
        sunrise_idx = next((b.index for b in self.day_plan.blocks if b.is_sunlight), 14)
        sunset_clock_idx = next((b.index for b in reversed(self.day_plan.blocks) if b.is_sunlight), 37)
        sunset_logical_idx = (sunset_clock_idx - sunrise_idx) % 48 + 1

        now = get_current_time(self.day_plan.sunrise.tzinfo)
        is_today = (self.day_plan.target_date == now.date())

        # Logical 1-48 blocks starting at Sunrise
        logical_blocks: list[tuple[ThirtyBlock, int]] = [
            (self.day_plan.get_block((sunrise_idx + i) % 48), i + 1) for i in range(48)
        ]

        current_block = next((b for b in self.day_plan.blocks if b.start_dt <= now < b.end_dt), None) if is_today else None
        current_logical_idx = self.day_plan.get_logical_index(current_block) if current_block else None

        upcoming_daylight = []
        upcoming_dark = []
        elapsed_open = []

        for b, log_idx in logical_blocks:
            range_str = f"Block {log_idx} ({b.start_dt.strftime('%I:%M %p')} – {b.end_dt.strftime('%I:%M %p')})"
            if is_today and b.end_dt <= now:
                if b.kind in (BlockKind.DAYLIGHT_DISCRETIONARY, BlockKind.DARK_DISCRETIONARY):
                    elapsed_open.append(range_str)
            else:
                if b.kind == BlockKind.DAYLIGHT_DISCRETIONARY:
                    upcoming_daylight.append(range_str)
                elif b.kind == BlockKind.DARK_DISCRETIONARY:
                    upcoming_dark.append(range_str)

        sleep_blocks = [
            str(log_idx)
            for b, log_idx in logical_blocks
            if b.kind == BlockKind.SLEEP
        ]
        work_blocks = [
            str(log_idx)
            for b, log_idx in logical_blocks
            if b.kind == BlockKind.WORK
        ]
        busy_blocks = [
            str(log_idx)
            for b, log_idx in logical_blocks
            if b.kind == BlockKind.BUSY_CALENDAR
        ]

        ambiguous_list = "\n".join(
            f"- [ID: {e.id}] '{e.summary}' ({e.start_dt.strftime('%I:%M %p')} - {e.end_dt.strftime('%I:%M %p')})"
            for e in self.ambiguous_events
        ) or "None"

        tasks_summary = "\n".join(
            f"- [ID: {t.id}] '{t.title}' (Notebook: {t.source_notebook}, Deferrals: {t.deferred_count})"
            for t in self.tasks[:10]
        ) or "No active backlog tasks"

        sunrise_start_str = self.day_plan.get_block(sunrise_idx).start_dt.strftime("%I:%M %p")

        # Sleep Horizon: Find the first upcoming sleep block of tonight
        sleep_blocks_logical = [log_idx for b, log_idx in logical_blocks if b.kind == BlockKind.SLEEP]
        current_idx_val = current_logical_idx if current_logical_idx is not None else 1
        upcoming_sleep = [idx for idx in sleep_blocks_logical if idx >= current_idx_val]
        sleep_start_log = upcoming_sleep[0] if upcoming_sleep else (sleep_blocks_logical[0] if sleep_blocks_logical else 33)
        sleep_start_block = self.day_plan.get_logical_block(sleep_start_log)
        sleep_start_time = sleep_start_block.start_dt.strftime('%I:%M %p').lstrip('0')
        blocks_until_sleep = max(0, sleep_start_log - current_idx_val)

        clock_info = ""
        if is_today:
            curr_str = f"Block {current_logical_idx} ({current_block.start_dt.strftime('%I:%M %p')} – {current_block.end_dt.strftime('%I:%M %p')})" if current_block else "Outside day bounds"
            clock_info = (
                f"- Current Clock Time: {now.strftime('%I:%M %p')}\n"
                f"- Current Active Block: {curr_str}\n"
                f"- Usable Time Remaining Until Sleep: {blocks_until_sleep} chunks until Sleep at {sleep_start_time} (Block {sleep_start_log})\n"
            )
        elapsed_info = f"\n- Elapsed (Past) Open Blocks Earlier Today: {', '.join(elapsed_open)}" if elapsed_open else ""

        # Group currently scheduled tasks (split into Upcoming vs Elapsed)
        upcoming_commitments = []
        past_commitments = []
        idx_loop = 0
        while idx_loop < len(logical_blocks):
            b, log_idx = logical_blocks[idx_loop]
            if b.kind == BlockKind.ASSIGNED or (b.label and b.label != "Work" and b.kind != BlockKind.SLEEP):
                lbl = b.label or "Scheduled Task"
                start_log = log_idx
                start_time = b.start_dt.strftime('%I:%M %p').lstrip('0')
                end_time = b.end_dt.strftime('%I:%M %p').lstrip('0')
                j = idx_loop + 1
                while j < len(logical_blocks):
                    next_b, next_log = logical_blocks[j]
                    if (next_b.kind == b.kind or next_b.label == b.label) and next_b.label == lbl:
                        end_time = next_b.end_dt.strftime('%I:%M %p').lstrip('0')
                        j += 1
                    else:
                        break
                end_log = logical_blocks[j - 1][1]
                count = j - idx_loop
                count_str = f"{count} chunk" if count == 1 else f"{count} chunks"
                if start_log == end_log:
                    entry = f"- Block {start_log} ({start_time} – {end_time}, {count_str}): {lbl}"
                else:
                    entry = f"- Blocks {start_log} through {end_log} ({start_time} – {end_time}, {count_str}): {lbl}"

                if is_today and b.end_dt <= now:
                    past_commitments.append(entry)
                else:
                    upcoming_commitments.append(entry)
                idx_loop = j
            else:
                idx_loop += 1

        upcoming_commitments_str = "\n".join(upcoming_commitments) or "None scheduled"
        past_commitments_str = "\n".join(past_commitments) or "None"
        scheduled_commitments_str = (
            f"- Upcoming Commitments (Later Today):\n{upcoming_commitments_str}\n\n"
            f"- Completed Commitments (Earlier Today, Elapsed):\n{past_commitments_str}"
        )

        return f"""You are the Thirties Planning Assistant. You schedule the user's day in 48 discrete thirty-minute blocks numbered 1 to 48.
The day begins at Block 1 (the thirty containing sunrise). Each subsequent block is exactly 30 minutes long.
Keep all answers concise, structured, and action-oriented (1-3 sentences). Never write creative essays or long conversational rambles.

PLANNING DATE: {self.day_plan.target_date.strftime('%A, %B %d, %Y')}

CURRENT ASTRONOMICAL CONTEXT:
TEMPORAL STATUS:
{clock_info}- Block 1 (Sunrise block): starts at {sunrise_start_str} (exact sunrise at {sunrise_str})
- Sunset: {sunset_str} (Block {sunset_logical_idx})
- Available Daylight Thirties: {self.day_plan.daylight_available_count} (out of {self.day_plan.daylight_discretionary_total} total discretionary)
- Available Dark Thirties: {self.day_plan.dark_available_count} (out of {self.day_plan.dark_discretionary_total} total discretionary)

CURRENTLY SCHEDULED TASKS & COMMITMENTS:
{scheduled_commitments_str}

AVAILABLE OPEN TIME:
- Upcoming Daylight Thirties: {', '.join(upcoming_daylight) or 'None remaining'}
- Upcoming Dark Thirties: {', '.join(upcoming_dark) or 'None remaining'}{elapsed_info}

DETERMINISTIC CONSTRAINTS:
- Sleep Blocks: {', '.join(sleep_blocks) or 'None'}
- Work Blocks: {', '.join(work_blocks) or 'None'}
- Hard Calendar Blocks: {', '.join(busy_blocks) or 'None'}

AMBIGUOUS EVENTS REQUIRING CLARIFICATION:
{ambiguous_list}

TOP BACKLOG TASKS:
{tasks_summary}

BEHAVIOR RULES & DIRECTIVES:
1. COMMUNICATION STYLE (CLOCK TIMES & CHUNK COUNTS):
   - Always communicate using clear clock times and chunk counts first, with block numbers as secondary reference.
     Good: "You have 4 chunks scheduled for Game Night from 6:30 PM to 8:30 PM (Blocks 24–27)."
     Bad: "Game Night is in Blocks 24 through 27."
   - The user does not memorize block numbers: always anchor your statements with start/end clock times (e.g. "from 06:30 PM to 08:30 PM") and discrete chunk/thirty counts. Never translate chunks into hours (e.g. say "4 chunks", never "2 hours").
   - Keep answers concise, structured, and action-oriented (1-3 sentences).

2. DISTINGUISH SUGGESTIONS FROM DIRECT ALLOCATIONS:
   - When the user asks for advice or a suggestion (e.g. "Where would you suggest I compose?", "What should I do next?", "Any ideas?"):
     Suggest 1 or 2 UPCOMING open blocks suitable for the task, explain briefly why, and ask if they would like you to schedule it.
     DO NOT output directives when merely suggesting!

3. GENERIC SCHEDULE MUTATION DIRECTIVES (Output on their own line when executing user actions):
   a) MODIFY_BLOCKS: <start>[-<end>] | [kind=<WORK|SLEEP|DISCRETIONARY>] [label=<label>] [locked=<true|false>]
      - For single block task: MODIFY_BLOCKS: 26 | label=Quick Lunch
      - For multi-block duration: MODIFY_BLOCKS: 19-22 | label=Composing
      - For work envelope: MODIFY_BLOCKS: 7-18 | kind=WORK locked=true
      - For sleep envelope: MODIFY_BLOCKS: 32-47 | kind=SLEEP locked=true
   b) CLEAR_BLOCKS: <start>[-<end>] (or CLEAR_BLOCKS: WORK)
      - Clear work blocks for day off: CLEAR_BLOCKS: WORK
      - Clear specific block: CLEAR_BLOCKS: 26
   c) RESOLVE_EVENT: <event_id> | <attend|decline>
      - Confirm attendance: RESOLVE_EVENT: unconfirmed | attend
      - Decline event: RESOLVE_EVENT: unconfirmed | decline
   d) INSPECT_BLOCKS: <start>[-<end>]
      - Inspect range: INSPECT_BLOCKS: 1-10
   e) FINALIZE_PLAN
      - Finalize day plan: FINALIZE_PLAN

4. DURATION & THIRTY ARITHMETIC:
   - 1 block = 30 minutes (0.5 hr).
   - 2 blocks = 1 hour.
   - 4 blocks = 2 hours.
   - N hours = round(N * 2) blocks.
   - When a duration is requested (e.g. "schedule block 19 for 2 hours"):
     2 hours = 4 blocks -> Blocks 19 through 22 (19 + 4 - 1 = 22).
     Directive: MODIFY_BLOCKS: 19-22 | label=Composing

5. BACKWARD SCHEDULING (APPOINTMENTS, BUFFERS & ROUTINES):
   - When the user has an appointment or event at Time T (e.g. 03:00 PM, Block 17):
     1. Transit / travel buffer MUST be placed in the block immediately BEFORE the appointment (e.g. Block 16: 02:30 PM – 03:00 PM).
     2. Preparation (shower, getting ready) MUST precede the travel buffer (e.g. Block 15: 02:00 PM – 02:30 PM).
     3. NEVER schedule preparation or travel buffer in or after the appointment block!
   - When the user mentions uncompleted morning routines:
     Schedule the morning routine in the EARLIEST upcoming open block today, NOT right before an afternoon appointment!

6. TEMPORAL AWARENESS:
   - For forward planning and recommendations, ONLY pick from UPCOMING open blocks. Never recommend or schedule into past elapsed blocks unless the user explicitly asks to retroactively log past work.

7. ENERGY ALIGNMENT:
   - Daylight Thirties are for high-focus, creative composition, writing, and deep problem-solving.
   - Dark Thirties are for administrative tasks, reading, light dev chores, and calm wind-down.
"""

    def _init_conversation(self) -> None:
        sys_prompt = self._build_system_prompt()
        self.messages = [{"role": "system", "content": sys_prompt}]

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
        **kwargs: Any,
    ) -> str:
        """Universal schedule primitive: modify block kinds, assigned tasks, or lock states."""
        if start_time and not start_block:
            start_block = self.day_plan.time_str_to_logical_block(str(start_time))
        if end_time and not end_block:
            end_block = self.day_plan.time_str_to_logical_block(str(end_time), is_end=True)

        norm_kind = kind.upper() if kind else None

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

        preserved_tasks: list[str] = []

        if norm_kind == "WORK":
            # Clear old WORK blocks outside target_indices back to discretionary
            for b in self.day_plan.blocks:
                log_idx = self.day_plan.get_logical_index(b)
                if b.kind == BlockKind.WORK and log_idx not in target_indices:
                    b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                    b.is_locked = False
                    if not b.assigned_task_id and b.label == "Work":
                        b.label = ""

            # Assign target blocks to WORK
            for b in target_blocks:
                log_idx = self.day_plan.get_logical_index(b)
                if b.label and b.label != "Work":
                    preserved_tasks.append(f"Block {log_idx} ('{b.label}')")
                else:
                    b.label = label or "Work"
                    b.assigned_task_id = task_id
                b.kind = BlockKind.WORK
                b.is_locked = True if is_locked is None else is_locked

        elif norm_kind == "SLEEP":
            # Clear old SLEEP blocks outside target_indices
            for b in self.day_plan.blocks:
                log_idx = self.day_plan.get_logical_index(b)
                if b.kind == BlockKind.SLEEP and log_idx not in target_indices:
                    b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                    b.is_locked = False
                    if b.label == "Sleep":
                        b.label = ""

            # Assign target blocks to SLEEP
            for b in target_blocks:
                b.kind = BlockKind.SLEEP
                b.label = label or "Sleep"
                b.is_locked = True if is_locked is None else is_locked
                b.assigned_task_id = None

        elif norm_kind == "DISCRETIONARY":
            for b in target_blocks:
                b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                b.is_locked = False if is_locked is None else is_locked
                b.label = label or ""
                b.assigned_task_id = task_id

        else:
            # Assign task / label while preserving diurnal container kind
            for b in target_blocks:
                if label:
                    b.label = label
                if task_id:
                    b.assigned_task_id = task_id
                if is_locked is not None:
                    b.is_locked = is_locked

        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)

        count = len(target_blocks)
        chunk_word = "chunk" if count == 1 else "chunks"
        count_str = f"{count} {chunk_word}"

        if target_blocks:
            first_log = self.day_plan.get_logical_index(target_blocks[0])
            last_log = self.day_plan.get_logical_index(target_blocks[-1])
            start_clk = target_blocks[0].start_dt.strftime('%I:%M %p').lstrip('0')
            end_clk = target_blocks[-1].end_dt.strftime('%I:%M %p').lstrip('0')
            span_str = f"from {start_clk} to {end_clk} (Block {first_log})" if first_log == last_log else f"from {start_clk} to {end_clk} (Blocks {first_log}–{last_log})"
        else:
            span_str = "requested blocks"

        if norm_kind == "WORK":
            if preserved_tasks:
                return f"Work is now scheduled {span_str} for {count_str}, keeping your existing {', '.join(preserved_tasks)} intact."
            return f"Work is now scheduled {span_str} for {count_str}."
        elif norm_kind == "SLEEP":
            return f"Sleep window is now scheduled {span_str} for {count_str}."
        else:
            lbl_desc = f"'{label}'" if label else "scheduled"
            if len(target_blocks) == 1:
                return f"Allocated block {first_log} from {start_clk} to {end_clk} (1 chunk) to {lbl_desc}."
            else:
                return f"Allocated blocks {first_log} through {last_log} from {start_clk} to {end_clk} ({count_str}) to {lbl_desc}."

    def clear_blocks(
        self,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
        clear_kind: Optional[str] = None,
        clear_all_work: bool = False,
        **kwargs: Any,
    ) -> str:
        """Clear blocks back to open discretionary time."""
        target_blocks: list[ThirtyBlock] = []
        norm_kind = clear_kind.upper() if clear_kind else None

        if norm_kind == "WORK" or clear_all_work:
            target_blocks = [b for b in self.day_plan.blocks if b.kind == BlockKind.WORK]
        elif norm_kind == "SLEEP":
            target_blocks = [b for b in self.day_plan.blocks if b.kind == BlockKind.SLEEP]
        elif start_block is not None:
            if end_block is None:
                end_block = start_block
            for b_idx in range(start_block, end_block + 1):
                if 1 <= b_idx <= 48:
                    target_blocks.append(self.day_plan.get_logical_block(b_idx))

        for b in target_blocks:
            b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
            b.is_locked = False
            b.label = ""
            b.assigned_task_id = None
            b.source_event_id = None

        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)

        count = len(target_blocks)
        count_str = f"{count} block" if count == 1 else f"{count} blocks"
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

        for block in self.day_plan.blocks:
            if block.source_event_id == event_id or (event_id in ("unconfirmed", "*") and block.source_event_id):
                if is_attending:
                    block.kind = BlockKind.BUSY_CALENDAR
                    block.is_locked = True
                else:
                    block.kind = BlockKind.DAYLIGHT_DISCRETIONARY if block.is_sunlight else BlockKind.DARK_DISCRETIONARY
                    block.label = ""
                    block.source_event_id = None
                    block.is_locked = False

        self.ambiguous_events = [e for e in self.ambiguous_events if e.id != event_id and (event_id != "unconfirmed")]
        self.day_plan.recalculate_counts()
        if self.scheduler and self.scheduler.state_db:
            self.scheduler.state_db.save_day_snapshot(self.day_plan)
        action_desc = "locked as busy calendar block" if is_attending else "declined and opened as discretionary"
        return f"Event {event_id} resolved: {action_desc}."

    def inspect_blocks(
        self,
        start_block: int = 1,
        end_block: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """Inspect schedule occupancy, envelope kinds, labels, and times for a block range."""
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

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Dispatch model tool call and mutate local DayPlan / Joplin state."""
        if name == "modify_blocks":
            return self.modify_blocks(**arguments)

        elif name in ("clear_blocks", "deallocate_blocks"):
            return self.clear_blocks(**arguments)

        elif name in ("resolve_event", "resolve_calendar_event"):
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

        # Backward compatibility aliases for existing tests
        elif name in ("allocate_thirty_block", "allocate_thirty_blocks"):
            start_idx = arguments.get("start_block", arguments.get("block_index"))
            end_idx = arguments.get("end_block", start_idx)
            task_id = arguments.get("task_id")
            label = arguments.get("custom_label") or arguments.get("label", "")
            return self.modify_blocks(start_block=start_idx, end_block=end_idx, label=label, task_id=task_id)

        elif name in ("reinstate_work_blocks", "restore_work_blocks", "set_work_blocks"):
            return self.modify_blocks(
                start_block=arguments.get("start_block"),
                end_block=arguments.get("end_block"),
                start_time=arguments.get("start_time"),
                end_time=arguments.get("end_time"),
                kind="WORK",
            )

        elif name in ("set_sleep_blocks", "adjust_sleep_window", "set_bedtime"):
            return self.modify_blocks(
                start_block=arguments.get("start_block"),
                end_block=arguments.get("end_block"),
                start_time=arguments.get("start_time"),
                end_time=arguments.get("end_time"),
                kind="SLEEP",
            )

        elif name == "decompose_task":
            parent_id = arguments["parent_task_id"]
            subtasks = arguments.get("subtasks", [])
            return f"Task {parent_id} decomposed into {len(subtasks)} subtasks."

        elif name == "create_calendar_entry":
            return f"Calendar entry created: {arguments.get('summary')}."

        return f"Unknown tool: {name}"

    def send_user_message(self, user_text: str) -> str:
        """Process user input turn, execute model tool calls, and return reply."""
        # Developer / Runtime Simulation Slash Commands
        clean_text = user_text.strip()
        if clean_text.startswith("/"):
            cmd = clean_text.lower()
            if cmd == "/night":
                set_debug_time("22:30")
                reply = "Simulated time set to 10:30 PM (Nighttime Mode). The Solar Arc has shifted to the Nocturnal Lunar Arc."
            elif cmd == "/midnight":
                set_debug_time("00:00")
                reply = "Simulated time set to 12:00 AM (Midnight Mode)."
            elif cmd in ("/day", "/midday"):
                day_sec = (self.day_plan.sunset - self.day_plan.sunrise).total_seconds()
                midday_dt = self.day_plan.sunrise + timedelta(seconds=day_sec / 2.0)
                set_debug_time(midday_dt.strftime("%H:%M"))
                reply = f"Simulated time set to daytime solar midday ({midday_dt.strftime('%I:%M %p').lstrip('0')})."
            elif cmd == "/noon":
                set_debug_time("12:00")
                reply = "Simulated time set to 12:00 PM (Clock Noon Mode)."
            elif cmd in ("/morning", "/sunrise"):
                set_debug_time(self.day_plan.sunrise.strftime("%H:%M"))
                reply = f"Simulated time set to sunrise ({self.day_plan.sunrise.strftime('%I:%M %p').lstrip('0')})."
            elif cmd in ("/reset", "/now"):
                set_debug_time(None)
                reply = "Simulated time cleared. Real-world system clock restored."
            elif cmd.startswith("/time"):
                val = clean_text[5:].strip()
                set_debug_time(val)
                reply = f"Simulated time set to {val}."
            else:
                reply = f"Unknown command: {clean_text}. Available: /night, /midnight, /day, /midday, /noon, /sunrise, /reset, /time HH:MM"

            self.messages.append({"role": "user", "content": user_text})
            self.messages.append({"role": "assistant", "content": reply})
            return reply

        logger.info("[Conversation] User message: %r", user_text)
        self.messages.append({"role": "user", "content": user_text})

        response = self.inference_engine.chat(
            messages=self.messages,
            tools=PLANNING_TOOLS,
        )

        reply_content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])
        logger.debug("[Conversation] Model output: %r (tool_calls=%r)", reply_content, tool_calls)

        self.messages.append(response)

        # Execute returned tool calls
        executed_tools: list[tuple[str, str]] = []
        if tool_calls:
            for call in tool_calls:
                fn_name = call.get("name")
                fn_args = call.get("arguments", {})
                logger.info("[Conversation] Executing tool: %s with args: %s", fn_name, fn_args)
                tool_output = self.execute_tool(fn_name, fn_args)
                logger.info("[Conversation] Tool %s result: %s", fn_name, tool_output)
                executed_tools.append((fn_name, tool_output))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": fn_name,
                    "content": tool_output,
                })

        if not reply_content.strip():
            if executed_tools:
                reply_content = executed_tools[-1][1]
            else:
                reply_content = "Understood. Let me know if you would like me to adjust any of your upcoming blocks."

        logger.info("[Conversation] Final assistant reply: %r", reply_content)
        return reply_content
