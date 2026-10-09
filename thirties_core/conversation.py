"""Conversational planning state machine and tool definitions for Thirties.

Binds on-device SLM inference with the deterministic scheduling engine,
allowing the user to negotiate their daily plan through natural language.
"""

from __future__ import annotations

import json
import logging
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

        # Compute index anchors
        sunrise_idx = next((b.index for b in self.day_plan.blocks if b.is_sunlight), 14)
        sunset_idx = next((b.index for b in reversed(self.day_plan.blocks) if b.is_sunlight), 37)

        sleep_indices = [str(b.index) for b in self.day_plan.blocks if b.kind == BlockKind.SLEEP]
        work_indices = [str(b.index) for b in self.day_plan.blocks if b.kind == BlockKind.WORK]
        busy_indices = [str(b.index) for b in self.day_plan.blocks if b.kind == BlockKind.BUSY_CALENDAR]

        ambiguous_list = "\n".join(
            f"- [ID: {e.id}] '{e.summary}' ({e.start_dt.strftime('%I:%M %p')} - {e.end_dt.strftime('%I:%M %p')})"
            for e in self.ambiguous_events
        ) or "None"

        tasks_summary = "\n".join(
            f"- [ID: {t.id}] '{t.title}' (Notebook: {t.source_notebook}, Deferrals: {t.deferred_count})"
            for t in self.tasks[:10]
        ) or "No active backlog tasks"

        return f"""You are the 30s Diurnal Planning Agent. You organize the user's day into 48 discrete 30-minute intervals (0-47).
You do not speak in granular minutes or seconds. You speak exclusively in units of "Thirties", Daylight Thirties, and Dark Thirties.

CURRENT ASTRONOMICAL CONTEXT:
- Sunrise: {sunrise_str} (Block {sunrise_idx})
- Sunset: {sunset_str} (Block {sunset_idx})
- Available Daylight Thirties: {self.day_plan.daylight_available_count}
- Available Dark Thirties: {self.day_plan.dark_available_count}

DETERMINISTIC CONSTRAINTS:
- Sleep Blocks: {', '.join(sleep_indices)}
- Work Blocks: {', '.join(work_indices)}
- Hard Calendar Blocks: {', '.join(busy_indices) or 'None'}

AMBIGUOUS EVENTS REQUIRING CLARIFICATION:
{ambiguous_list}

TOP BACKLOG TASKS:
{tasks_summary}

BEHAVIOR RULES:
1. Always resolve ambiguous calendar commitments first by asking the user directly.
2. Respect the user's daily energy level. High focus and creative composition work belongs in Daylight Thirties; administrative tasks, reading, and light dev tasks fit into Dark Thirties.
3. If a task has been deferred >= 3 times, actively suggest breaking it down into smaller subtasks or deferring it back to the Joplin backlog.
4. Execute tool calls to assign blocks or adjust calendar entries when the user agrees. Never hallucinate available time.
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
            idx = arguments["block_index"]
            task_id = arguments.get("task_id")
            label = arguments.get("custom_label") or ""

            block = self.day_plan.get_block(idx)
            block.kind = BlockKind.ASSIGNED
            block.assigned_task_id = task_id
            block.label = label
            self.day_plan.recalculate_counts()
            return f"Allocated block {idx} to '{label}'."

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
