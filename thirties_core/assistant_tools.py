"""Tools beyond the day's schedule: outcomes, review, memory, profile, history, statistics.

These sit next to the schedule tools in ``PlanTools`` and share its dispatcher and
contract: natural-language arguments in, short plain-English results out, and a
result starting with ``NOT_DONE`` when nothing changed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from thirties_core.clock import (ClockError, format_clock, format_duration, parse_day, parse_duration_minutes,
                                 thirty_ceil, thirty_of)
from thirties_core.memory import (PROFILE_SECTIONS, CheckinMutes, ConversationStore, EnergyLog, MemoryStore,
                                  ProfileStore, ReminderStore)
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem, new_span_id
from thirties_core.stats import fact_lines, unreviewed_spans

if TYPE_CHECKING:
    from thirties_core.joplin_engine import JoplinEngine
    from thirties_core.plan_tools import PlanTools

logger = logging.getLogger(__name__)


NOT_DONE_PREFIX = "NOT_DONE"  # plan_tools.NOT_DONE (not imported here: plan_tools imports this module)


class ToolError(Exception):
    """A tool call that cannot be carried out; nothing was changed."""


@dataclass
class Services:
    """Everything beyond the day plan that tools and brains read or write."""
    conversations: ConversationStore
    profile: ProfileStore
    memory: MemoryStore
    energy: EnergyLog
    tasks: List[TaskItem] = field(default_factory=list)
    joplin: Optional["JoplinEngine"] = None
    reminders: Optional[ReminderStore] = None
    mutes: Optional[CheckinMutes] = None

    @classmethod
    def for_db(cls, db, tasks: Optional[List[TaskItem]] = None, joplin=None) -> "Services":
        return cls(ConversationStore(db), ProfileStore(db), MemoryStore(db), EnergyLog(db), list(tasks or []), joplin,
                   ReminderStore(db), CheckinMutes(db))


ASSISTANT_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "log_outcome",
        "description": "Record what happened with a planned item or a backlog task: done, partly done, or skipped. "
                       "Done backlog tasks are checked off in Joplin.",
        "parameters": {
            "event": "The item's id or title (a schedule item, or a backlog task).",
            "status": "'done', 'partial' or 'skipped'.",
            "started": "Clock time it actually started, if known.",
            "finished": "Clock time it actually finished, if known.",
            "duration": "How long it actually took, if known, e.g. '45 minutes'.",
            "estimated": "true if the times are your best guess rather than what the user said.",
            "note": "Optional note.",
        },
        "required": ["event", "status"],
    },
    {
        "name": "log_energy",
        "description": "Record how energetic the user says they feel, 1 (drained) to 5 (great).",
        "parameters": {"level": "1 to 5.", "note": "Optional note."},
        "required": ["level"],
    },
    {
        "name": "review_day",
        "description": "List planned items that have ended with no outcome recorded, and suggest where unfinished "
                       "backlog tasks could go. Read-only.",
        "parameters": {"day": "Which day to review (default: today)."},
        "required": [],
    },
    {
        "name": "remember",
        "description": "Save a short note about how things work for this user (e.g. 'Probation items are maybes').",
        "parameters": {"note": "The note."},
        "required": ["note"],
    },
    {
        "name": "forget",
        "description": "Delete a memory note.",
        "parameters": {"note_id": "The note's id."},
        "required": ["note_id"],
    },
    {
        "name": "update_profile",
        "description": "Add to or rewrite a section of the user profile: " + ", ".join(PROFILE_SECTIONS) + ".",
        "parameters": {
            "section": "Section name.",
            "text": "What to add (one line) or the new section text.",
            "replace": "true to replace the whole section instead of adding a line.",
        },
        "required": ["section", "text"],
    },
    {
        "name": "search_history",
        "description": "Search past conversations for a topic. Read-only.",
        "parameters": {"query": "Words to look for."},
        "required": ["query"],
    },
    {
        "name": "list_conversations",
        "description": "List conversations in a date range. Read-only.",
        "parameters": {"from_day": "First day (default: a week ago).", "to_day": "Last day (default: today)."},
        "required": [],
    },
    {
        "name": "read_conversation",
        "description": "Read a past conversation. Read-only.",
        "parameters": {"conversation_id": "The conversation's id."},
        "required": ["conversation_id"],
    },
    {
        "name": "get_statistics",
        "description": "Statistics on completion, real durations, best times of day and energy. Read-only.",
        "parameters": {"days": "How many days back to look (default 60)."},
        "required": [],
    },
    {
        "name": "week_overview",
        "description": "A day-by-day summary of a week: what's planned, what got done, and free time. Read-only.",
        "parameters": {"start_day": "First day (default: today).", "days": "How many days (default 7)."},
        "required": [],
    },
    {
        "name": "add_task",
        "description": "Add a task to one of the user's lists (a Joplin note). Only when the user asks or agrees.",
        "parameters": {"title": "The task.", "list": "Which list (note title).",
                       "section": "Optional heading within the note."},
        "required": ["title", "list"],
    },
    {
        "name": "break_down_task",
        "description": "Break a task into smaller steps, written into Joplin as subtasks under it. Works on any "
                       "checklist item in the user's lists, including routine notes and ticked items. Propose "
                       "the steps first; call this only after the user agrees.",
        "parameters": {"task": "The task (title or task_id).", "steps": "The steps, as a list of short actions.",
                       "list": "The note it's in, if the same item is in more than one (e.g. 'Morning routine')."},
        "required": ["task", "steps"],
    },
    {
        "name": "rollover_proposal",
        "description": "Yesterday's planned items with no outcome, each placed in today's open time (or back to the "
                       "lists if nothing fits). Read-only; remembered so apply_rollover does exactly this.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "apply_rollover",
        "description": "Apply today's rollover proposal (after the user agrees). Items the user doesn't want today "
                       "go back to their lists.",
        "parameters": {"skip": "Titles or words of items to leave out (optional)."},
        "required": [],
    },
    {
        "name": "set_reminder",
        "description": "Send the user a message at a given time (desktop notification, and Telegram if paired). "
                       "If they don't say a time, pick a sensible one and tell them.",
        "parameters": {"text": "What to remind them of.",
                       "date": "Which day: 'today', 'tomorrow', 'Saturday', 'next week', 'Oct 20' (default today).",
                       "time": "Clock time like '3:00 PM', or 'in 2 hours' (default 9:00 AM)."},
        "required": ["text"],
    },
    {
        "name": "list_reminders",
        "description": "List upcoming reminders. Read-only.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel an upcoming reminder.",
        "parameters": {"reminder": "Its id or words from it."},
        "required": ["reminder"],
    },
    {
        "name": "mute_checkins",
        "description": "Stop check-in questions: about one item (when the user says there's no need to ask), or "
                       "all of them until a time (default: the end of the day).",
        "parameters": {"event": "The item's id or title (leave empty for all check-ins).",
                       "until": "Clock time, or 'tomorrow' (default: end of the day)."},
        "required": [],
    },
    {
        "name": "get_weather",
        "description": "The forecast for a day (default: the rest of today), as plain facts. Read-only.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "match_backlog",
        "description": "Before scheduling something the user names, check whether it's already on their lists: "
                       "returns the same task (schedule it with its task_id), only similar ones (ask), or none (new). "
                       "Read-only.",
        "parameters": {"text": "What the user wants to do, in their words."},
        "required": ["text"],
    },
    {
        "name": "search_backlog",
        "description": "Find backlog tasks by keywords. Read-only.",
        "parameters": {"query": "Words to look for."},
        "required": ["query"],
    },
]

ASSISTANT_READ_ONLY = {"review_day", "search_history", "list_conversations", "read_conversation",
                       "get_statistics", "search_backlog", "week_overview", "list_reminders", "rollover_proposal",
                       "match_backlog", "get_weather"}


_STOPWORDS = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "with", "my", "some", "at", "about",
              "it", "is", "do", "that", "this", "i", "me", "up", "out", "from", "by", "or"}


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1 and w not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def rank_tasks(tasks: List[TaskItem], query: str, limit: int = 8) -> List[TaskItem]:
    """Backlog tasks most relevant to a query (keyword overlap with title, note, notebook and section)."""
    q = {_stem(w) for w in _words(query)}
    if not q:
        return []
    scored = []
    for t in tasks:
        title = {_stem(w) for w in _words(t.title)}
        context = {_stem(w) for w in _words(f"{t.parent_note_title or ''} {t.section} {t.source_notebook}")}
        score = 3 * len(q & title) + len(q & context)
        if score:
            scored.append((score, -len(t.title), t))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [t for _, _, t in scored[:limit]]


def backlog_match(text: str, task: TaskItem) -> str:
    """How a request relates to a backlog task: 'same', 'similar' (shares words but may be a different
    thing, like 'message Brad back' vs 'Message Brad about a weekend trip') or 'none'."""
    q = {_stem(w) for w in _words(text)}
    t = {_stem(w) for w in _words(task.title)}
    if not q or not t or not (q & t):
        return "none"
    if q == t or (q <= t and len(t - q) <= 1) or (t <= q and len(q - t) <= 1):
        return "same"
    return "similar"


def describe_task(t: TaskItem) -> str:
    where = " > ".join(x for x in (t.source_notebook, t.parent_note_title, t.section) if x)
    flags = []
    if t.someday:
        flags.append("someday/maybe")
    if t.deferred_count:
        flags.append(f"deferred {t.deferred_count}x")
    return f"{t.title} [{where}]" + (f" ({', '.join(flags)})" if flags else "") + f" task_id={t.id}"


def _checkbox_id(note_id: str, text: str, occurrence: int = 0) -> str:
    """The id the Joplin reader gives a checklist item (see joplin_engine.checklist_tasks)."""
    import hashlib
    return hashlib.sha256(f"{note_id}|{text.strip()}|{occurrence}".encode("utf-8")).hexdigest()[:16]


class AssistantTools:
    """Outcome, review, memory, profile, history and statistics tools."""

    def __init__(self, plan_tools: "PlanTools", services: Services) -> None:
        self.pt = plan_tools
        self.s = services

    # --- helpers ---

    @property
    def _db(self):
        return self.pt.scheduler.state_db if self.pt.scheduler else None

    def _today(self):
        return self.pt.today()

    def _find_task(self, ref: str) -> Optional[TaskItem]:
        ref = ref.strip()
        exact = [t for t in self.s.tasks if t.id == ref or t.title.lower() == ref.lower()]
        if exact:
            return exact[0]
        q = {_stem(w) for w in _words(ref)}

        def overlap(t: TaskItem) -> int:
            return len(q & {_stem(w) for w in _words(t.title)})

        ranked = rank_tasks(self.s.tasks, ref, limit=2)
        if not ranked or overlap(ranked[0]) == 0:
            return None
        if len(ranked) == 1 or overlap(ranked[0]) > overlap(ranked[1]):
            return ranked[0]
        return None

    def _writer(self):
        if self.pt.scheduler is None or self.s.joplin is None:
            return None
        from thirties_core.joplin_writeback import JoplinWriter
        return JoplinWriter(self.pt.scheduler.config, self._db, self.s.joplin.api_client)

    def _complete_backlog_task(self, task: TaskItem) -> str:
        if self._db:
            self._db.record_task_completed(task.id, self.pt.day_plan.target_date)
        if task.id.startswith("media:"):
            return ""
        writer = self._writer()
        if writer is None:
            return "(not checked off in Joplin: Joplin isn't connected)"
        from thirties_core.joplin_writeback import complete_op
        status, msg = writer.submit(complete_op(task))
        if status in ("done", "queued"):
            self.s.tasks = [t for t in self.s.tasks if t.id != task.id]
        return {"done": "and checked it off in Joplin",
                "queued": "(it'll be checked off in Joplin as soon as Joplin is open)",
                "not_allowed": "(not checked off in Joplin: Thirties isn't allowed to change Joplin; Preferences → Tasks)",
                "failed": f"(couldn't check it off in Joplin: {msg})"}[status]

    # --- changing the backlog in Joplin (only when the user asked or agreed) ---

    def _joplin_notes(self) -> List[Dict[str, Any]]:
        """The Joplin notes Thirties reads, with their bodies (empty if Joplin isn't connected)."""
        try:
            notes = self.s.joplin.fetch_active_notes() if self.s.joplin is not None else []
        except Exception:
            return []
        return notes if isinstance(notes, list) else []

    def _notes(self) -> Dict[str, str]:
        notes = {n["id"]: n["title"] for n in self._joplin_notes()}  # routine notes too, all ticked or not
        notes.update({t.parent_note_id: t.parent_note_title for t in self.s.tasks if t.parent_note_id and t.parent_note_title})
        notes.update({t.id: t.title for t in self.s.tasks if not t.is_checkbox_item and not t.id.startswith("media:")})
        return notes

    def _note_item(self, ref: str, list_name: str = "") -> Optional[TaskItem]:
        """A checklist line, open or ticked, in any note: routine notes keep their items ticked between days,
        so those aren't backlog tasks, but the user can still have them changed."""
        from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX
        want = ref.strip().lower()
        in_list = {_stem(w) for w in _words(list_name)}
        hits = []
        for note in self._joplin_notes():
            if in_list and not in_list & {_stem(w) for w in _words(note["title"])}:
                continue
            for line in (note.get("body") or "").splitlines():
                m = CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)
                if m and m.group(2).strip().lower() == want:
                    hits.append(TaskItem(id=_checkbox_id(note["id"], m.group(2).strip()), title=m.group(2).strip(),
                                         source_notebook=note.get("source_notebook", ""), is_checkbox_item=True,
                                         parent_note_title=note["title"], parent_note_id=note["id"]))
                    break
        if len(hits) > 1:
            raise ToolError(f"'{ref}' is in more than one list ({', '.join(h.parent_note_title for h in hits)}). "
                            "Which one? Give it as list.")
        return hits[0] if hits else None

    def add_task(self, title: str, list: str = "", section: str = "", **_: Any) -> str:
        notes = self._notes()
        want = {_stem(w) for w in _words(list)}
        scored = sorted(((len(want & {_stem(w) for w in _words(name)}), name, nid) for nid, name in notes.items()),
                        reverse=True)
        if not list or not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
            options = ", ".join(sorted({name for name in notes.values()})[:12])
            raise ToolError(f"Which list should '{title}' go on? Lists: {options}.")
        _, note_title, note_id = scored[0]
        writer = self._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin, so it can't change your lists yet. "
                            "Connect it in Preferences → Tasks.")
        status, msg = writer.submit({"op": "add_task", "note_id": note_id, "title": title.strip(),
                                     "section": section or None})
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        notebook = next((t.source_notebook for t in self.s.tasks if t.parent_note_id == note_id), "")
        new_id = _checkbox_id(note_id, title)
        self.s.tasks.append(TaskItem(id=new_id, source_notebook=notebook, title=title.strip(),
                                     is_checkbox_item=True, parent_note_title=note_title, parent_note_id=note_id,
                                     section=section or ""))
        return (f"Added '{title.strip()}' to {note_title}" + (" (Joplin will get it when it's open)" if status == "queued" else "")
                + f". To schedule it, use task_id={new_id}.")

    def break_down_task(self, task: str, steps: Any, list: str = "", **_: Any) -> str:
        found, ticked_too = self._find_task(str(task)), False
        if found is not None and list and not {_stem(w) for w in _words(list)} & \
                {_stem(w) for w in _words(found.parent_note_title or found.title)}:
            found = None
        if found is None:
            found, ticked_too = self._note_item(str(task), list), True
        if found is None:
            raise ToolError(f"There is no item matching '{task}' in the user's lists.")
        if isinstance(steps, str):
            steps = [s for s in re.split(r"\s*(?:;|\n|\|)\s*", steps) if s.strip()]
        steps = [str(s).strip() for s in steps if str(s).strip()]
        if len(steps) < 2:
            raise ToolError("Give at least two steps.")
        writer = self._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin, so it can't change your lists yet. "
                            "Connect it in Preferences → Tasks.")
        from thirties_core.joplin_writeback import break_down_op
        status, msg = writer.submit(dict(break_down_op(found, steps), ticked_too=ticked_too))
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        note_id = found.parent_note_id if found.is_checkbox_item else found.id
        for step in steps:
            self.s.tasks.append(TaskItem(id=_checkbox_id(note_id, step), source_notebook=found.source_notebook,
                                         title=step, is_checkbox_item=True,
                                         parent_note_title=found.parent_note_title or found.title,
                                         parent_note_id=note_id, section=found.section,
                                         parent_task_id=found.id if found.is_checkbox_item else None))
        return (f"Broke '{found.title}' into {len(steps)} steps in Joplin: " + "; ".join(steps)
                + (" (Joplin will get them when it's open)." if status == "queued" else "."))

    # --- outcomes ---

    def log_outcome(self, event: Any, status: str, started: Any = None, finished: Any = None, duration: Any = None,
                    estimated: Any = False, note: str = "", **_: Any) -> str:
        try:
            st = SpanStatus(str(status).strip().lower())
        except ValueError:
            raise ToolError("status must be 'done', 'partial' or 'skipped'.")
        if st == SpanStatus.PLANNED:
            raise ToolError("status must be 'done', 'partial' or 'skipped'.")

        span = None
        try:
            span = self.pt._find_span(event)
        except Exception:
            span = None
        task = None if span else self._find_task(str(event))
        if span is None and task is None:
            raise ToolError(f"There is no schedule item or backlog task matching '{event}'.")

        start_dt = self.pt._start(started) if started not in (None, "") else None
        end_dt = self.pt._end(finished, start_dt or self.pt.day_plan.day_start) if finished not in (None, "") else None
        if start_dt and end_dt is None and duration not in (None, ""):
            end_dt = start_dt + timedelta(minutes=parse_duration_minutes(duration))
        if end_dt and start_dt is None and duration not in (None, ""):
            start_dt = end_dt - timedelta(minutes=parse_duration_minutes(duration))
        guessed = str(estimated).strip().lower() in ("true", "1", "yes")

        if span is None:
            # A backlog task done outside the plan: record it on the schedule if we know when.
            result = f"Marked '{task.title}' {st.value}"
            if st == SpanStatus.DONE:
                result += " " + self._complete_backlog_task(task)
            elif st == SpanStatus.SKIPPED and self._db:
                self._db.record_task_deferred(task.id)
            if start_dt or end_dt:
                end_dt = end_dt or start_dt + timedelta(minutes=30)
                start_dt = start_dt or end_dt - timedelta(minutes=30)
                logged = EventSpan(new_span_id(), task.title, start_dt, end_dt, SpanCategory.TASK, task_id=task.id,
                                   status=st, actual_start=start_dt, actual_end=end_dt, notes=note,
                                   time_source="estimated" if guessed or not (started and finished) else "reported")
                self.pt.day_plan.spans.append(logged)
                self.pt._persist(logged)
                result += f"; logged it {format_clock(start_dt)} – {format_clock(end_dt)}"
            return result + "."

        if span.source == "calendar" or span.is_envelope:
            raise ToolError(f"Outcomes are tracked for planned activities, not for {span.title}.")
        span.status = st
        if note:
            span.notes = note
        if st != SpanStatus.SKIPPED:
            planned_minutes = span.duration_minutes
            if start_dt and not end_dt:
                end_dt = start_dt + timedelta(minutes=planned_minutes)
            if end_dt and not start_dt:
                # "Took until 3:30": assume it started on time unless that's impossible.
                start_dt = span.start_dt if end_dt > span.start_dt else end_dt - timedelta(minutes=planned_minutes)
            span.actual_start = start_dt or span.start_dt
            span.actual_end = end_dt or span.end_dt
            if (span.actual_start, span.actual_end) != (span.start_dt, span.end_dt):
                # Show what really happened, in the thirties it touched; the exact times and the original
                # plan stay on the record for statistics.
                if span.planned_start is None:
                    span.planned_start, span.planned_end = span.start_dt, span.end_dt
                if span.actual_end - span.actual_start < timedelta(minutes=30):
                    span.start_dt, span.end_dt = span.actual_start, span.actual_end
                else:
                    span.start_dt, span.end_dt = thirty_of(span.actual_start), thirty_ceil(span.actual_end)
            if started not in (None, "") and (finished not in (None, "") or duration not in (None, "")) and not guessed:
                span.time_source = "reported"
            elif start_dt or end_dt:
                span.time_source = "estimated"
            else:
                span.time_source = "planned"
        self.pt._persist(span)
        result = f"Marked {span.title} {st.value}"
        if span.actual_start and st != SpanStatus.SKIPPED:
            result += f" ({format_clock(span.actual_start)} – {format_clock(span.actual_end)}, {span.time_source})"
        if span.task_id:
            task = next((t for t in self.s.tasks if t.id == span.task_id), None)
            if st == SpanStatus.DONE and task:
                result += " " + self._complete_backlog_task(task)
            elif st == SpanStatus.SKIPPED and self._db:
                count = self._db.record_task_deferred(span.task_id)
                result += f"; it has now been put off {count} times"
        return result + "."

    def log_energy(self, level: Any, note: str = "", **_: Any) -> str:
        try:
            value = int(str(level).strip())
        except ValueError:
            raise ToolError("level must be a number from 1 (drained) to 5 (great).")
        self.s.energy.record(value, at=self.pt._now(), note=note)
        return f"Noted energy {max(1, min(5, value))}/5."

    def review_day(self, **_: Any) -> str:
        plan = self.pt.day_plan
        now = self.pt._now()
        pending = unreviewed_spans(plan.spans, now, plan.day_start)
        if not pending:
            return "Everything that has ended so far has an outcome recorded."
        lines = [f"- {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title} id={s.id}" for s in pending]
        out = "No outcome recorded yet for:\n" + "\n".join(lines)
        tasks = [s for s in pending if s.task_id]
        if tasks and self.pt.scheduler:
            tomorrow = plan.target_date + timedelta(days=1)
            next_plan, _ = self.pt.scheduler.build_day_plan(tomorrow)
            proposals = []
            for s in tasks:
                need = s.duration_minutes
                slot = next(((a, b) for a, b in self.pt.open_windows_for(next_plan)
                             if (b - a).total_seconds() // 60 >= need), None)
                if slot:
                    proposals.append(f"- {s.title}: tomorrow {format_clock(slot[0])} ({format_duration(need)}), "
                                     "or back to the backlog")
                else:
                    proposals.append(f"- {s.title}: no {format_duration(need)} gap tomorrow; back to the backlog")
            out += "\nIf unfinished, suggested rollover:\n" + "\n".join(proposals)
        return out

    # --- rollover: yesterday's unfinished items, as one proposal ---

    def rollover_plan(self) -> List[Dict[str, Any]]:
        """Yesterday's planned items that ended with no outcome, each placed today: at the same time if that's
        open and still ahead, else in the first open stretch long enough; no start if nothing fits."""
        from datetime import datetime as _dt
        today = self._today()
        if self.pt.day_plan.target_date != today:
            return []
        yesterday = self.pt.plan_for(today - timedelta(days=1))
        now = self.pt._now()
        pending = unreviewed_spans(yesterday.spans, now, yesterday.day_start)
        floor = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
        windows = [(max(a, floor), b) for a, b in self.pt.open_windows() if b > floor]
        plan = []
        for s in pending:
            need = timedelta(minutes=max(30, s.duration_minutes))
            same = _dt.combine(today, s.start_dt.timetz())
            slot = next(((a, b) for a, b in windows if a <= same and same + need <= b), None)
            start = same if slot else None
            if slot is None:
                slot = next(((a, b) for a, b in windows if b - a >= need), None)
                start = slot[0] if slot else None
            if slot:
                i = windows.index(slot)
                windows[i:i + 1] = [w for w in ((slot[0], start), (start + need, slot[1])) if w[1] - w[0] >= timedelta(minutes=30)]
            plan.append({"span_id": s.id, "title": s.title, "minutes": int(need.total_seconds() // 60),
                         "start": start.isoformat() if start else None})
        return plan

    def _rollover_key(self) -> str:
        return f"rollover:{self._today().isoformat()}"

    def rollover_proposal(self, **_: Any) -> str:
        """The proposal, as text; it's remembered so a 'yes' can apply exactly this."""
        plan = self.rollover_plan()
        if not plan:
            return "Nothing from yesterday is waiting: everything has an outcome."
        if self._db:
            import json
            self._db.set_sync_meta(self._rollover_key(), json.dumps({"plan": plan, "state": "offered"}))
        from datetime import datetime as _dt
        lines = ["From yesterday, still open:"]
        fits = [p for p in plan if p["start"]]
        lines += [f"- {format_clock(_dt.fromisoformat(p['start']))} – {p['title']}" for p in fits]
        lines += [f"- {p['title']} (no room today: back to your lists)" for p in plan if not p["start"]]
        lines.append("Want me to put them in today like that?")
        return "\n".join(lines)

    def apply_rollover(self, skip: Any = None, **_: Any) -> str:
        """Apply the last rollover proposal; items in ``skip`` (titles or words) go back to the lists."""
        import json
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        if not raw:
            raise ToolError("There's no rollover proposal for today; ask for one first.")
        data = json.loads(raw)
        if data.get("state") == "applied":
            return "Yesterday's items were already rolled over."
        skip_words = {_stem(w) for w in _words(" ".join(skip) if isinstance(skip, list) else str(skip or ""))}
        from datetime import datetime as _dt
        yesterday = (self._today() - timedelta(days=1)).isoformat()
        moved, back = [], []
        for p in data["plan"]:
            skipped = skip_words and skip_words & {_stem(w) for w in _words(p["title"])}
            if p["start"] and not skipped:
                start = _dt.fromisoformat(p["start"])
                out = self.pt.execute("update_event", {"event": p["span_id"], "day": yesterday, "to_day": "today",
                                                       "start": format_clock(start)})
                if not out.startswith(NOT_DONE_PREFIX):
                    moved.append(f"{format_clock(start)} – {p['title']}")
                    continue
            self.pt.execute("log_outcome", {"event": p["span_id"], "day": yesterday, "status": "skipped"})
            back.append(p["title"])
        self._db.set_sync_meta(self._rollover_key(), json.dumps({**data, "state": "applied"}))
        parts = []
        if moved:
            parts.append("Rolled into today:\n" + "\n".join(f"- {m}" for m in moved))
        if back:
            parts.append("Back to your lists: " + ", ".join(back) + ".")
        return "\n".join(parts) or "Nothing to roll over."

    def rollover_pending(self) -> bool:
        """A proposal was offered today and not yet applied or declined."""
        import json
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        return bool(raw) and json.loads(raw).get("state") == "offered"

    def decline_rollover(self) -> None:
        import json
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        if raw:
            self._db.set_sync_meta(self._rollover_key(), json.dumps({**json.loads(raw), "state": "declined"}))

    # --- memory & profile ---

    def remember(self, note: str, **_: Any) -> str:
        if not str(note or "").strip():
            raise ToolError("The note is empty.")
        saved = self.s.memory.add(str(note))
        return f"Remembered: {saved.text} (note id {saved.id})."

    def forget(self, note_id: str, **_: Any) -> str:
        if not self.s.memory.forget(str(note_id)):
            raise ToolError(f"No memory note with id '{note_id}'.")
        return f"Forgot note {note_id}."

    def update_profile(self, section: str, text: str, replace: Any = False, **_: Any) -> str:
        name = next((s for s in self.s.profile.sections() if s.lower() == str(section).strip().lower()), None)
        if name is None:
            name = next((s for s in PROFILE_SECTIONS if str(section).strip().lower() in s.lower()), None)
        if name is None:
            raise ToolError(f"Unknown profile section '{section}'. Sections: {', '.join(PROFILE_SECTIONS)}.")
        if str(replace).strip().lower() in ("true", "1", "yes"):
            self.s.profile.set_section(name, str(text))
            return f"Rewrote the '{name}' section of the profile."
        self.s.profile.append(name, str(text))
        return f"Added to '{name}' in the profile."

    # --- history ---

    def _summarize_finished(self) -> None:
        try:
            self.s.conversations.summarize_finished(self._today())
        except Exception as e:
            logger.warning("Summarizing conversations failed: %s", e)

    def search_history(self, query: str, **_: Any) -> str:
        self._summarize_finished()
        hits = self.s.conversations.search(str(query))
        if not hits:
            return f"Nothing in past conversations mentions '{query}'."
        return "Matches:\n" + "\n".join(
            f"- {h['date']} ({h['role']}, conversation {h['conversation_id']}): {h['text'][:200]}" for h in hits)

    def list_conversations(self, from_day: Any = None, to_day: Any = None, **_: Any) -> str:
        today = self._today()
        start = parse_day(from_day, today) if from_day else today - timedelta(days=7)
        end = parse_day(to_day, today) if to_day else today
        self._summarize_finished()
        convs = self.s.conversations.between(start, end)
        if not convs:
            return "No conversations in that range."
        return "\n".join(f"- {c.plan_date.isoformat()} id={c.id}: {c.title or '(untitled)'} "
                         f"({c.user_turns} messages){' — ' + c.summary if c.summary else ''}" for c in convs)

    def read_conversation(self, conversation_id: str, **_: Any) -> str:
        conv = self.s.conversations.get(str(conversation_id))
        if conv is None:
            raise ToolError(f"No conversation with id '{conversation_id}'.")
        lines = [f"{m['role']}: {m.get('content', '')}" for m in conv.messages if m.get("role") in ("user", "assistant")]
        return f"Conversation from {conv.plan_date.isoformat()}:\n" + "\n".join(lines[-40:])

    # --- statistics & backlog ---

    def statistics_facts(self, days: int = 60) -> List[str]:
        if not self.pt.scheduler:
            return []
        now = self.pt._now()
        db = self.pt.scheduler.state_db
        spans = db.get_spans_between(now - timedelta(days=days), now + timedelta(days=1))
        deferred = sorted(((t.title, t.deferred_count) for t in self.s.tasks if t.deferred_count >= 2),
                          key=lambda x: -x[1])
        facts = fact_lines(spans, now, self.s.energy.entries(since=now - timedelta(days=days)), deferred)
        note = self.capacity_note()
        return ([note] if note else []) + facts

    def _history(self, days: int = 90):
        now = self.pt._now()
        return self.pt.scheduler.state_db.get_spans_between(now - timedelta(days=days), now + timedelta(days=1)), now

    def typical_duration(self, title: str) -> Optional[int]:
        """Minutes this kind of thing really takes the user (reliable times only), or None if unknown."""
        if not self.pt.scheduler:
            return None
        from thirties_core.stats import typical_minutes
        spans, _ = self._history()
        return typical_minutes(spans, title)

    def capacity_note(self) -> str:
        """A heads-up when today's plan is well beyond what the user usually gets through; else ''."""
        if not self.pt.scheduler:
            return ""
        from thirties_core.stats import daily_capacity_minutes, planned_minutes
        spans, now = self._history(60)
        capacity = daily_capacity_minutes([s for s in spans if s.start_dt.date() < self.pt.day_plan.target_date], now)
        planned = planned_minutes(self.pt.day_plan.active_spans())
        if capacity is None or planned < capacity * 1.25 or planned - capacity < 60:
            return ""
        return (f"Heads-up: about {format_duration(planned)} of activities are planned for this day; on a good day "
                f"you get through about {format_duration(capacity)}.")

    def get_statistics(self, days: Any = 60, **_: Any) -> str:
        try:
            n = int(str(days))
        except ValueError:
            n = 60
        facts = self.statistics_facts(n)
        return "\n".join(f"- {f}" for f in facts) if facts else "Not enough history yet for statistics."

    def week_overview(self, start_day: Any = None, days: Any = 7, **_: Any) -> str:
        if not self.pt.scheduler:
            raise ToolError("No schedule store available.")
        today = self._today()
        first = parse_day(start_day, today) if start_day else today
        try:
            count = max(1, min(14, int(str(days))))
        except ValueError:
            count = 7
        lines = []
        for offset in range(count):
            d = first + timedelta(days=offset)
            plan = d == self.pt.day_plan.target_date and self.pt.day_plan or self.pt.scheduler.build_day_plan(d)[0]
            items = [s for s in plan.spans_in_view() if not s.is_envelope and not s.tentative]
            done = sum(1 for s in items if s.status == SpanStatus.DONE)
            work = next((s for s in plan.active_spans() if s.category == SpanCategory.WORK), None)
            free = sum((b - a).total_seconds() for a, b in self.pt.open_windows_for(plan)
                       if (b - a).total_seconds() >= 1800 and a.hour >= 6) / 3600
            head = f"{d.strftime('%a %b %d')}: " + (f"work {format_clock(work.start_dt)}–{format_clock(work.end_dt)}; "
                                                    if work else "no work; ")
            head += f"{len(items)} planned" + (f", {done} done" if done else "") + f"; ~{free:.0f}h free"
            if items:
                head += " — " + ", ".join(s.title for s in items[:6]) + ("…" if len(items) > 6 else "")
            lines.append(head)
        return "\n".join(lines)

    # --- weather ---

    def _weather(self):
        if not self.pt.scheduler:
            return None
        from thirties_core.weather import weather_for
        return weather_for(self.pt.scheduler.config, self._db)

    def weather_facts(self) -> List[str]:
        """The rest of the day being planned (all of it, for another day)."""
        weather = self._weather()
        if weather is None:
            return []
        plan, now = self.pt.day_plan, self.pt._now()
        start = max(now, plan.day_start) if plan.day_start <= now < plan.day_end else plan.day_start
        sleep = next((s.start_dt for s in plan.active_spans() if s.is_envelope and s.category.value == "sleep"
                      and s.start_dt > start), plan.day_end)
        try:
            return weather.facts(start, sleep)
        except Exception as e:
            logger.info("No weather: %s", e)
            return []

    def weather_warning(self, span) -> Optional[str]:
        """'rain likely (70%) around 6 PM' when an outdoor-sounding item lands in bad weather, else None."""
        from thirties_core.weather import OUTDOOR_RE
        if not OUTDOOR_RE.search(span.title):
            return None
        weather = self._weather()
        try:
            return weather.bad_for_outdoors(span.start_dt, span.end_dt) if weather else None
        except Exception:
            return None

    def get_weather(self, **_: Any) -> str:
        facts = self.weather_facts()
        return "\n".join(facts) if facts else "No forecast available right now."

    def match_backlog(self, text: str, **_: Any) -> str:
        """Is the thing the user wants to schedule already on their lists? Decided here, not guessed."""
        ranked = rank_tasks(self.s.tasks, str(text), limit=6)
        same = [t for t in ranked if backlog_match(str(text), t) == "same"]
        similar = [t for t in ranked if backlog_match(str(text), t) == "similar"]
        if same:
            t = same[0]
            return f"Same task: {describe_task(t)}. Schedule it with this task_id."
        if similar:
            return ("No exact match; these are only similar and may be different things (ask the user, in your own "
                    "words, whether they mean one of them or something new):\n"
                    + "\n".join(f"- {describe_task(t)}" for t in similar[:3]))
        return "Nothing on the lists matches: treat it as a new item."

    def search_backlog(self, query: str, **_: Any) -> str:
        found = rank_tasks(self.s.tasks, str(query))
        if not found:
            return f"No backlog tasks match '{query}'."
        return "\n".join(f"- {describe_task(t)}" for t in found)

    # --- reminders and check-ins ---

    def reminder_due(self, date: Any = None, time: Any = None) -> "datetime":
        """When a reminder is due, from a day ('Saturday', 'next week') and a time ('3 PM', 'in 2 hours')."""
        from datetime import datetime as _dt, time as _time
        now = self.pt._now()
        time_text = str(time or "").strip().lower()
        m = re.match(r"^in\s+(.+)$", time_text)
        if m:
            return now + timedelta(minutes=parse_duration_minutes(m.group(1)))
        day_text = str(date or "").strip().lower()
        if day_text in ("next week", "next wk"):
            target = self._today() + timedelta(days=7 - self._today().weekday())
        else:
            target = parse_day(day_text or "today", self._today())
        if time_text:
            from thirties_core.clock import parse_clock
            t = parse_clock(time_text)
        elif target == self._today():  # "sometime today": a couple of hours from now, on the half hour
            soon = now + timedelta(hours=2)
            return soon.replace(minute=0 if soon.minute < 30 else 30, second=0, microsecond=0)
        else:
            t = _time(9, 0)
        due = _dt.combine(target, t, tzinfo=self.pt.day_plan.day_start.tzinfo)
        if not day_text and due <= now:
            due += timedelta(days=1)
        return due

    def set_reminder(self, text: str, date: Any = None, time: Any = None, **_: Any) -> str:
        if self.s.reminders is None:
            raise ToolError("Reminders aren't available here.")
        try:
            due = self.reminder_due(date, time)
        except (ClockError, ValueError) as e:
            raise ToolError(f"I couldn't read when: {e}")
        if due <= self.pt._now():
            raise ToolError(f"{format_clock(due)} on {due.strftime('%A')} has already passed.")
        rem = self.s.reminders.add(due, str(text))
        return f"Reminder set for {due.strftime('%A, %B %d')} at {format_clock(due)}: {rem.text} (id {rem.id})."

    def list_reminders(self, **_: Any) -> str:
        pending = self.s.reminders.pending() if self.s.reminders else []
        if not pending:
            return "No upcoming reminders."
        tz = self.pt.day_plan.day_start.tzinfo
        return "\n".join(f"- {r.due.astimezone(tz).strftime('%a %b %d')} {format_clock(r.due.astimezone(tz))}: "
                         f"{r.text} (id {r.id})" for r in pending)

    def cancel_reminder(self, reminder: str, **_: Any) -> str:
        pending = self.s.reminders.pending() if self.s.reminders else []
        q = {_stem(w) for w in _words(str(reminder))}
        found = [r for r in pending if r.id == str(reminder).strip()] or \
            [r for r in pending if q and q <= {_stem(w) for w in _words(r.text)}] or \
            [r for r in pending if q & {_stem(w) for w in _words(r.text)}]
        if len(found) != 1:
            raise ToolError("Which reminder? " + (self.list_reminders() if pending else "There are none."))
        self.s.reminders.set_state(found[0].id, "cancelled")
        return f"Cancelled the reminder: {found[0].text}."

    def mute_checkins(self, event: Any = None, until: Any = None, **_: Any) -> str:
        if self.s.mutes is None:
            raise ToolError("Check-ins aren't available here.")
        now = self.pt._now()
        span = self.pt._find_span(event) if event not in (None, "") else None
        if until in (None, ""):
            end = now + timedelta(days=2) if span else self.pt.day_plan.day_end
        elif str(until).strip().lower() == "tomorrow":
            end = self.pt.day_plan.day_end
        else:
            end = self.pt._end(until, now)
        self.s.mutes.mute(end, span.id if span else "")
        if span:
            return f"I won't ask about {span.title} again."
        return f"No more check-ins until {format_clock(end)}" + (" tomorrow." if end.date() > now.date() else ".")

    def handlers(self) -> Dict[str, Any]:
        return {
            "log_outcome": self.log_outcome,
            "log_energy": self.log_energy,
            "review_day": self.review_day,
            "remember": self.remember,
            "forget": self.forget,
            "update_profile": self.update_profile,
            "search_history": self.search_history,
            "list_conversations": self.list_conversations,
            "read_conversation": self.read_conversation,
            "get_statistics": self.get_statistics,
            "search_backlog": self.search_backlog,
            "add_task": self.add_task,
            "match_backlog": self.match_backlog,
            "get_weather": self.get_weather,
            "rollover_proposal": self.rollover_proposal,
            "apply_rollover": self.apply_rollover,
            "set_reminder": self.set_reminder,
            "list_reminders": self.list_reminders,
            "cancel_reminder": self.cancel_reminder,
            "mute_checkins": self.mute_checkins,
            "break_down_task": self.break_down_task,
            "week_overview": self.week_overview,
        }


__all__ = ["AssistantTools", "Services", "ASSISTANT_TOOL_SPECS", "ASSISTANT_READ_ONLY", "rank_tasks",
           "describe_task", "ToolError", "ClockError"]
