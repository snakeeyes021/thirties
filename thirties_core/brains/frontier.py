"""Frontier brain: drives a large model (Gemini today) with one rich prompt.

The model sees the whole schedule and the full Joplin backlog, and acts by
emitting tool calls that ``PlanTools`` executes. It speaks only in clock times;
all time math happens in the tools. After each round the brain shows the model
its tool results, so it can react to conflicts or read-only lookups, and it
checks the model's claims against the real state change.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.clock import format_clock
from thirties_core.inference import InferenceEngine
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, READ_ONLY_TOOLS, TOOL_SPECS, PlanTools

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3

ACTION_CLAIM_PATTERN = re.compile(
    r"\b(?:i\s+have|i\'ve|i\s+did|i)\s+(?:(?:\w+ly|now|already|just)\s+)?(?:cleared|scheduled|allocated|updated|set|assigned|removed|reverted|corrected|locked|finalized|put|placed|moved|added|shifted|extended|blocked)\b"
    r"|\b(?:plan|schedule|work\s+shift|blocks?)\s+(?:has\s+been\s+)?(?:updated|cleared|scheduled|finalized|locked|set)\b"
    r"|\bday\s+plan\s+finalized\b",
    re.IGNORECASE,
)


def render_tool_specs() -> str:
    lines = []
    for spec in TOOL_SPECS:
        params = spec["parameters"]
        sig = ", ".join(p if p in spec["required"] else f"{p}?" for p in params)
        lines.append(f"- {spec['name']}({sig}): {spec['description']}")
        for p, desc in params.items():
            lines.append(f"    {p}: {desc}")
    return "\n".join(lines)


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

    def _is_today(self, now) -> bool:
        plan = self.tools.day_plan
        gen = self.tools.scheduler.config.general if self.tools.scheduler else None
        lat = gen.latitude if gen else 40.7128
        lon = gen.longitude if gen else -74.0060
        return plan.target_date == get_diurnal_date(now, lat=lat, lon=lon, tz_name=plan.day_start.tzinfo)

    def build_system_prompt(self) -> str:
        plan = self.tools.day_plan
        now = get_current_time(plan.day_start.tzinfo)
        is_today = self._is_today(now)
        date_str = plan.target_date.strftime("%A, %B %d, %Y")
        if is_today:
            when = f"Today, {date_str}. Current time: {format_clock(now)}."
        else:
            when = f"{date_str} (not today; today is {now.strftime('%A, %B %d')})."

        pending = [s for s in plan.spans_in_view() if s.tentative]
        invites = "\n".join(
            f"- {s.title}, {format_clock(s.start_dt)} – {format_clock(s.end_dt)} id={s.id}" for s in pending
        ) or "None"

        return f"""You are Thirties, the user's personal planning assistant: an astute executive assistant for their time. You help them plan, re-plan and review their day in conversation, absorb the messiness of real life, and keep their schedule accurate.

PLANNING DATE: {when}
Sunrise {format_clock(plan.sunrise)}, sunset {format_clock(plan.sunset)}. This day runs from {format_clock(plan.day_start)} until {format_clock(plan.day_end)} tomorrow morning, so tonight belongs to this day.
Open thirties (30-minute chunks) left: {plan.daylight_available_count} in daylight, {plan.dark_available_count} in the dark.

SCHEDULE
{self.tools.schedule_text(upcoming_from=now if is_today else None)}

UNCONFIRMED CALENDAR INVITES
{invites}

BACKLOG
{self.build_backlog_summary()}

HOW TO CHANGE THE SCHEDULE
Put each tool call on its own line, exactly in this form, then write your reply to the user as normal text:
CALL add_event {{"title": "Composing", "start": "5:00 PM", "duration": "2 hours"}}
CALL update_event {{"event": "<id from the schedule>", "shift": "+1 hour"}}
CALL lines are hidden from the user. After your calls run you will see their results and can adjust or finish your reply.

Tools:
{render_tool_specs()}

