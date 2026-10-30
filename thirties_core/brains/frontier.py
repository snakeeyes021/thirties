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
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.clock import format_clock
from thirties_core.inference import InferenceEngine
from thirties_core.lists import ListCatalog, urgency_label
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


ROLE_BADGES = {
    "routine": " [Routine: done every day, in order]",
    "today": " [Today's list: what the user means to do today]",
    "menu": " [Menu: options to pick from when there's free time]",
    "list": " [List]",
    "project": " [Project: a one-off with an end]",
    "backlog": " [Project backlog: plan time on the project as one item ('<project> dev'), never single tickets]",
    "reference": " [Reference / wishlist: never suggest unless asked]",
}


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

        avail = plan.availability(now if is_today else None)
        services = self.tools.services
        assistant = self.tools.assistant
        profile = services.profile.as_markdown() if services and not services.profile.is_empty() else "(empty so far)"
        memory = "\n".join(f"- {n.text} (id {n.id})" for n in services.memory.notes()) if services else ""
        stats = "\n".join(f"- {f}" for f in assistant.statistics_facts()) if assistant else ""
        weather = "\n".join(f"- {f}" for f in assistant.weather_facts()) if assistant else ""
        weather = weather or "- No forecast available."
        review_block = ""
        if services and self.tools.scheduler:
            from thirties_core.stats import unreviewed_spans
            since = plan.day_start - timedelta(days=1)
            recent = self.tools.scheduler.state_db.get_spans_between(since, now)
            waiting = unreviewed_spans(recent, now, since)
            if waiting:
                review_block = ("WAITING FOR AN OUTCOME (ask about these once, briefly, when it fits; record with log_outcome)\n"
                                + "\n".join(f"- {s.start_dt.strftime('%a')} {format_clock(s.start_dt)} – "
                                             f"{format_clock(s.end_dt)}: {s.title} id={s.id}" for s in waiting[-8:])
                                + "\n\n")

        pending = [s for s in plan.spans_in_view() if s.tentative]
        invites = "\n".join(
            f"- {s.title}, {format_clock(s.start_dt)} – {format_clock(s.end_dt)} id={s.id}" for s in pending
        ) or "None"

        return f"""You are Thirties, the user's personal planning assistant: an astute executive assistant for their time. You help them plan, re-plan and review their day in conversation, absorb the messiness of real life, and keep their schedule accurate.

PLANNING DATE: {when}
Sunrise {format_clock(plan.sunrise)}, sunset {format_clock(plan.sunset)}. This day runs from {format_clock(plan.day_start)} until {format_clock(plan.day_end)} tomorrow morning, so tonight belongs to this day.
Open blocks (half-hour chunks) still ahead: {avail["daylight_open"]} in daylight, {avail["dark_open"]} in the dark.

SCHEDULE
{self.tools.schedule_text(upcoming_from=now if is_today else None)}

UNCONFIRMED CALENDAR INVITES
{invites}

{review_block}BACKLOG
{self.build_backlog_summary()}

USER PROFILE (what you know about the user's life; keep it current with update_profile)
{profile}

YOUR MEMORY NOTES (how things work for this user; add with remember, delete with forget)
{memory}

STATISTICS (computed from what actually happened)
{stats}

WEATHER (from the forecast)
{weather}

HOW TO CHANGE THE SCHEDULE
Put each tool call on its own line, exactly in this form, then write your reply to the user as normal text:
CALL add_event {{"title": "Composing", "start": "5:00 PM", "duration": "2 hours"}}
CALL update_event {{"event": "<id from the schedule>", "shift": "+1 hour"}}
CALL lines are hidden from the user. After your calls run you will see their results and can adjust or finish your reply.

Tools:
{render_tool_specs()}

RULES
1. Change the schedule, or the user's lists in Joplin (add_task, break_down_task), only when the user asks or agrees to a proposal. When they ask for ideas or suggestions, propose and ask; don't change anything.
2. Every change needs a CALL. Never say something was scheduled, moved or removed without the CALL that does it. Tool results are notes for you, not the user: never quote them or mention tools; say what happened, or ask what you need to, in your own words.
3. Give times as clock times. Say AM or PM only where it could be mistaken: the first time if it isn't obvious from the current time, whenever a time crosses noon or midnight from the one before, and on a bare hour that's ambiguous. Never work out end times or durations yourself; the tools do all time math and report the exact result.
4. Use the statistics: propose durations from how long things really take the user, and if a heads-up says the day holds more than they usually get through, say so once, kindly, and offer what could move.
5. To change something already on the schedule, use update_event or remove_event with its id. For example, "work went til 4:30" means updating the work item's end. Don't recreate items, and don't touch items the user didn't mention.
6. A result starting with {NOT_DONE} means nothing changed. Tell the user plainly and offer a way forward. Calendar commitments are never overwritten.
7. Daylight and dark are physical facts, not rules about what belongs when. Only consider them when it physically matters (a daylight hike) or the user has said what they prefer.
8. Changes are live the moment you make them. There is no separate step to save, confirm, "lock in" or finalize a plan, so never offer one.
9. Scheduling versus lists. When the user names something to do, call match_backlog. "Same task": schedule it with that task_id. "Only similar": don't assume or correct them; ask whether it's that item (quote it) or something new. "Nothing": schedule it as a new item. Add to a list (add_task) when they ask, or offer once if it sounds like a to-do they'll want to track (not meals or routines); when they want it both added and scheduled, do both in one add_task call by giving it start (and duration), so the two stay linked.
10. Backlog: bring it up only when the user is deciding how to use open time or asks for ideas, not after every change. Recommend specific, concrete actions, not whole notes or reference lists such as shopping lists. Each note says what it's for, and items say how pressing they are (now, soon, by a date; unmarked is no hurry): favour what's pressing, and never push someday or reference items. When the user says how pressing something is, record it with set_urgency. For an item deferred 3 or more times, be honest and kind once in a conversation: suggest a single 30-minute start, breaking it down, or dropping it. Don't repeat it if the user didn't take it up.
11. Answer in the user's terms, with only what this reply needs. Use their units: if they count in chunks or blocks, so do you ("you have three open chunks at 4:00 PM"); otherwise use clock times and hours. Mention how long something takes only when length or fit is the question. List a plan (a proposal too) one item per line, start time first, in the user's own short words ("- 4:00 PM – Feed cat"), with no durations; each item ends when the next begins, so add an end time only before a gap ("(until 7:00 PM)"). Never write a running paragraph of times, and never mention ids or block numbers. Be concise.
12. Track what actually happens. When the user says they did, finished, skipped or are still on something, record it with log_outcome, only for the items they named (doing PT doesn't mean the shower happened). Ask about something not reported at most once; if they don't say, leave it. If they don't say when, estimate and pass estimated=true. If you're unsure whether something happened, ask a short check-in question ("Did you get to X, or are you still on Y?"). If they mention how they feel, log_energy.
13. Learn. When you learn something lasting about the user's life (a habit, a goal, a preference), add it with update_profile and mention that you did. When you learn how something works for them (what a notebook or section means, how they like things done), save it with remember. Your notes and the profile are shown to you in every conversation.
14. Other days: every schedule tool takes a day ("tomorrow", "Monday", "Oct 20"). Use it for anything not on the day being planned. For bigger-picture planning (goals over weeks), look at week_overview, statistics and the profile, and spread work across days. Past conversations are available through search_history, list_conversations and read_conversation; use them when the user refers to an earlier discussion.
15. One item per thing. Things done at the same time (an audiobook during dinner, working on two things at once) are separate items sharing the time: just add each at its time. Never join two things into one title.
16. A task that keeps getting put off, or that the user calls too big or hard to start, can be broken into 3 to 6 small, concrete steps. Propose them; once the user agrees, write them with break_down_task, and offer to schedule the first step.
17. "Remind me to X" is a thing to do: set_reminder puts it on the plan (and today's list) with a ping when it's time; if they give no time, choose a sensible one and say it. Only a bare message with nothing to do ("ping me at 3 that the laundry's done") is ping_only; call that a ping, never a "reminder". If they say there's no need to keep asking about something, or to stop check-ins, use mute_checkins.
18. Mornings: Thirties offers to carry yesterday's unfinished items into today, both planned ones and ones still on today's list, plus anything pressing (rollover_proposal shows it). When the user agrees, call apply_rollover (with skip for anything they want left out) instead of moving items one by one. If it names items with no list to go back to, propose a list for each, one per line ("- Clip cat nails → Errands/Admin"), and add them with add_task once the user agrees.
19. Time is told in half-hour blocks, like reading a piano roll. Items keep their real times (the tools show them, with the block each is in); blocks are how the day is shown. A schedule or list is told by blocks: each row is a block and what's in it, things at the same time joined with + ("- 10:30 – Personal call + Break"); when one thing ends and another begins within a block, the block's time, then each on its own line; consecutive blocks with the same things are one row. Asked about one item, give its real time and its block ("Your Uber is at 12:15, in the 12:00 block"). Never change a time the user gave: 12:15 is never "12:00". Exact minutes for everything only if asked (list_schedule exact=true). Never put the word "thirty" next to a time: say block or chunk. Things set by someone else's clock (appointments, rides, flights, shows, calls at a set time) are fixed: they keep their exact time and never slide; for an unclear case, say fixed=true or false when adding it. The time now is said as it is ("it's 8:41"), never as its block. Items may overlap, nest or coincide: adding or moving one never moves, trims or splits another, and things sharing time are told as one entry ("Redfox dev + Thirties dev"). Only fixed things conflict.
20. Weather: when suggesting or planning something outdoors (a walk, a run, yard work, errands on foot), check the forecast; don't suggest it into rain or bad weather, and if the user plans it there (a tool result says "Weather heads-up"), mention it once, briefly, with a better time if there is one.
21. Today's list versus the plan: the list is what the user means to do today (their Today note); the plan is when. Everything put on today's plan goes on the list by itself. When the user says they'll do something today without a time, or agrees to a day's list, put it on the list with add_to_today. Mornings start from the list.
22. The plan changes only when the user asks or agrees. When they report running late or behind, record what happened (log_outcome) and leave the rest of the plan alone; speak up only if a fixed commitment (calendar, work) is at risk, once. "Push everything back" by an amount is push_back (it moves what's past and not marked too). When they ask for a new plan ("replan", "just figure it out"), call redraw: Python lays the rest of today out again in order, around fixed things, and says what doesn't fit; then show the new plan, and if something didn't fit, ask whether to plan it tomorrow or let it go. Record constraints with set_item_facts as soon as the user states them (after X, not before a time, needs daylight, stays put) so every redraw keeps them. Nothing is done until the user says so: items not reported are still to do. When you list the day, items not marked yet go at the bottom under "Not marked yet", one line per block, with no times.
23. A routine (a note of daily steps) goes on the plan with put_routine: give each thirty its start and the steps done in it, in the routine's order. Never add a routine to a list, as a new item, or as one block ("Morning routine"). Daily things and time on a project ("Redfox dev") don't carry over to the next day as unfinished.
24. Something to do with no time given, or "later": call slot_options and pick a slot with a short reason (a quieter part of the day, not crammed against dinner); "later" is a real gap later on.
"""

    def _day_list_section(self) -> str:
        """The user's list for today (their Today note) and routines, with what's done."""
        if self.tools.scheduler is None:
            return ""
        from thirties_core.backlog import DONE_HIDES_DAYS
        from thirties_core.today import notes_with_role, with_done
        db = self.tools.scheduler.state_db
        roles = ListCatalog(db).roles()
        today = self.tools.today()
        done = {"today": db.completed_since(today - timedelta(days=DONE_HIDES_DAYS)),  # routines: today's only
                "routine": db.completed_since(today)}
        lines = []
        for role, heading in (("today", "Today's list"), ("routine", "Routine")):
            for note in notes_with_role(roles, self.active_notes, role):
                items = with_done(note, done[role])
                if items:
                    lines.append(f"{heading} ({note['title']}):")
                    lines += [f"- {'[done] ' if ticked else ''}{text}" for text, ticked in items]
        if not lines:
            return ""
        return ("TODAY'S LIST (what the user means to do today: their Today note, plus routines; ticks are what's "
                "done):\n" + "\n".join(lines))

    def build_backlog_summary(self) -> str:
        """The full Joplin backlog, organized by notebook and note, for a large-context model."""
        if not self.tasks and not self.active_notes:
            return "No active backlog tasks or notes found."

        # Map tasks by parent_note_id
        tasks_by_note: Dict[str, List[TaskItem]] = {}
        standalone_tasks: List[TaskItem] = []
        media = [t for t in self.tasks if t.id.startswith("media:")]
        for t in self.tasks:
            if t.id.startswith("media:"):
                continue
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
        day_list = self._day_list_section()
        if day_list:
            sections.append(day_list)
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

        roles: Dict[str, str] = {}
        if self.tools.scheduler is not None:
            roles = {nid: r["role"] for nid, r in ListCatalog(self.tools.scheduler.state_db).roles().items()}
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

                # What the note is for: the role the user confirmed or the model guessed, else a guess from the title
                lower_title = title.lower()
                if roles.get(n_id) in ROLE_BADGES:
                    note_type_badge = ROLE_BADGES[roles[n_id]]
                elif "routine" in lower_title:
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
                    nb_block.append("  Open checklist items:")
                    depth: Dict[str, int] = {}
                    current_section = None
                    for t in note_tasks:
                        if t.section != current_section:
                            current_section = t.section
                            if t.section:
                                tag = " (SOMEDAY/MAYBE: not committed; don't suggest unless asked)" if t.someday else ""
                                nb_block.append(f"  Section '{t.section}'{tag}:")
                        depth[t.id] = depth.get(t.parent_task_id, -1) + 1 if t.parent_task_id else 0
                        indent = "  " * (depth[t.id] + 1)
                        def_str = f" [Deferred: {t.deferred_count}x]" if t.deferred_count > 0 else ""
                        maybe = " [someday]" if t.someday and not t.section else ""
                        pressing = urgency_label(t) if t.urgency in ("now", "soon") or t.due else ""
                        pressing = f" [{pressing}]" if pressing and t.list_role not in ("today", "routine") else ""
                        nb_block.append(f"{indent}- [ID: {t.id}] '{t.title}'{def_str}{maybe}{pressing}")
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
        if media:
            by_list: Dict[str, List[TaskItem]] = {}
            for t in media:
                by_list.setdefault(t.source_notebook.split(" > ")[-1], []).append(t)
            import random
            pick = random.Random(date.today().toordinal())  # a different handful each day
            sections.append("WISHLISTS FROM THE USER'S PUBLIC ACCOUNTS (Goodreads, Letterboxd, Serializd; read-only, "
                            "synced automatically; separate from Joplin). A sample of each; search_backlog finds the "
                            "rest:\n" + "\n".join(
                                f"=== {name} ({len(items)} in all) ===\n" + "\n".join(
                                    f"- [ID: {t.id}] {t.title}" for t in (items if len(items) <= 15 else pick.sample(items, 15)))
                                for name, items in by_list.items()))
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

    RECENT_TURNS = 8

    def _for_model(self) -> List[Dict[str, Any]]:
        """What the model sees: the whole conversation, but tool results only for the last few user turns
        (older ones are spent; the schedule in the prompt is current)."""
        user_idx = [i for i, m in enumerate(self.messages) if m.get("role") == "user"]
        cutoff = user_idx[-self.RECENT_TURNS] if len(user_idx) > self.RECENT_TURNS else 0
        return [m for i, m in enumerate(self.messages) if (i >= cutoff or m.get("role") != "tool")
                and not m.get("display_only")]

    def respond(self, user_text: str) -> str:
        """Run one user turn: call the model, execute its tool calls, verify, reply."""
        self.messages[0] = {"role": "system", "content": self.build_system_prompt()}
        changes_before = self.tools.successful_changes
        logger.info("[FrontierBrain] User message: %r", user_text)
        self.messages.append({"role": "user", "content": user_text})
        turn_start = len(self.messages)

        reply = ""
        last_results: List[str] = []
        reflected = False
        for round_index in range(MAX_ROUNDS):
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
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

            changed = self.tools.successful_changes - changes_before
            if not calls:
                if self._claims_change(content) and changed == 0 and not reflected:
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

        for attempt in range(2):  # a reply that's empty, or that narrates the conversation, gets one more try
            problem = "empty" if not reply.strip() else ("meta" if self._is_meta(reply) else "")
            if not problem or attempt == 1:
                break
            logger.warning("[FrontierBrain] Reply was %s; asking again: %r", problem, reply[:300])
            self.messages.append({"role": "user", "content": (
                "[System check, not from the user] " + (
                    "You didn't write a reply. Reply to the user now, in your own words." if problem == "empty" else
                    "Your reply described the conversation, your instructions or tool results. Write only the "
                    "message the user should read.")
            )})
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
            self.messages.append(response)
            for call in response.get("tool_calls", []) or []:
                output = self.tools.execute(call.get("name"), call.get("arguments", {}))
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                      "name": call.get("name"), "content": output})
            reply = response.get("content", "") or ""
        if self._is_meta(reply):
            kept = [p for p in re.split(r"\n\s*\n", reply) if not self._is_meta(p)]
            reply = "\n\n".join(kept)
        reply = "\n".join(line for line in reply.splitlines()  # tool syntax never reaches the user, however it came
                          if not re.match(r"^\s*`*\s*CALL\s+[A-Za-z_]\w*", line)).strip()
        if self._claims_change(reply) and self.tools.successful_changes == changes_before:
            logger.warning("[FrontierBrain] Suppressing unverified change claim: %r", reply)
            reply = "I wasn't able to change your schedule just now. Could you tell me again what you'd like moved or added?"
        if not reply.strip():
            reply = ("Done." if self.tools.successful_changes > changes_before else
                     "Sorry, I lost my train of thought there. Could you say that again?")
        reply = self._own_up(turn_start, reply)
        self._show_only(turn_start, reply)
        logger.info("[FrontierBrain] Final reply: %r", reply)
        return reply

    def _show_only(self, turn_start: int, reply: str) -> None:
        """A turn can take several rounds (a draft, tool calls, a retry); the model keeps them all as context, but
        the user sees one message: the final reply. Without this the chat showed every draft, repeated or
        half-finished ones and the model's notes to itself included."""
        rounds = [m for m in self.messages[turn_start:] if m.get("role") == "assistant"]
        for m in rounds:
            m["hidden"] = True
        final = next((m for m in reversed(rounds) if str(m.get("content", "")).strip() == reply.strip()), None)
        if final is not None:
            final.pop("hidden", None)
        else:  # the reply was cleaned up or replaced: show it, but don't feed it back to the model as said twice
            self.messages.append({"role": "assistant", "content": reply, "display_only": True})

    _ADMITS_RE = re.compile(r"\b(couldn'?t|could not|can'?t|cannot|wasn'?t able|unable|didn'?t|failed|not able|"
                            r"won'?t|isn'?t on|not on (?:the|your))\b", re.IGNORECASE)
    _CLAIMS_RE = re.compile(r"\b(marked|logged|noted|all set|done|added|moved|scheduled|updated|cleaned|cleared|"
                            r"removed|checked (?:it )?off|ticked|taken care of)\b", re.IGNORECASE)

    def _failed_changes(self, turn_start: int) -> List[str]:
        """Changes that failed this turn with nothing after them that worked: a refusal the model got around
        another way (moving a break with a fresh add) isn't a failure the user needs to hear about."""
        failed: List[str] = []
        for m in self.messages[turn_start:]:
            if m.get("role") != "tool" or m.get("name") in READ_ONLY_TOOLS:
                continue
            out = str(m.get("content") or "")
            if out.startswith(NOT_DONE):
                failed.append(out[len(NOT_DONE):].lstrip(": ").strip())
            else:
                failed = []
                joplin = re.search(r"\((couldn'?t [^:]*Joplin.*)\)\.?$", out)  # done here, but not in Joplin
                if joplin:
                    failed.append(re.sub(r"\s+", " ", joplin.group(1)))
        return failed

    def _own_up(self, turn_start: int, reply: str) -> str:
        """A reply that says a change happened, when the change failed, gets one more try with the failures
        named ("It is marked done now" over a failed write, "All set" over three refusals: 10-22, 10-24). If it
        still claims it, a plain line is added; the model's tool wording never reaches the user."""
        failed = self._failed_changes(turn_start)
        if not failed or self._ADMITS_RE.search(reply) or not self._CLAIMS_RE.search(reply):
            return reply
        logger.warning("[FrontierBrain] Reply claims a change that failed: %r (%s)", reply[:200], failed)
        self.messages.append({"role": "user", "content": (
            "[System check, not from the user] These didn't go through: " + " | ".join(failed) + ". Your reply "
            "says otherwise. Rewrite it so it's true: say plainly what didn't happen and what you'll do instead.")})
        response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
        self.messages.append(response)
        for call in response.get("tool_calls", []) or []:
            output = self.tools.execute(call.get("name"), call.get("arguments", {}))
            self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                  "name": call.get("name"), "content": output})
        fixed = str(response.get("content") or "").strip()
        if fixed and (self._ADMITS_RE.search(fixed) or not self._failed_changes(turn_start)):
            return fixed
        return reply.rstrip() + "\n\n(One part of that didn't go through, so it isn't recorded yet.)"

    _META_RE = re.compile(
        r"(^|\n)\s*(wait[,!.]|notice that|let'?s check|let me check|looking at the (prompt|transcript|history)|"
        r"the previous (turn|assistant)|the assistant'?s? (previous|reply|turn)|in the (prompt|transcript|history)\b)|"
        r"\b(tool results?|tool calls?|(?-i:CALL [a-z]+_[a-z_]+)|system instructions|rule \d+\b|push_?later|"
        r"replace_?overlaps|"
        r"task_id|\bid=[0-9a-f]{6,})|\(Result of [a-z_]+|for the assistant only", re.IGNORECASE)

    def _is_meta(self, text: str) -> bool:
        """The model talking about the transcript, its rules or its tools instead of to the user."""
        return bool(self._META_RE.search(text or ""))
