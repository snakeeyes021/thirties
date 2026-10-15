"""Frontier brain: drives a large model (Gemini today) with one rich prompt.

The model sees the whole plan and the full Joplin backlog, and acts by emitting
tool calls that ``PlanTools`` executes. After each turn the brain checks the
model's claims against the real state change and asks it to correct itself
when they disagree.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.inference import InferenceEngine
from thirties_core.models import BlockKind, TaskItem, ThirtyBlock
from thirties_core.plan_tools import PLANNING_TOOLS, PlanTools

logger = logging.getLogger(__name__)


ACTION_CLAIM_PATTERN = re.compile(
    r"\b(?:i\s+have|i\'ve|i\s+did|i)\s+(?:(?:\w+ly|now|already|just)\s+)?(?:cleared|scheduled|allocated|updated|set|assigned|removed|reverted|corrected|locked|finalized|put|placed)\b"
    r"|\b(?:plan|schedule|work\s+shift|blocks?)\s+(?:has\s+been\s+)?(?:updated|cleared|scheduled|finalized|locked|set)\b"
    r"|\bday\s+plan\s+finalized\b",
    re.IGNORECASE,
)


class FrontierBrain:
    """Single-prompt brain for large-context frontier models."""

    def __init__(
        self,
        tools: PlanTools,
        inference_engine: InferenceEngine,
        tasks: Optional[List[TaskItem]] = None,
        active_notes: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.tools = tools
        self.inference_engine = inference_engine
        self.tasks = list(tasks or [])
        self.active_notes = list(active_notes or [])
        self.messages: List[Dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        """Start a fresh transcript with an up-to-date system prompt."""
        self.messages = [{"role": "system", "content": self.build_system_prompt()}]

    def build_system_prompt(self) -> str:
        sunrise_str = self.tools.day_plan.sunrise.strftime("%I:%M %p")
        sunset_str = self.tools.day_plan.sunset.strftime("%I:%M %p")

        # Compute index anchors (clock 0-47)
        sunrise_idx = next((b.index for b in self.tools.day_plan.blocks if b.is_sunlight), 14)
        sunset_clock_idx = next((b.index for b in reversed(self.tools.day_plan.blocks) if b.is_sunlight), 37)
        sunset_logical_idx = (sunset_clock_idx - sunrise_idx) % 48 + 1

        now = get_current_time(self.tools.day_plan.sunrise.tzinfo)
        gen = self.tools.scheduler.config.general if self.tools.scheduler else None
        lat = gen.latitude if gen else 40.7128
        lon = gen.longitude if gen else -74.0060
        diurnal_today = get_diurnal_date(now, lat=lat, lon=lon, tz_name=self.tools.day_plan.sunrise.tzinfo)
        is_today = (self.tools.day_plan.target_date == diurnal_today)

        # Logical 1-48 blocks starting at Sunrise
        logical_blocks: list[tuple[ThirtyBlock, int]] = [
            (self.tools.day_plan.get_block((sunrise_idx + i) % 48), i + 1) for i in range(48)
        ]

        current_block = next((b for b in self.tools.day_plan.blocks if b.start_dt <= now < b.end_dt), None) if is_today else None
        current_logical_idx = self.tools.day_plan.get_logical_index(current_block) if current_block else None

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
            for e in self.tools.ambiguous_events
        ) or "None"

        tasks_summary = self.build_backlog_summary()

        sunrise_start_str = self.tools.day_plan.get_block(sunrise_idx).start_dt.strftime("%I:%M %p")

        # Sleep Horizon: Find the first upcoming sleep block of tonight
        sleep_blocks_logical = [log_idx for b, log_idx in logical_blocks if b.kind == BlockKind.SLEEP]
        current_idx_val = current_logical_idx if current_logical_idx is not None else 1
        upcoming_sleep = [idx for idx in sleep_blocks_logical if idx >= current_idx_val]
        sleep_start_log = upcoming_sleep[0] if upcoming_sleep else (sleep_blocks_logical[0] if sleep_blocks_logical else 33)
        sleep_start_block = self.tools.day_plan.get_logical_block(sleep_start_log)
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

        planning_date_desc = (
            f"Today ({self.tools.day_plan.target_date.strftime('%A, %B %d, %Y')})"
            if is_today
            else self.tools.day_plan.target_date.strftime('%A, %B %d, %Y')
        )

        return f"""You are the Thirties Planning Assistant. You schedule the user's day in 48 discrete thirty-minute blocks numbered 1 to 48.
The day begins at Block 1 (the thirty containing sunrise). Each subsequent block is exactly 30 minutes long.
Keep all answers concise, structured, and action-oriented (1-3 sentences). Never write creative essays or long conversational rambles.