RULES
1. Change the schedule only when the user asks or agrees to a proposal. When they ask for ideas or suggestions, propose and ask; don't change anything.
2. Every change needs a CALL. Never say something was scheduled, moved or removed without the CALL that does it.
3. Give times as clock times with AM/PM, and durations or shifts in words ("2 hours", "+1 hour"). Never work out end times yourself; the tools do all time math and report the exact result.
4. To change something already on the schedule, use update_event or remove_event with its id. For example, "work went til 4:30" means updating the work item's end. Don't recreate items, and don't touch items the user didn't mention.
5. A result starting with {NOT_DONE} means nothing changed. Tell the user plainly and offer a way forward. Calendar commitments are never overwritten.
6. Daylight and dark are physical facts, not rules about what belongs when. Only consider them when it physically matters (a daylight hike) or the user has said what they prefer.
7. Call finalize_day only when the user explicitly says they're done planning.
8. Backlog: recommend specific, concrete actions, not whole notes or reference lists such as shopping lists. For items deferred 3 or more times, be honest and kind: suggest a single 30-minute start, breaking it down, or dropping it.
9. Be concise: a few sentences. Talk in clock times; never mention ids or block numbers to the user.
"""

    def build_backlog_summary(self) -> str:
        """The full Joplin backlog, organized by notebook and note, for a large-context model."""
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

    def _claims_change(self, text: str) -> bool:
        return bool(ACTION_CLAIM_PATTERN.search(text or ""))

    def respond(self, user_text: str) -> str:
        """Run one user turn: call the model, execute its tool calls, verify, reply."""
        self.messages[0] = {"role": "system", "content": self.build_system_prompt()}
        snapshot_before = self.tools.capture_snapshot()
        logger.info("[FrontierBrain] User message: %r", user_text)
        self.messages.append({"role": "user", "content": user_text})

        reply = ""
        last_results: List[str] = []
        reflected = False
        for round_index in range(MAX_ROUNDS):
            response = self.inference_engine.chat(messages=self.messages, tools=TOOL_SPECS)
            content = response.get("content", "") or ""
            calls = response.get("tool_calls", []) or []
            self.messages.append(response)
            if content.strip():
                reply = content

            results = []
            for call in calls:
                name, args = call.get("name"), call.get("arguments", {})
                output = self.tools.execute(name, args)
                logger.info("[FrontierBrain] %s(%s) -> %s", name, args, output)
                results.append(output)
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"), "name": name, "content": output})
            if results:
                last_results = results

            delta = self.tools.compute_delta(snapshot_before, self.tools.capture_snapshot())
            if not calls:
                if self._claims_change(content) and delta.total_changes == 0 and not reflected:
                    reflected = True
                    logger.warning("[FrontierBrain] Model claimed a change without tool calls; asking it to correct.")
                    self.messages.append({"role": "user", "content": (
                        "[System check, not from the user] Your reply says the schedule changed, but you made no "
                        "CALLs, so nothing changed. Make the CALLs now, or correct your reply."
                    )})
                    continue
                break
            needs_follow_up = (
                any(c.get("name") in READ_ONLY_TOOLS for c in calls)
                or any(r.startswith(NOT_DONE) for r in results)
                or not content.strip()
            )
            if not needs_follow_up or round_index == MAX_ROUNDS - 1:
                break
            if any(r.startswith(NOT_DONE) for r in results):
                reply = ""  # the draft was written before the refusal; don't show it

        delta = self.tools.compute_delta(snapshot_before, self.tools.capture_snapshot())
        if self._claims_change(reply) and delta.total_changes == 0:
            logger.warning("[FrontierBrain] Suppressing unverified change claim: %r", reply)
            reply = "I wasn't able to change your schedule just now. Could you tell me again what you'd like moved or added?"
        if not reply.strip():
            reply = " ".join(
                f"I couldn't do that: {r[len(NOT_DONE) + 2:]}" if r.startswith(NOT_DONE) else r for r in last_results
            ) or "Understood."
        logger.info("[FrontierBrain] Final reply: %r", reply)
        return reply
