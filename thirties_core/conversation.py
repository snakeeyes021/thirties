"""Conversational planning state machine and tool definitions for Thirties.

Binds on-device SLM inference with the deterministic scheduling engine,
allowing the user to negotiate their daily plan through natural language.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
            "name": "resolve_calendar_event",
            "description": "Mark an ambiguous calendar event as attended (hard block) or ignored.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The unique event ID"},
                    "attending": {"type": "boolean", "description": "True if attending, False if declining"}
                },
                "required": ["event_id", "attending"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "allocate_thirty_block",
            "description": "Assign a task or label to a specific Thirty index (0 to 47).",
            "parameters": {
                "type": "object",
                "properties": {
                    "block_index": {"type": "integer", "minimum": 0, "maximum": 47},
                    "task_id": {"type": "string", "description": "Joplin task ID if assigning a task"},
                    "custom_label": {"type": "string", "description": "Display label for this block"}
                },
                "required": ["block_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_entry",
            "description": "Add an appointment or hard block to Google / GNOME Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["summary", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "decompose_task",
            "description": "Break a heavily deferred task into smaller subtasks in Joplin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_task_id": {"type": "string"},
                    "subtasks": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["parent_task_id", "subtasks"]
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

        now = datetime.now(self.day_plan.sunrise.tzinfo)
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

        clock_info = ""
        if is_today:
            curr_str = f"Block {current_logical_idx} ({current_block.start_dt.strftime('%I:%M %p')} – {current_block.end_dt.strftime('%I:%M %p')})" if current_block else "Outside day bounds"
            clock_info = f"- Current Clock Time: {now.strftime('%I:%M %p')}\n- Current Active Block: {curr_str}\n"

        elapsed_info = f"\n- Elapsed (Past) Open Blocks Earlier Today: {', '.join(elapsed_open)}" if elapsed_open else ""

        return f"""You are the Thirties Planning Assistant. You schedule the user's day in 48 discrete thirty-minute blocks numbered 1 to 48.
The day begins at Block 1 (the thirty containing sunrise). Each subsequent block is exactly 30 minutes long.
Keep all answers concise, structured, and action-oriented (1-3 sentences). Never write creative essays or long conversational rambles.

PLANNING DATE: {self.day_plan.target_date.strftime('%A, %B %d, %Y')}

CURRENT ASTRONOMICAL CONTEXT:
TEMPORAL STATUS:
{clock_info}- Block 1 (Sunrise block): starts at {sunrise_start_str} (exact sunrise at {sunrise_str})
- Sunset: {sunset_str} (Block {sunset_logical_idx})
- Available Daylight Thirties: {self.day_plan.daylight_available_count}
- Available Dark Thirties: {self.day_plan.dark_available_count}

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

BEHAVIOR RULES:
1. You are a concise, structured day planning assistant (1-3 sentences per response).
2. DISTINGUISH SUGGESTIONS FROM DIRECT ALLOCATIONS:
   - When the user asks for advice or a suggestion (e.g. "Where would you suggest I compose?", "What should I do next?", "Any ideas?"):
     Suggest 1 or 2 UPCOMING open blocks suitable for the task, explain briefly why, and ask if they would like you to schedule it.
     DO NOT output ALLOCATE_BLOCK when merely suggesting!
   - When the user instructs you to allocate/schedule (e.g. "allocate block 12 to Dorico", "put composing in block 12"), OR confirms a suggestion (e.g. "yes", "sure", "let's do that", "sounds good"):
     Output the allocation directive on its own line:
       ALLOCATE_BLOCK: <block_number> | <label>
     And provide a 1-sentence confirmation stating the block number, start time, and activity.
3. TEMPORAL AWARENESS:
   - For forward planning and recommendations, ONLY pick from UPCOMING open blocks. Never recommend or schedule into past elapsed blocks unless the user explicitly asks to retroactively log past work (e.g. "Earlier this morning at 8:00 AM I worked on X").
4. ENERGY ALIGNMENT:
   - Daylight Thirties are for high-focus, creative composition, writing, and deep problem-solving.
   - Dark Thirties are for administrative tasks, reading, light dev chores, and calm wind-down.
5. When the user confirms attendance ("yes", "attending"), confirm the event.
"""

    def _init_conversation(self) -> None:
        sys_prompt = self._build_system_prompt()
        self.messages = [{"role": "system", "content": sys_prompt}]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Dispatch model tool call and mutate local DayPlan / Joplin state."""
        if name == "resolve_calendar_event":
            event_id = arguments["event_id"]
            attending = arguments["attending"]

            # Update blocks associated with this event
            for block in self.day_plan.blocks:
                if block.source_event_id == event_id:
                    if attending:
                        block.kind = BlockKind.BUSY_CALENDAR
                        block.is_locked = True
                    else:
                        block.kind = BlockKind.DAYLIGHT_DISCRETIONARY if block.is_sunlight else BlockKind.DARK_DISCRETIONARY
                        block.label = ""
                        block.source_event_id = None
                        block.is_locked = False

            self.ambiguous_events = [e for e in self.ambiguous_events if e.id != event_id]
            self.day_plan.recalculate_counts()
            action_desc = "locked as busy calendar block" if attending else "ignored and opened as discretionary"
            return f"Event {event_id} resolved: {action_desc}."

        elif name == "allocate_thirty_block":
            raw_idx = arguments["block_index"]
            task_id = arguments.get("task_id")
            label = arguments.get("custom_label") or ""

            if 1 <= raw_idx <= 48:
                block = self.day_plan.get_logical_block(raw_idx)
                logical_idx = raw_idx
            else:
                block = self.day_plan.get_block(raw_idx % 48)
                logical_idx = self.day_plan.get_logical_index(block)

            block.kind = BlockKind.ASSIGNED
            block.assigned_task_id = task_id
            block.label = label
            self.day_plan.recalculate_counts()

            # Persist to database so allocations survive calendar navigation!
            if self.scheduler and self.scheduler.state_db:
                self.scheduler.state_db.save_day_snapshot(self.day_plan)

            return f"Allocated block {logical_idx} to '{label}'."

        elif name == "decompose_task":
            parent_id = arguments["parent_task_id"]
            subtasks = arguments.get("subtasks", [])
            return f"Task {parent_id} decomposed into {len(subtasks)} subtasks."

        elif name == "finalize_day_plan":
            notes = arguments.get("notes", "")
            self.day_plan.notes = notes
            self.scheduler.finalize_day_plan(self.day_plan)
            return "Day plan finalized and locked in."

        return f"Unknown tool: {name}"

    def send_user_message(self, user_text: str) -> str:
        """Process user input turn, execute model tool calls, and return reply."""
        self.messages.append({"role": "user", "content": user_text})

        response = self.inference_engine.chat(
            messages=self.messages,
            tools=PLANNING_TOOLS,
        )

        reply_content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        self.messages.append(response)

        # Execute returned tool calls
        if tool_calls:
            for call in tool_calls:
                fn_name = call.get("name")
                fn_args = call.get("arguments", {})
                tool_output = self.execute_tool(fn_name, fn_args)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": fn_name,
                    "content": tool_output,
                })

        return reply_content