PLANNING DATE: {planning_date_desc}

CURRENT ASTRONOMICAL CONTEXT:
TEMPORAL STATUS:
{clock_info}- Block 1 (Sunrise block): starts at {sunrise_start_str} (exact sunrise at {sunrise_str})
- Sunset: {sunset_str} (Block {sunset_logical_idx})
- Available Daylight Thirties: {self.tools.day_plan.daylight_available_count} (out of {self.tools.day_plan.daylight_discretionary_total} total discretionary)
- Available Dark Thirties: {self.tools.day_plan.dark_available_count} (out of {self.tools.day_plan.dark_discretionary_total} total discretionary)

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
   a) MODIFY_BLOCKS: <start>[-<end>] OR <start_time> - <end_time> | [kind=<WORK|SLEEP|DISCRETIONARY>] [label=<label>] [locked=<true|false>]
      - For single block task: MODIFY_BLOCKS: 26 | label=Quick Lunch
      - For multi-block duration: MODIFY_BLOCKS: 19-22 | label=Composing
      - For clock times (PREFER clock times when the user specifies clock times to let the engine resolve exact boundaries!):
        MODIFY_BLOCKS: 07:00 AM - 03:00 PM | kind=WORK
        MODIFY_BLOCKS: 7am - 3pm | kind=WORK
      - For work envelope: MODIFY_BLOCKS: 7-18 | kind=WORK locked=true
      - For sleep envelope: MODIFY_BLOCKS: 32-47 | kind=SLEEP locked=true
   b) CLEAR_BLOCKS: <start>[-<end>] OR <start_time> - <end_time> (or CLEAR_BLOCKS: WORK)
      - Clear work blocks for day off: CLEAR_BLOCKS: WORK
      - Clear specific block: CLEAR_BLOCKS: 26
      - Clear clock range: CLEAR_BLOCKS: 02:00 PM - 03:00 PM
   c) RESOLVE_EVENT: <event_id> | <attend|decline>
      - Confirm attendance: RESOLVE_EVENT: unconfirmed | attend
      - Decline event: RESOLVE_EVENT: unconfirmed | decline
   d) INSPECT_BLOCKS: <start>[-<end>] OR <start_time> - <end_time>
      - Inspect range: INSPECT_BLOCKS: 1-10
      - Inspect time range: INSPECT_BLOCKS: 02:00 PM - 04:00 PM
   e) FINALIZE_PLAN
      - ONLY emit FINALIZE_PLAN when the user explicitly confirms they are finished planning or asks to lock in their plan (e.g. "looks good", "finalize my plan", "all set").
      - NEVER emit FINALIZE_PLAN on an initial turn, during ongoing negotiation, or when merely updating work/sleep hours!

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

8. CONTRACT MUTATION INTEGRITY & BOUNDARIES:
   - YOU CANNOT CHANGE THE SCHEDULE THROUGH POLITE WORDS ALONE! If you claim in your response to have cleared, scheduled, or updated blocks, you MUST emit the corresponding directive (e.g. CLEAR_BLOCKS, MODIFY_BLOCKS). Never claim an action occurred without emitting the directive.
   - DO NOT automatically schedule backlog tasks across open blocks unless the user explicitly requests you to schedule tasks or asks you to fill open time. When the user asks to modify work or clear blocks, ONLY perform that specific action!

9. JOPLIN BACKLOG PRIORITIZATION & TAXONOMIC AWARENESS:
   - You have full, transparent visibility into the user's active Joplin notebooks, notes, and checklist items.
   - DISTINGUISH CONCRETE ACTIONS FROM REFERENCE LISTS & PROJECT BUCKETS:
     * Never suggest a reference wishlist (like 'Shopping List') as an errand to do today!
     * Never suggest a generic note title (like 'Errands/Admin' or 'Wedding To-Do') as an action—always recommend the SPECIFIC concrete action inside the note (e.g. 'Costco customer service' or 'Print out recipes').
     * Creative & Dev tasks (from '3. Creative', '2. Dev'): Map into upcoming Daylight Thirties.
     * Admin & Chore tasks (from '1. Tasks', '4. Media', '5. Misc.'): Map into upcoming Dark Thirties.
     * For Chronically Deferred items (3+ deferrals), offer honest tough love: advise tackling just 1 single block (30 minutes) to break avoidance, breaking it down, or deleting it.
