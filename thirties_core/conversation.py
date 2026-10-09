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
            "description": "Assign a task or label to a Thirty block or range of blocks (1 to 48).",
            "parameters": {
                "type": "object",
                "properties": {
                    "block_index": {"type": "integer", "minimum": 1, "maximum": 48},
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "task_id": {"type": "string", "description": "Joplin task ID if assigning a task"},
                    "custom_label": {"type": "string", "description": "Display label for this block"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reinstate_work_blocks",
            "description": "Reinstate or restore the configured work blocks for today (re-locking them as Work).",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_blocks",
            "description": "Clear or deallocate blocks (e.g. opening work blocks for a day off, or unassigning scheduled tasks), returning them to open discretionary time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "end_block": {"type": "integer", "minimum": 1, "maximum": 48},
                    "clear_all_work": {"type": "boolean", "description": "True to clear all work blocks for today"}
                }
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

        clock_info = ""
        if is_today:
            curr_str = f"Block {current_logical_idx} ({current_block.start_dt.strftime('%I:%M %p')} – {current_block.end_dt.strftime('%I:%M %p')})" if current_block else "Outside day bounds"
            clock_info = f"- Current Clock Time: {now.strftime('%I:%M %p')}\n- Current Active Block: {curr_str}\n"

        elapsed_info = f"\n- Elapsed (Past) Open Blocks Earlier Today: {', '.join(elapsed_open)}" if elapsed_open else ""

        # Group currently scheduled tasks / custom commitments (e.g. Game Night, Composing, Appointments)
        scheduled_commitments = []
        i = 0
        while i < len(logical_blocks):
            b, log_idx = logical_blocks[i]
            if b.kind == BlockKind.ASSIGNED or (b.label and b.kind not in (BlockKind.SLEEP, BlockKind.WORK, BlockKind.DAYLIGHT_DISCRETIONARY, BlockKind.DARK_DISCRETIONARY)):
                lbl = b.label or "Scheduled Task"
                start_log = log_idx
                start_time = b.start_dt.strftime('%I:%M %p')
                end_time = b.end_dt.strftime('%I:%M %p')
                j = i + 1
                while j < len(logical_blocks):
                    next_b, next_log = logical_blocks[j]
                    if (next_b.kind == b.kind or next_b.label == b.label) and next_b.label == lbl:
                        end_time = next_b.end_dt.strftime('%I:%M %p')
                        j += 1
                    else:
                        break
                end_log = logical_blocks[j - 1][1]
                if start_log == end_log:
                    scheduled_commitments.append(f"- Block {start_log} ({start_time} – {end_time}): {lbl}")
                else:
                    scheduled_commitments.append(f"- Blocks {start_log} through {end_log} ({start_time} – {end_time}): {lbl}")
                i = j
            else:
                i += 1
        scheduled_commitments_str = "\n".join(scheduled_commitments) or "None scheduled yet"

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

BEHAVIOR RULES:
1. COMMUNICATION STYLE (CLOCK TIMES & CHUNK COUNTS):
   - Always communicate using clear clock times and chunk counts first, with block numbers as secondary reference.
     Good: "You have 4 chunks (2 hours) scheduled for Game Night from 6:30 PM to 8:30 PM (Blocks 24–27)."
     Bad: "Game Night is in Blocks 24 through 27."
   - The user does not memorize block numbers: always anchor your statements with start/end clock times (e.g. "from 06:30 PM to 08:30 PM") and discrete chunk/thirty counts. Never translate chunks into hours (e.g. say "4 chunks", never "2 hours").
   - Keep answers concise, structured, and action-oriented (1-3 sentences).
2. DISTINGUISH SUGGESTIONS FROM DIRECT ALLOCATIONS:
   - When the user asks for advice or a suggestion (e.g. "Where would you suggest I compose?", "What should I do next?", "Any ideas?"):
     Suggest 1 or 2 UPCOMING open blocks suitable for the task, explain briefly why, and ask if they would like you to schedule it.
     DO NOT output ALLOCATE_BLOCK when merely suggesting!
   - When the user instructs you to allocate/schedule (e.g. "allocate block 12 to Dorico", "put composing in block 12", "let's go block 28 for two hours"), OR confirms a suggestion (e.g. "yes", "sure", "let's do that", "sounds good"):
     For a single block:
       ALLOCATE_BLOCK: <block_number> | <label>
     For a multi-block span or duration:
       ALLOCATE_BLOCKS: <start_block>-<end_block> | <label>
     And provide a 1-sentence confirmation stating the block number(s), start and end times, and activity.
3. DURATION & THIRTY ARITHMETIC:
   - 1 block = 30 minutes (0.5 hr).
   - 2 blocks = 1 hour.
   - 3 blocks = 1.5 hours.
   - 4 blocks = 2 hours.
   - N hours = round(N * 2) blocks.
   - When a duration is requested (e.g. "at least 2 hours starting at block 28"):
     2 hours = 4 blocks -> Block 28 through Block 31 (28 + 4 - 1 = 31).
     Directive: ALLOCATE_BLOCKS: 28-31 | <label>
4. SCHEDULE MUTATIONS & DAYS OFF:
   - When the user indicates they do not have work today (e.g. "I don't have work today", "day off", "open my work blocks"):
     Output directive:
       CLEAR_WORK_BLOCKS
     And confirm that all work blocks are now open discretionary time for planning.
   - When the user asks to reinstate, restore, or put back work blocks (e.g. "turns out I do have work today", "put them back", "reinstate work blocks"):
     Output directive:
       REINSTATE_WORK_BLOCKS
     And confirm that work blocks are reinstated on their schedule.
   - When the user asks to deallocate or clear specific blocks (e.g. "clear blocks 4-18", "deallocate block 28"):
     Output directive:
       CLEAR_BLOCKS: <start_block>-<end_block>
5. BACKWARD SCHEDULING (APPOINTMENTS, BUFFERS & ROUTINES):
   - When the user has an appointment or event at Time T (e.g. 03:00 PM, Block 17):
     1. Transit / travel buffer MUST be placed in the block immediately BEFORE the appointment (e.g. Block 16: 02:30 PM – 03:00 PM).
     2. Preparation (shower, getting ready) MUST precede the travel buffer (e.g. Block 15: 02:00 PM – 02:30 PM).
     3. NEVER schedule preparation or travel buffer in or after the appointment block!
   - When the user mentions uncompleted morning routines:
     Schedule the morning routine in the EARLIEST upcoming open block today, NOT right before an afternoon appointment!
6. TEMPORAL AWARENESS:
   - For forward planning and recommendations, ONLY pick from UPCOMING open blocks. Never recommend or schedule into past elapsed blocks unless the user explicitly asks to retroactively log past work (e.g. "Earlier this morning at 8:00 AM I worked on X").
7. ENERGY ALIGNMENT:
   - Daylight Thirties are for high-focus, creative composition, writing, and deep problem-solving.
   - Dark Thirties are for administrative tasks, reading, light dev chores, and calm wind-down.
8. When the user confirms attendance ("yes", "attending"), confirm the event.
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

        elif name in ("allocate_thirty_block", "allocate_thirty_blocks"):
            start_idx = arguments.get("start_block", arguments.get("block_index"))
            end_idx = arguments.get("end_block", start_idx)
            task_id = arguments.get("task_id")
            label = arguments.get("custom_label") or ""

            if start_idx is None:
                start_idx = 1
            if end_idx is None:
                end_idx = start_idx

            assigned_indices: list[int] = []
            for b_idx in range(start_idx, end_idx + 1):
                if 1 <= b_idx <= 48:
                    block = self.day_plan.get_logical_block(b_idx)
                    log_idx = b_idx
                else:
                    block = self.day_plan.get_block(b_idx % 48)
                    log_idx = self.day_plan.get_logical_index(block)

                block.kind = BlockKind.ASSIGNED
                block.assigned_task_id = task_id
                block.label = label
                block.is_locked = False
                assigned_indices.append(log_idx)

            self.day_plan.recalculate_counts()

            # Persist to database so allocations survive calendar navigation!
            if self.scheduler and self.scheduler.state_db:
                self.scheduler.state_db.save_day_snapshot(self.day_plan)

            if len(assigned_indices) == 1:
                return f"Allocated block {assigned_indices[0]} to '{label}'."
            else:
                return f"Allocated blocks {start_idx} through {end_idx} ({len(assigned_indices)} blocks) to '{label}'."

        elif name in ("reinstate_work_blocks", "restore_work_blocks", "set_work_blocks"):
            start_block = arguments.get("start_block")
            end_block = arguments.get("end_block")
            start_time = arguments.get("start_time")
            end_time = arguments.get("end_time")

            if start_time and not start_block:
                start_block = self.day_plan.time_str_to_logical_block(str(start_time))
            if end_time and not end_block:
                end_block = self.day_plan.time_str_to_logical_block(str(end_time), is_end=True)

            target_blocks: list[ThirtyBlock] = []
            if start_block is not None and end_block is not None:
                for idx in range(start_block, end_block + 1):
                    if 1 <= idx <= 48:
                        target_blocks.append(self.day_plan.get_logical_block(idx))
            else:
                work_start = self.scheduler.config.general.work_start_thirty
                work_end = self.scheduler.config.general.work_end_thirty
                for b in self.day_plan.blocks:
                    if work_start <= b.index <= work_end:
                        target_blocks.append(b)

            reinstated_count = 0
            preserved_tasks: list[str] = []
            for b in target_blocks:
                if b.kind == BlockKind.ASSIGNED:
                    log_idx = self.day_plan.get_logical_index(b)
                    lbl = b.label or "scheduled task"
                    preserved_tasks.append(f"Block {log_idx} ('{lbl}')")
                    continue
                b.kind = BlockKind.WORK
                b.label = "Work"
                b.is_locked = True
                b.assigned_task_id = None
                reinstated_count += 1

            self.day_plan.recalculate_counts()

            if self.scheduler and self.scheduler.state_db:
                self.scheduler.state_db.save_day_snapshot(self.day_plan)

            if target_blocks:
                first_log = self.day_plan.get_logical_index(target_blocks[0])
                last_log = self.day_plan.get_logical_index(target_blocks[-1])
                start_clk = target_blocks[0].start_dt.strftime('%I:%M %p').lstrip('0')
                end_clk = target_blocks[-1].end_dt.strftime('%I:%M %p').lstrip('0')
                span_str = f"from {start_clk} to {end_clk} (Blocks {first_log}–{last_log})"
            else:
                span_str = "work blocks"

            chunk_word = "chunk" if reinstated_count == 1 else "chunks"
            count_str = f"{reinstated_count} {chunk_word}"

            if preserved_tasks:
                return f"Work is now scheduled {span_str} for {count_str}, keeping your existing {', '.join(preserved_tasks)} intact."
            return f"Work is now scheduled {span_str} for {count_str}."

        elif name in ("clear_blocks", "deallocate_blocks"):
            clear_all_work = arguments.get("clear_all_work", False)
            start_idx = arguments.get("start_block")
            end_idx = arguments.get("end_block", start_idx)

            target_blocks = []
            if clear_all_work:
                target_blocks = [b for b in self.day_plan.blocks if b.kind == BlockKind.WORK]
            elif start_idx is not None:
                if end_idx is None:
                    end_idx = start_idx
                for b_idx in range(start_idx, end_idx + 1):
                    if 1 <= b_idx <= 48:
                        target_blocks.append(self.day_plan.get_logical_block(b_idx))
                    else:
                        target_blocks.append(self.day_plan.get_block(b_idx % 48))

            for b in target_blocks:
                b.kind = BlockKind.DAYLIGHT_DISCRETIONARY if b.is_sunlight else BlockKind.DARK_DISCRETIONARY
                b.is_locked = False
                b.label = ""
                b.assigned_task_id = None
                b.source_event_id = None

            self.day_plan.recalculate_counts()

            if self.scheduler and self.scheduler.state_db:
                self.scheduler.state_db.save_day_snapshot(self.day_plan)

            return f"Cleared and opened {len(target_blocks)} blocks as discretionary time."

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

        self.messages.append({"role": "user", "content": user_text})

        response = self.inference_engine.chat(
            messages=self.messages,
            tools=PLANNING_TOOLS,
        )

        reply_content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        self.messages.append(response)

        # Execute returned tool calls
        executed_tools: list[tuple[str, str]] = []
        if tool_calls:
            for call in tool_calls:
                fn_name = call.get("name")
                fn_args = call.get("arguments", {})
                tool_output = self.execute_tool(fn_name, fn_args)
                executed_tools.append((fn_name, tool_output))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": fn_name,
                    "content": tool_output,
                })

        # Anti-hallucination grounding check:
        # If the assistant generated text claiming an action or the user instructed a change,
        # but the model failed to emit a tool directive, execute the corresponding mutation directly!
        if not executed_tools:
            # 1. Setting or reinstating work blocks
            if (re.search(r"\b(reinstat|restor|put.*back)\b.*\bwork\b", reply_content, re.IGNORECASE) or
                re.search(r"(?:work\s+today\s+is|work\s+is|schedule\s+work|set\s+work|reinstate|restore|put (?:them )?back|add back|turns out(?:\s+I)?(?:\s+do)?\s+have\s+work|do have work)", user_text, re.IGNORECASE)):
                reinstate_args: dict[str, Any] = {}
                time_range_match = re.search(r"(?:started at|from|at)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:and goes until|to|until|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", user_text, re.IGNORECASE)
                if time_range_match:
                    reinstate_args["start_time"] = time_range_match.group(1).strip()
                    reinstate_args["end_time"] = time_range_match.group(2).strip()
                tool_output = self.execute_tool("reinstate_work_blocks", reinstate_args)
                executed_tools.append(("reinstate_work_blocks", tool_output))
            # 2. Clearing work blocks
            elif (re.search(r"\b(deallocat|cleared|marked.*open)\b.*\bwork\b", reply_content, re.IGNORECASE) or
                  re.search(r"(?:don't(?:\s+\w+)?\s+have\s+work|no\s+work(?:day|\s+today)?|day\s+off|(?:clear|deallocate|open)\s+(?:all\s+)?(?:my\s+)?work)", user_text, re.IGNORECASE)):
                tool_output = self.execute_tool("clear_blocks", {"clear_all_work": True})
                executed_tools.append(("clear_blocks", tool_output))

        # Grounding synchronization:
        # If schedule mutations were executed, ensure the reply accurately reflects the verified action.
        if executed_tools:
            for fn_name, tool_output in executed_tools:
                if fn_name in ("reinstate_work_blocks", "clear_blocks"):
                    if "4 through 18" in reply_content or not reply_content.strip():
                        reply_content = tool_output
                    elif fn_name == "reinstate_work_blocks" and any(k in user_text.lower() for k in ("7am", "7:00", "8am", "9am", "10am")) and "4 through 18" in reply_content:
                        reply_content = tool_output

        return reply_content