"""

    def build_backlog_summary(self) -> str:
        """Construct comprehensive Joplin backlog presentation for Gemini."""
        if not self.tasks and not self.active_notes:
            return "No active backlog tasks or notes found."

        # Map tasks by parent_note_id
        tasks_by_note: Dict[str, List[TaskItem]] = {}
        standalone_tasks: List[TaskItem] = []
        for t in self.tasks:
            if t.parent_note_id:
                tasks_by_note.setdefault(t.parent_note_id, []).append(t)
            else:
                standalone_tasks.append(t)

        # 1. Flag chronically deferred tasks first (tough love)
        chronic_lines = []
        for t in self.tasks:
            if t.deferred_count >= 3:
                parent_str = f" (in note '{t.parent_note_title}')" if t.parent_note_title else ""
                chronic_lines.append(
                    f"- [ID: {t.id}] '{t.title}'{parent_str} [Notebook: {t.source_notebook}] [CHRONIC DEFERRAL: {t.deferred_count}x]"
                )

        sections = []
        if chronic_lines:
            sections.append(
                "🔥 CHRONICALLY DEFERRED ITEMS (Urgent / Needs Tough Love or Single 30-Min Chunk):\n"
                + "\n".join(chronic_lines)
            )

        # 2. Present notes organized by notebook hierarchy
        notes_by_notebook: Dict[str, List[Dict[str, Any]]] = {}
        for note in self.active_notes:
            notes_by_notebook.setdefault(note["source_notebook"], []).append(note)

        # Include standalone tasks if any
        for t in standalone_tasks:
            if not any(n["id"] == t.id for n in self.active_notes):
                notes_by_notebook.setdefault(t.source_notebook, []).append({
                    "id": t.id,
                    "source_notebook": t.source_notebook,
                    "title": t.title,
                    "body": t.description,
                    "is_todo": True,
                    "todo_completed": 0,
                })

        backlog_sections = []
        for nb_path in sorted(notes_by_notebook.keys()):
            nb_notes = notes_by_notebook[nb_path]
            nb_block = [f"=== NOTEBOOK: {nb_path} ==="]

            for note in nb_notes:
                n_id = note["id"]
                title = note["title"]
                body = note.get("body", "")
                is_todo = note.get("is_todo", False)
                todo_comp = note.get("todo_completed", 0)

                note_tasks = tasks_by_note.get(n_id, [])

                # Classify note intent (Reference list vs Actionable note vs Routine)
                lower_title = title.lower()
                if "routine" in lower_title:
                    note_type_badge = " [Daily Routine Checklist]"
                elif "shopping" in lower_title or "wishlist" in lower_title or "purchas" in lower_title:
                    note_type_badge = " [Reference / Purchasing Wishlist - NOT an immediate daily errand]"
                elif "errand" in lower_title or "admin" in lower_title:
                    note_type_badge = " [Active Admin & Logistics Bucket]"
                elif is_todo:
                    note_type_badge = " [To-Do Note]"
                else:
                    note_type_badge = " [Note]"

                nb_block.append(f"### Note: \"{title}\"{note_type_badge}")

                if note_tasks:
                    nb_block.append("  Active Checklist Items:")
                    for t in note_tasks:
                        def_str = f" [Deferred: {t.deferred_count}x]" if t.deferred_count > 0 else ""
                        nb_block.append(f"  - [ID: {t.id}] '{t.title}'{def_str}")
                elif is_todo and todo_comp == 0:
                    nb_block.append(f"  - [ID: {n_id}] Single Actionable Item: '{title}'")
                    if body.strip():
                        excerpt = "\n    ".join(body.strip().splitlines()[:5])
                        nb_block.append(f"    Details:\n    {excerpt}")
                elif body.strip():
                    clean_lines = [l for l in body.splitlines() if l.strip()]
                    if clean_lines:
                        excerpt = "\n    ".join(clean_lines[:8])
                        nb_block.append(f"    Contents:\n    {excerpt}")

                nb_block.append("")

            backlog_sections.append("\n".join(nb_block))

        if backlog_sections:
            sections.append("ACTIVE JOPLIN NOTEBOOK HIERARCHY & TASK BACKLOG:\n" + "\n\n".join(backlog_sections))
        elif self.tasks:
            # Fallback if active_notes was empty
            task_lines = [
                f"- [ID: {t.id}] '{t.title}' [Notebook: {t.source_notebook}]"
                for t in self.tasks[:50]
            ]
            sections.append("ACTIVE TASKS:\n" + "\n".join(task_lines))

        return "\n\n".join(sections)

    def _detect_discrepancy(self, draft_text: str, tool_output: str) -> bool:
        """Check for factual contradictions between model draft text and executed tool output."""
        if not draft_text or not tool_output:
            return False

        # 1. Chunk count mismatch
        t_cm = re.search(r"(\d+)\s+chunks?", tool_output, re.IGNORECASE)
        d_cm = re.search(r"(\d+)\s+chunks?", draft_text, re.IGNORECASE)
        if t_cm and d_cm:
            if int(t_cm.group(1)) != int(d_cm.group(1)):
                return True

        # 2. Block range mismatch (e.g. tool scheduled Blocks 1–16, but model claimed Blocks 1-15)
        t_bm = re.search(r"Blocks?\s+(\d+)(?:\s*(?:–|-|through)\s*(\d+))?", tool_output, re.IGNORECASE)
        d_bm = re.search(r"Blocks?\s+(\d+)(?:\s*(?:–|-|through)\s*(\d+))?", draft_text, re.IGNORECASE)
        if t_bm and d_bm:
            t_start, t_end = int(t_bm.group(1)), int(t_bm.group(2) or t_bm.group(1))
            d_start, d_end = int(d_bm.group(1)), int(d_bm.group(2) or d_bm.group(1))
            if (t_start, t_end) != (d_start, d_end):
                return True

        return False

    def respond(self, user_text: str) -> str:
        """Run one user turn: call the model, execute its tool calls, verify, reply."""
        # 1. Capture snapshot before turn
        snapshot_before = self.tools.capture_snapshot()

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

        # 2. Execute returned tool calls
        executed_tools: list[tuple[str, str]] = []
        if tool_calls:
            for call in tool_calls:
                fn_name = call.get("name")
                fn_args = call.get("arguments", {})
                logger.info("[Conversation] Executing tool: %s with args: %s", fn_name, fn_args)
                tool_output = self.tools.execute(fn_name, fn_args)
                logger.info("[Conversation] Tool %s result: %s", fn_name, tool_output)
                executed_tools.append((fn_name, tool_output))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": fn_name,
                    "content": tool_output,
                })

        snapshot_after = self.tools.capture_snapshot()
        delta = self.tools.compute_delta(snapshot_before, snapshot_after)

        claims_mutation = bool(ACTION_CLAIM_PATTERN.search(reply_content))
        contract_violation = (claims_mutation and delta.total_changes == 0)

        needs_follow_up = False
        if any(fn_name == "inspect_blocks" for fn_name, _ in executed_tools):
            needs_follow_up = True
        elif executed_tools and not reply_content.strip():
            needs_follow_up = True
        elif contract_violation:
            needs_follow_up = True
            logger.warning("[Conversation] Contract Violation: Model claimed mutation '%s' but delta was 0. Triggering reflection turn.", reply_content)
            self.messages.append({
                "role": "user",
                "content": (
                    "[CONTRACT VERIFICATION NOTICE]: You stated to the user that you cleared, scheduled, or updated the plan, "
                    "but you emitted NO tool directives and no schedule state was changed (state delta = 0). "
                    "You CANNOT change the schedule through conversational statements alone. "
                    "You MUST output explicit tool directives (e.g. CLEAR_BLOCKS: ALL, MODIFY_BLOCKS: ...) "
                    "on their own line to execute changes. Output the required directive now."
                )
            })
        elif executed_tools and reply_content.strip():
            for _, t_out in executed_tools:
                if self._detect_discrepancy(reply_content, t_out):
                    logger.info("[Conversation] Discrepancy detected between draft reply and tool output. Initiating self-correction turn.")
                    needs_follow_up = True
                    break

        if needs_follow_up:
            follow_up = self.inference_engine.chat(
                messages=self.messages,
                tools=PLANNING_TOOLS,
            )
            follow_up_content = follow_up.get("content", "")
            follow_up_calls = follow_up.get("tool_calls", [])
            self.messages.append(follow_up)
            if follow_up_calls:
                for call in follow_up_calls:
                    fn_name = call.get("name")
                    fn_args = call.get("arguments", {})
                    tool_output = self.tools.execute(fn_name, fn_args)
                    executed_tools.append((fn_name, tool_output))
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", "call"),
                        "name": fn_name,
                        "content": tool_output,
                    })
                # Re-compute delta after follow-up calls
                snapshot_after = self.tools.capture_snapshot()
                delta = self.tools.compute_delta(snapshot_before, snapshot_after)

            if follow_up_content.strip():
                reply_content = follow_up_content

        # Final Verifier Guard: If the final reply STILL claims mutation but delta.total_changes == 0,
        # prevent lying to the user
        if ACTION_CLAIM_PATTERN.search(reply_content) and delta.total_changes == 0:
            logger.warning("[Conversation] Final Verifier Guard: Suppressing unverified mutation claim: %r", reply_content)
            reply_content = "I was unable to update your schedule because no valid modification directive could be executed. Please specify the blocks or time range you would like to change."

        if not reply_content.strip():
            if executed_tools:
                reply_content = executed_tools[-1][1]
            else:
                reply_content = "Understood. Let me know if you would like me to adjust any of your upcoming blocks."

        logger.info("[Conversation] Final assistant reply: %r", reply_content)
        return reply_content
