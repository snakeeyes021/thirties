"""Tools beyond the day's schedule: outcomes, review, memory, profile, history, statistics.

These sit next to the schedule tools in ``PlanTools`` and share its dispatcher and
contract: natural-language arguments in, short plain-English results out, and a
result starting with ``NOT_DONE`` when nothing changed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from thirties_core.clock import ClockError, format_clock, format_duration, parse_day, parse_duration_minutes, thirty_of
from thirties_core.horizons import HORIZON_READ_ONLY, HORIZON_TOOL_SPECS, horizon_handlers
from thirties_core.memory import (PROFILE_SECTIONS, CheckinMutes, ConversationStore, EnergyLog, MemoryStore,
                                  ProfileStore, ReminderStore)
from thirties_core.models import EventSpan, SpanCategory, SpanStatus, TaskItem, new_span_id
from thirties_core.kind_tools import KindTools, rollover_question
from thirties_core.kinds import ONGOING, put_off_counts, rolls_over
from thirties_core.stats import fact_lines, session_lines, unreviewed_spans

if TYPE_CHECKING:
    from thirties_core.joplin_engine import JoplinEngine
    from thirties_core.plan_tools import PlanTools

logger = logging.getLogger(__name__)


NOT_DONE_PREFIX = "NOT_DONE"  # plan_tools.NOT_DONE (not imported here: plan_tools imports this module)


class ToolError(Exception):
    """A tool call that cannot be carried out; nothing was changed."""


class OtherDaySpan(ToolError):
    """An item named by its id that is on another day's plan (``plan``), not the one the call is on."""

    def __init__(self, message: str, plan: Any) -> None:
        super().__init__(message)
        self.plan = plan


# An id, not a name: a plan item's (models.new_span_id, 12 hex) or a list item's (joplin_engine.item_id, 16 hex).
# 11-02: "the documentary last night", logged with yesterday's item id and no day, became an unplanned activity
# titled "14d911095040", done at 9:50 PM that evening.
ITEM_ID = re.compile(r"(cal:)?[0-9a-f]{12}([0-9a-f]{4})?")


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
            "finished_for_good": "true only when the user says an ongoing thing (project time, something worked on "
                                 "over many sessions) is finished for good; plain done on it means a session.",
            "note": "Optional note.",
            "new": "true when it's not one of the similar items offered: it's recorded as something done that wasn't "
                   "planned.",
        },
        "required": ["event", "status"],
    },
    {
        "name": "log_day_times",
        "description": "Record when the user actually woke, went to sleep, started or finished work (\"started work "
                       "at 9:40\", \"done with work\", an answer to the morning or end-of-work question). Only what "
                       "they said; Work and Sleep move to the real times.",
        "parameters": {"woke": "Clock time they woke up.", "slept": "Clock time they went to sleep.",
                       "work_started": "Clock time work started.",
                       "work_ended": "Clock time work ended, or 'now' if they're just done.",
                       "estimated": "true if the times are your best guess rather than what the user said."},
        "required": [],
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
        "name": "check_goal",
        "description": "Record the user's answer about a goal from GOALS TO CHECK: still current, done, or dropped.",
        "parameters": {"goal": "The goal, as written in the profile.",
                       "state": "'current' (still a goal), 'done' (finished) or 'dropped' (no longer wanted)."},
        "required": ["goal", "state"],
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
        "description": "Add a task to one of the user's lists (a Joplin note), on the list and in the section where "
                       "it belongs; when no list is obvious, the drop box. Choose yourself; don't ask the user where.",
        "parameters": {"title": "The task.", "list": "Which list (note title, or 'drop box').",
                       "section": "The heading within the note it belongs under (leave out for the obvious one; "
                                  "never a someday section for something dated or pressing).",
                       "reason": "Why that list and section, in a few words.",
                       "urgency": "If the user said: 'now', 'soon', 'whenever' or 'someday'.",
                       "due": "A date it must be done by, if they gave one.",
                       "start": "To also schedule it, the clock time it starts, e.g. '7:00 PM'. It's then linked: "
                                "done on the plan means ticked in the list.",
                       "duration": "How long, when scheduling it, e.g. '30 minutes'.",
                       "day": "The day, when scheduling it for another day.",
                       "schedule_as": "A shorter name for the schedule, when the list wording is long."},
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
        "description": "The morning's one question about yesterday's unmarked one-off to-dos (did any get "
                       "done?). Nothing is moved into today: what wasn't done waits on its list. Read-only.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "propose_leftovers",
        "description": "Your version of this morning's proposal for what's left from before (it starts as Python's "
                       "first guess). Every leftover goes in keep (with a natural 'when': today, tomorrow, this "
                       "week, this weekend, before <day>, by <day>, next week, next month, someday; Python works "
                       "out the days) or in drop (it has gone moot or can't happen any more, e.g. a one-time event "
                       "whose day is gone). Drops wait for the user's yes.",
        "parameters": {"keep": "A list of {item, when, why}.", "drop": "A list of {item, why}."},
        "required": ["keep"],
    },
    {
        "name": "place_leftover",
        "description": "Move an item left open on an earlier day where the user says: a natural horizon (today, "
                       "tomorrow, this week, this weekend, before <day>, next week, next month, someday) or "
                       "'drop' (let go; it stays on its own list).",
        "parameters": {"item": "The item's title.", "horizon": "Where it goes, in natural words, or 'drop'."},
        "required": ["item", "horizon"],
    },
    {
        "name": "set_item_note",
        "description": "Set the longer description of an item (\"add a note to X: ...\"): kept by Thirties, shown "
                       "when the item is expanded in the app, never in the item's title. Joplin isn't changed.",
        "parameters": {"item": "The item (its title or id).", "note": "The description; empty to clear it."},
        "required": ["item", "note"],
    },
    {
        "name": "leftover_action",
        "description": "Act on something left from before, as the user says (the same as the app's Before today "
                       "tab): accept (stands as proposed), done (it got done), drop, keep (a proposed drop kept "
                       "instead), undrop (back from the archive).",
        "parameters": {"item": "The item's title.", "action": "accept, done, drop, keep or undrop.",
                       "when": "For keep or undrop, where it goes if the user names it (otherwise the horizon the "
                               "proposal showed)."},
        "required": ["item", "horizon"],
    },
    {
        "name": "apply_rollover",
        "description": "Close the morning's question about yesterday's items. It moves nothing: record what got "
                       "done with log_outcome.",
        "parameters": {"skip": "Titles or words of items to leave out (optional)."},
        "required": [],
    },
    {
        "name": "set_reminder",
        "description": "\"Remind me to X\": puts X on the plan at that time (and on that day's list), with a ping "
                       "when it's time. Almost every reminder is a thing to do, so this is the default. Only for a "
                       "bare message the user asks for as a ping or alarm (\"ping me at 3 that the laundry's done\"), "
                       "set ping_only. If they don't say a time, pick a sensible one and tell them.",
        "parameters": {"text": "The thing to do (or, with ping_only, the message).",
                       "date": "Which day: 'today', 'tomorrow', 'Saturday', 'next week', 'Oct 20' (default today).",
                       "time": "Clock time like '3:00 PM', or 'in 2 hours' (default 9:00 AM).",
                       "task_id": "The list item it is, if it's on a list (it stays linked).",
                       "ping_only": "true for just a ping (an alarm), not an item on the plan."},
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
        "name": "set_urgency",
        "description": "Record how pressing an item on the user's lists is, when they say so (\"that's not "
                       "urgent\", \"the thermals are for this winter\", \"the ENT call has to happen this week\"). "
                       "It sticks: guesses never override it.",
        "parameters": {"task": "The task (title or task_id).",
                       "urgency": "'now' (today or tomorrow), 'soon' (a week or two), 'whenever' or 'someday'.",
                       "due": "A date it must be done by, if they gave one, e.g. 'Friday' or 'Nov 3'."},
        "required": ["task"],
    },
    {
        "name": "put_routine",
        "description": "Put one of the user's routines (a note of daily steps) on the plan as its steps. Each part is "
                       "one thirty and the steps done in it, in the routine's order. On today's plan it also goes on "
                       "today's list, as one item with its steps under it. Never add a routine to a list or as a "
                       "single item.",
        "parameters": {"routine": "Which routine (its note's name), e.g. 'Morning Routine'.",
                       "parts": "The thirties, as a list of {start, items}: e.g. [{\"start\": \"9:00 AM\", \"items\": "
                                "[\"Feed kitties\", \"Caffeinate\"]}, {\"start\": \"3:00 PM\", \"items\": [\"Shower\"]}]. "
                                "Items are the routine's steps, worded as in the note.",
                       "start": "Instead of parts: one thirty for every step.",
                       "day": "The day, if not today."},
        "required": ["routine"],
    },
    {
        "name": "add_to_today",
        "description": "Put things on today's list (the user's Today note), when the user says they mean to do them "
                       "today. Items from their lists stay linked: ticking either one ticks both. Scheduling a list "
                       "item for today adds it on its own.",
        "parameters": {"items": "The things, as a list: task titles or task_ids from the lists, or new things in the "
                                "user's words.",
                       "day": "A later day's list ('tomorrow', 'Monday'): it goes on Today that morning."},
        "required": ["items"],
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
ASSISTANT_TOOL_SPECS += HORIZON_TOOL_SPECS  # planning across horizons: thirties_core/horizons.py
from thirties_core.sun_notices import SUN_TOOL_SPECS  # noqa: E402
ASSISTANT_TOOL_SPECS += SUN_TOOL_SPECS  # a message at sunrise / sunset: thirties_core/sun_notices.py
from thirties_core.calendar_modes import CALENDAR_MODE_TOOL_SPECS  # noqa: E402
ASSISTANT_TOOL_SPECS += CALENDAR_MODE_TOOL_SPECS  # mine / ask / show / ignore per calendar: calendar_modes.py
ASSISTANT_READ_ONLY |= HORIZON_READ_ONLY


_STOPWORDS = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "with", "my", "some", "at", "about",
              "it", "is", "do", "that", "this", "i", "me", "up", "out", "from", "by", "or"}


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1 and w not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _short(title: str, limit: int = 80) -> str:
    """A long list line said in brief (11-05: whole paragraphs, with links and markup, in the morning's "Also
    pressing"): its markup dropped, cut at its first aside, and at a word under ``limit``."""
    from thirties_core.titles import title_of  # one headline logic for the chat and the app (11-06)
    return title_of(title, limit)


def _stem_words(text: str) -> set:
    return {_stem(w) for w in _words(text)}


def _same_words(a: str, b: str) -> bool:
    """The same wording: case, spacing, quotes and end punctuation aside."""
    def norm(x: str) -> str:
        return re.sub(r"\s+", " ", str(x).strip().strip("\"'“”‘’").rstrip(".!?").strip()).lower()
    return norm(a) == norm(b) != ""


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


def _as_flag(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


class AssistantTools(KindTools):
    """Outcome, review, memory, profile, history and statistics tools."""

    def __init__(self, plan_tools: "PlanTools", services: Services) -> None:
        self.pt = plan_tools
        self.s = services
        self._quiet_today = False  # put_routine lists its steps on Today together, as one group
        self._for_good = False  # an ongoing item's home stays open unless it's finished for good (_tick_twin)
        self._today_groups: Dict[tuple, set] = {}  # routine groups written to Today this session (Joplin may lag)
        self._routine_tools = None
        self._proposal_tools = None

    @property
    def routines(self):
        """Routines, daily anchors, the week, and the morning set-up (routines.py)."""
        if self._routine_tools is None:
            from thirties_core.routines import RoutineTools
            self._routine_tools = RoutineTools(self)
        return self._routine_tools

    @property
    def proposals(self):
        """The day's proposal (day_proposal.py)."""
        if self._proposal_tools is None:
            from thirties_core.day_proposal import DayProposal
            self._proposal_tools = DayProposal(self)
        return self._proposal_tools

    # --- helpers ---

    @property
    def _db(self):
        return self.pt.scheduler.state_db if self.pt.scheduler else None

    def _today(self):
        return self.pt.today()

    def _exact_task(self, ref: str) -> Optional[TaskItem]:
        """The list item this is, by id or by its exact wording (case, spacing and end punctuation aside); never
        a near match. Anything that changes a list goes through this: near matches ticked "Near vanity boxes" for
        "Kitty litter boxes" and put "My Dinner with Andre" on Today for "Dinner"."""
        ref = ref.strip()
        exact = [t for t in self.s.tasks if t.id == ref or _same_words(t.title, ref)]
        return exact[0] if exact else None

    def _no_exact_match(self, ref: str, what: str = "item") -> ToolError:
        """The refusal when only near matches exist: they're named, with ids, for the model to ask about."""
        near = [t for t in rank_tasks(self.s.tasks, ref, limit=3) if backlog_match(ref, t) != "different"]
        if not near:
            return ToolError(f"There is no {what} matching '{ref}' in the user's lists.")
        return ToolError(f"No list item is exactly '{ref}'. Only similar: " + "; ".join(describe_task(t) for t in near)
                         + ". If the user means one of these, call again with its task_id; if not, it's not on a list.")

    def _find_task(self, ref: str) -> Optional[TaskItem]:
        ref = ref.strip()
        exact = self._exact_task(ref)
        if exact:
            return exact
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

    def _complete_backlog_task(self, task: TaskItem, for_good: bool = False) -> str:
        self._for_good = for_good  # _tick_twin: an ongoing home item stays open unless it's finished for good
        if self._db:
            self._db.record_task_completed(task.id, self.pt.day_plan.target_date)
        if task.id.startswith("media:"):
            return ""
        if task.list_role == "routine":  # a template: the day's tick goes on Today's copy
            self._tick_on_today(task.title)
            return ""
        writer = self._writer()
        if writer is None:
            return "(not checked off in Joplin: Joplin isn't connected)"
        from thirties_core.joplin_writeback import complete_op
        status, msg = writer.submit(complete_op(task))
        if status in ("done", "queued"):
            self.s.tasks = [t for t in self.s.tasks if t.id != task.id]
            self._tick_twin(task, writer)
        return {"done": "and checked it off in Joplin",
                "queued": "(it'll be checked off in Joplin as soon as Joplin is open)",
                "not_allowed": "(not checked off in Joplin: Thirties isn't allowed to change Joplin; Preferences → Tasks)",
                "failed": f"(couldn't check it off in Joplin: {msg})"}[status]

    # --- the day's list: the Today note ---

    def _today_note(self) -> Optional[Dict[str, Any]]:
        from thirties_core.lists import ListCatalog
        from thirties_core.today import notes_with_role
        if self._db is None:
            return None
        found = notes_with_role(ListCatalog(self._db).roles(), self._joplin_notes(), "today")
        return found[0] if found else None

    def _joplin_up(self) -> bool:
        try:
            return bool(self.s.joplin.api_client.ping())
        except Exception:
            return False

    def morning_pending(self) -> bool:
        """The morning reset couldn't run yet (Joplin was closed at wake-up)."""
        return bool(self._db) and self._db.get_sync_meta(f"day_started:{self._today().isoformat()}") == "pending"

    def _tick_twin(self, task: TaskItem, writer) -> None:
        """A done item ticks its twin: the home item of a Today item, or the Today item of a home item."""
        from thirties_core.joplin_writeback import complete_op
        from thirties_core.today import TodayLinks, note_items
        today = self._today_note()
        if today is None or self._db is None:
            return
        links = TodayLinks(self._db)
        if task.parent_note_id == today["id"]:
            link = links.home_of(task.title)
            home = next((t for t in self.s.tasks if link and t.id == link["home_id"]), None)
            if home is not None and self.kind_of(home) == ONGOING and not getattr(self, "_for_good", False):
                return  # today's session is done; the thing itself goes on (B2)
            if home is not None:
                self._db.record_task_completed(home.id, self.pt.day_plan.target_date)  # done is done, on both
                if not home.id.startswith("media:") and home.list_role != "routine":  # nothing to tick in Joplin
                    # (a wishlist item), or nothing to write (a routine is a template)
                    writer.submit(complete_op(home))
                self.s.tasks = [t for t in self.s.tasks if t.id != home.id]
            return
        title = links.today_title_of(task.id)
        exact = next((text for text, ticked in note_items(today) if title and text.lower() == title and not ticked), None)
        if exact:
            writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": today["id"], "title": exact})
            self.s.tasks = [t for t in self.s.tasks if not (t.parent_note_id == today["id"] and t.title == exact)]

    def set_sun_notices(self, sunrise: Any = None, sunset: Any = None, **_: Any) -> str:
        from thirties_core.settings import settings_for
        from thirties_core.sun_notices import set_sun_notices
        if self.pt.scheduler is None:
            raise ToolError("There are no settings to change here.")
        return set_sun_notices(self.pt.scheduler.config, settings_for(self._db) if self._db else None, sunrise, sunset)

    def set_calendar_mode(self, calendar: Any = None, mode: Any = None, **_: Any) -> str:
        from thirties_core.calendar_modes import CalendarModeError, refresh_calendar_spans, set_calendar_mode
        from thirties_core.settings import settings_for
        if self.pt.scheduler is None:
            raise ToolError("There are no calendars here.")
        try:
            out = set_calendar_mode(self.pt.scheduler, settings_for(self._db) if self._db else None, calendar, mode,
                                    plan=self.pt.day_plan)
        except CalendarModeError as e:
            raise ToolError(str(e))
        if calendar not in (None, ""):
            refresh_calendar_spans(self.pt)
        return out

    def _done_unplanned(self, title: str, started: Any, finished: Any, duration: Any, note: str = "") -> str:
        """Something done that was never planned or listed ("emptied the litter boxes"): it goes on the record as done,
        when it was said unless they said when (10-30 sim: "refusing to log my litter box completion just because it
        wasn't pre-slotted")."""
        now = self.pt._now()
        start = self.pt._start(started) if started not in (None, "") else None
        end = self.pt._end(finished, start or self.pt.day_plan.day_start) if finished not in (None, "") else None
        minutes = parse_duration_minutes(duration) if duration not in (None, "") else 30
        end = end or (start + timedelta(minutes=minutes) if start else now)
        start = start or end - timedelta(minutes=minutes)
        self._not_in_future(start, end)
        reported = started not in (None, "") and (finished not in (None, "") or duration not in (None, ""))
        span = EventSpan(new_span_id(), title.strip(), start, end, SpanCategory.ACTIVITY, status=SpanStatus.DONE,
                         actual_start=start, actual_end=end, notes=note or "",
                         time_source="reported" if reported else "estimated")
        self.pt.day_plan.spans.append(span)
        self.pt.day_plan.spans.sort(key=lambda x: x.start_dt)
        self.pt._persist(span)
        self._tick_on_today(span.title)
        return (f"Recorded {span.title} done ({format_clock(start)} – {format_clock(end)}, "
                f"{span.time_source}); it wasn't planned.")

    def _not_in_future(self, start: Optional[datetime], end: Optional[datetime]) -> None:
        """Something done can't have finished, or started, later than now (a few minutes' slack for "done at 5"
        said at 4:58). Said of last night with no day, the times land on this day's evening (11-02)."""
        now = self.pt._now()
        late = next((t for t in (start, end) if t is not None and t > now + timedelta(minutes=10)), None)
        if late is not None:
            raise ToolError(f"{format_clock(late)} on {late.strftime('%A')} is later than now ({format_clock(now)}): "
                            "something done can't end in the future. If it happened on another day, give that day "
                            "(day='yesterday').")

    def _tick_on_today(self, title: str) -> None:
        from thirties_core.today import note_items
        today, writer = self._today_note(), self._writer()
        if today is None or writer is None:
            return
        exact = next((text for text, ticked in note_items(today) if text.lower() == title.strip().lower() and not ticked),
                     None)
        if exact:
            writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": today["id"], "title": exact})

    def sync_today(self) -> List[str]:
        """Catch up with edits made by hand: an item ticked on the Today note ticks its home item, and the other way
        round; a scheduled item taken off the Today note is mentioned once. Returns messages for the user."""
        import json
        from thirties_core.joplin_writeback import complete_op
        from thirties_core.today import TodayLinks, note_items
        today = self._today_note()
        writer = self._writer()
        if today is None or writer is None or self._db is None or not writer.allowed:
            return []
        if not self._joplin_up():
            return []  # nothing to deliver to; and queuing the same tick every two minutes would pile up
        notes = {n["id"]: n for n in self._joplin_notes()}
        on_today = {text.lower(): ticked for text, ticked in note_items(today)}
        links = TodayLinks(self._db)
        routine_ids = {n["id"] for n in self._routines()}
        ctx = self._kind_context()
        for link in links.all():
            home_items = {text.lower(): (text, ticked) for text, ticked in note_items(notes.get(link["home_note_id"]))}
            home = home_items.get(link["home_title"].lower())
            mine = on_today.get(link["today_title"])
            if home is None or mine is None:
                continue
            if mine and not home[1] and link["home_note_id"] not in routine_ids \
                    and self._link_kind(link, ctx) != ONGOING:  # a routine is never written to; ongoing stays open
                writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": link["home_note_id"],
                               "title": home[0]})
            elif mine and link["home_note_id"] in routine_ids:  # a routine step ticked by hand: it was done
                # today (its repeat schedule counts from the last time it was done)
                self._db.record_task_completed(link["home_id"], self._today())
            elif home[1] and not mine:
                exact = next(t for t, _ in note_items(today) if t.lower() == link["today_title"])
                writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": today["id"], "title": exact})
        messages = []
        key = f"today_removed_told:{self._today().isoformat()}"
        told = set(json.loads(self._db.get_sync_meta(key) or "[]"))
        plan = self.pt.plan_for(self._today())
        for span in plan.spans:
            linked = links.today_title_of(span.task_id) if span.task_id else None
            if (linked and linked not in on_today and span.status.value == "planned" and span.id not in told
                    and span.end_dt > self.pt._now()):
                messages.append(f"You took {span.title} off today's list; it's still planned at "
                                f"{format_clock(thirty_of(span.start_dt))}. Should I take it off the plan too?")
                told.add(span.id)
        if messages:
            self._db.set_sync_meta(key, json.dumps(sorted(told)))
        return messages

    def add_to_today(self, items: Any, **_: Any) -> str:
        from thirties_core.today import TodayLinks, note_items
        if isinstance(items, str):
            items = [x for x in re.split(r"\s*(?:;|\n|\|)\s*", items) if x.strip()]
        if self._db is not None and not self.pt.is_current(self.pt.day_plan) and \
                self.pt.day_plan.target_date > self._today():  # a later day's list: kept until that morning (O2)
            from thirties_core.day_lists import add_ahead
            return add_ahead(self, list(items or []))
        note = self._today_note()
        if note is None:
            raise ToolError("There's no Today note yet: make a note for today's list in Joplin, or mark one as "
                            "Today in Profile → Lists.")
        writer = self._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin, so it can't change your lists yet. "
                            "Connect it in Preferences → Tasks.")
        if isinstance(items, str):
            items = [x for x in re.split(r"\s*(?:;|\n|\|)\s*", items) if x.strip()]
        on_today = {text.lower() for text, _ in note_items(note)}
        on_today |= {t.title.lower() for t in self.s.tasks if t.parent_note_id == note["id"]}
        added, already, queued = [], [], False
        for name in items or []:
            task = self._exact_task(str(name))  # only the same item links; anything else is new, as worded
            title = task.title if task is not None else str(name).strip()
            if title.lower() in on_today:
                already.append(title)
                continue
            status, msg = writer.submit({"op": "add_task", "note_id": note["id"], "title": title})
            if status in ("not_allowed", "failed"):
                raise ToolError(msg)
            queued = queued or status == "queued"
            if task is not None and task.parent_note_id != note["id"]:
                TodayLinks(self._db).link(title, task)
            self.s.tasks.append(TaskItem(id=_checkbox_id(note["id"], title), source_notebook=note.get("source_notebook", ""),
                                         title=title, is_checkbox_item=True, parent_note_title=note["title"],
                                         parent_note_id=note["id"], list_role="today", urgency="now"))
            on_today.add(title.lower())
            added.append(title)
        if added and self._db is not None:  # today's, never a leftover of an earlier day (carry.py)
            from thirties_core.carry import note_added_today
            note_added_today(self._db, self._today(), added)
        parts = []
        if added:
            parts.append("Added to today's list: " + ", ".join(added) + (" (Joplin will get it when it's open)"
                                                                            if queued else "") + ".")
        if already:
            parts.append("Already on it: " + ", ".join(already) + ".")
        return " ".join(parts) or "Nothing to add."

    def _day_begun(self, adding: Any = ()) -> None:
        """Before the first write to Today for the day being planned, that day's morning reset (start_the_day).
        ``adding``: titles the caller is about to put on Today itself (the reset leaves them to it)."""
        plan = self.pt.day_plan
        if self._db is not None and self.pt.is_current(plan):
            from thirties_core.carry import settle
            settle(self, plan.target_date)  # what's left from before, sorted before anything is added today
        if self._db is not None and self.pt.is_current(plan) and \
                self._db.get_sync_meta(f"day_started:{plan.target_date.isoformat()}") != "done":
            self._adding = {str(t).strip().lower() for t in adding}
            try:
                self.start_the_day(plan.target_date)
            finally:
                self._adding = set()
        self.routines.set_up_day(plan)  # then the day's routines and anchors, once (planned before sunrise too)

    def list_today(self, span: EventSpan) -> str:
        """Everything on today's plan is on today's list (10-29: the plan and the Today note had drifted apart): a list
        item as its linked copy, a routine's step under the routine's name, anything else as it's worded. Work, sleep
        and calendar events stay off. Quietly: returns a note for the tool result, or ''."""
        if span.is_envelope or span.source == "calendar" or self._quiet_today:
            return ""
        task = next((t for t in self.s.tasks if span.task_id and t.id == span.task_id), None)
        if task is not None and task.list_role == "today":
            return ""
        self._day_begun({span.title})
        try:
            routine = self._routine_of(task.title if task is not None else span.title)
            if routine is not None:
                out = self._routine_on_today(routine[0], [routine[1]])
            else:
                out = self.add_to_today([task.title if task is not None else span.title])
        except ToolError:
            return ""
        return "(also added to today's list)" if out.startswith("Added") else ""

    def _routines(self) -> List[Dict[str, Any]]:
        from thirties_core.lists import ListCatalog
        from thirties_core.today import notes_with_role
        if self._db is None:
            return []
        return notes_with_role(ListCatalog(self._db).roles(), self._joplin_notes(), "routine")

    def _routine_of(self, title: str) -> Optional[Tuple[Dict[str, Any], str]]:
        """The routine note a step belongs to, and the step's exact wording; only the same wording counts."""
        from thirties_core.today import note_items
        want = str(title).strip().lower()
        for note in self._routines():
            for text, _ in note_items(note):
                if text.lower() == want:
                    return note, text
        return None

    def _routine_on_today(self, routine: Dict[str, Any], steps: List[str]) -> str:
        """A routine goes on today's list as one item named after its note, with its steps nested under it, each
        linked to its step in the routine note (10-26: it went on as one made-up line, "Morning Routine steps", which
        was then written into the routine note itself)."""
        from thirties_core.today import TodayLinks, group_items
        note = self._today_note()
        if note is None:
            raise ToolError("There's no Today note yet.")
        writer = self._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin.")
        name = routine["title"].strip()
        key = (self._today().isoformat(), name.lower())
        have = self._today_groups.get(key)
        if have is None:
            found = group_items(note, name)
            have = None if found is None else {t.lower() for t in found}
        missing = [s for s in steps if have is None or s.lower() not in have]
        if not missing:
            return "Already on it."
        ops = ([] if have is not None else [{"op": "add_task", "note_id": note["id"], "title": name}]) + \
              [{"op": "break_down", "note_id": note["id"], "parent_title": name, "steps": missing, "ticked_too": True}]
        for op in ops:
            status, msg = writer.submit(op)
            if status in ("not_allowed", "failed"):
                raise ToolError(msg)
        self._today_groups[key] = (have or set()) | {s.lower() for s in missing}
        links = TodayLinks(self._db)
        for step in missing:
            links.link(step, TaskItem(id=_checkbox_id(routine["id"], step), source_notebook=routine.get("source_notebook", ""),
                                      title=step, is_checkbox_item=True, parent_note_title=name,
                                      parent_note_id=routine["id"], list_role="routine"))
        return f"Added to today's list: {name} ({', '.join(missing)})."

    def put_routine(self, routine: str = "", parts: Any = None, start: Any = None, day: Any = None, **_: Any) -> str:
        """Put a routine on the plan as its steps: each part is one thirty holding the steps listed for it, in order.
        On today's plan the routine also goes on today's list, as one item with its steps nested."""
        import json
        from thirties_core.today import note_items
        routines = self._routines()
        want = {_stem(w) for w in _words(routine)}
        found = [n for n in routines if _same_words(n["title"], routine)] or \
            [n for n in routines if not routine or want & {_stem(w) for w in _words(n["title"])}]  # two routines
        # share "routine": the exact name wins
        if len(found) != 1:
            names = ", ".join(n["title"] for n in routines) or "none yet (mark a note as a routine in Profile → Lists)"
            raise ToolError(f"Which routine? Routines: {names}.")
        note = found[0]
        steps = [text for text, _ in note_items(note)]
        if isinstance(parts, str) and parts.strip():
            try:
                parts = json.loads(parts)
            except ValueError:
                raise ToolError("parts must be a list of {start, items}.")
        if not parts:
            if start in (None, ""):
                raise ToolError("Give start (one thirty for all of it) or parts (a start and the steps for each thirty).")
            parts = [{"start": start, "items": steps}]
        by_text = {s.lower(): s for s in steps}
        placed, already, lines, extra = [], [], [], []
        plan = self.pt.plan_for(day) if day not in (None, "") else self.pt.day_plan
        on_plan = {sp.title.lower() for sp in plan.spans if sp.status == SpanStatus.PLANNED or sp.status == SpanStatus.DONE}
        self._quiet_today = True
        try:
            for part in parts:
                names = part.get("items") or steps
                extra += [str(n).strip() for n in names if str(n).strip().lower() not in by_text]
                todo = [by_text.get(str(n).strip().lower(), str(n).strip()) for n in names]  # extras as said
                already += [t for t in todo if t.lower() in on_plan]
                todo = [t for t in todo if t.lower() not in on_plan]
                if not todo:
                    continue
                first = self.pt._start(part.get("start"))
                at = [first + timedelta(minutes=30 * k // len(todo)) for k in range(len(todo) + 1)]
                for k, text in enumerate(todo):
                    args = {"title": text, "start": format_clock(at[k]), "end": format_clock(at[k + 1]), "alongside": True}
                    if text.lower() in by_text:
                        args.update(category="task", task_id=_checkbox_id(note["id"], text))
                    if day not in (None, ""):
                        args["day"] = day
                    out = self.pt.execute("add_event", args)
                    if out.startswith(NOT_DONE_PREFIX):
                        raise ToolError(out[len(NOT_DONE_PREFIX):].lstrip(": "))
                    placed.append(text)
                lines.append(f"{format_clock(thirty_of(first))}: {', '.join(todo)}")
        finally:
            self._quiet_today = False
        if not placed:
            return f"{note['title']} is already on the plan."
        known = self.routines.known_extras(note)  # extras the user always places with it (Brush teeth): no asking
        extra = [x for x in extra if x.lower() not in known]
        self.routines.learn(note, parts)  # how the user places it becomes its usual placement (routines.py)
        listed = ""
        if self.pt.is_current(plan):
            self._day_begun(placed)
            try:
                listed = " " + self._routine_on_today(note, placed)
            except ToolError:
                listed = ""
        out = f"Put {note['title']} on the plan: " + "; ".join(lines) + "."
        if already:
            out += f" Already planned: {', '.join(already)}."
        if extra:  # the template only changes when the user asks (10-30: the model wrote Brush teeth into it)
            out += (f" {', '.join(extra)} isn't in the {note['title']} note: it's planned with it today only. Ask "
                    "the user whether to add it to the routine itself.")
        return out + listed

    # --- changing the backlog in Joplin (only when the user asked or agreed) ---

    def _joplin_notes(self) -> List[Dict[str, Any]]:
        """The Joplin notes Thirties reads, with their bodies (empty if Joplin isn't connected)."""
        try:
            notes = self.s.joplin.fetch_active_notes() if self.s.joplin is not None else []
        except Exception:
            return []
        return notes if isinstance(notes, list) else []

    def _notes(self) -> Dict[str, str]:
        """Note titles by id (horizons.py; add_task now goes through placement_lists.Placement)."""
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

    def add_task(self, title: str, list: str = "", section: str = "", start: Any = None, duration: Any = None,
                 day: Any = None, schedule_as: str = "", reason: str = "", urgency: str = "", due: Any = None,
                 **_: Any) -> str:
        # Which list and section: Python lists and checks, the model picks; never the user's job (placement_lists).
        from thirties_core.placement_lists import Placement
        placement = Placement.for_assistant(self)
        try:
            note = placement.resolve(list) if str(list or "").strip() else None
        except ToolError as e:
            note, why = None, str(e)
        else:
            why = "" if note is not None else "No list given."
        if note is None:
            raise ToolError(f"{why} Pick the list (and section) '{title}' belongs on and call add_task again with "
                            f"a short reason; don't ask the user. {placement.candidates_text(title)}")
        note_id, note_title = note["id"], note["title"]
        refused = placement.refusal(note) if placement.role(note) != "routine" else None
        if refused:
            raise ToolError(f"{refused} Pick another list. {placement.candidates_text(title)}")
        said = (self.pt.user_message or "").lower()
        asked = re.search(r"\b(add|put|include|make)\b[^.?!]*\b(to|in|into|part of)\b[^.?!]*\b(routine|"
                          + re.escape(note_title.lower()) + r")\b", said)  # "add brush teeth to my morning routine"
        if any(n["id"] == note_id for n in self._routines()) and self.pt.user_message is not None and not asked:
            raise ToolError(f"{note_title} is a routine, and the user didn't ask to change it. To plan a step with it "
                            "today only, give it to put_routine; ask before adding it to the routine itself.")
        if start not in (None, "") and any(n["id"] == note_id for n in self._routines()):
            # A routine is a template: scheduling it is put_routine, never a new line in it (10-26: "Morning Routine
            # steps" was written into the Morning Routine note).
            raise ToolError(f"{note_title} is a routine. To put it on the plan, use put_routine; its note only changes "
                            "when the user asks to change the routine itself.")
        writer = self._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin, so it can't change your lists yet. "
                            "Connect it in Preferences → Tasks.")
        if note.get("body") is None and self._joplin_up():  # known only from its items: read its sections
            live = self.s.joplin.api_client.get_note(note_id, fields="id,body")
            note["body"] = live.get("body") if live else None
        urgency = str(urgency or "").strip().lower()
        due_day = parse_day(str(due), self._today()) if due not in (None, "") else None
        dated = start not in (None, "") or day not in (None, "") or due_day is not None or urgency == "now"
        place, where = placement.choose_section(note, section or None, title, dated)
        op = {"op": "add_task", "note_id": note_id, "title": title.strip(), "place": place,
              "reason": str(reason or "")[:200]}
        if place is None:  # the note's sections weren't known (Joplin closed): the safe one when it's delivered
            op.update(safe=True, someday=[*placement.someday])  # (``list`` is the argument here)
        status, msg = writer.submit(op)
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        notebook = note.get("source_notebook") or next((t.source_notebook for t in self.s.tasks
                                                        if t.parent_note_id == note_id), "")
        new_id = _checkbox_id(note_id, title)
        new = TaskItem(id=new_id, source_notebook=notebook, title=title.strip(), is_checkbox_item=True,
                       parent_note_title=note_title, parent_note_id=note_id, section=place or "",
                       list_role=placement.role(note))
        self.s.tasks.append(new)
        if (urgency or due_day) and self._db is not None:
            from thirties_core.lists import URGENCIES, ListCatalog
            urgency = urgency if urgency in URGENCIES else ("now" if due_day and (due_day - self._today()).days <= 1
                                                            else "soon")
            ListCatalog(self._db).set_urgency(new_id, urgency, due_day, note_id=note_id, title=new.title, source="user")
            new.urgency, new.due = urgency, due_day.isoformat() if due_day else ""
        added = f"Added '{title.strip()}' to {note_title}{where}" + (" (Joplin will get it when it's open)"
                                                                     if status == "queued" else "")
        if start in (None, ""):
            return added + f". To schedule it, use task_id={new_id}."
        # Added and scheduled in one call, so the plan item is linked: two parallel calls in one reply couldn't
        # pass the task_id along, and ticking the plan item then left the list item open (10-24).
        args = {"title": (schedule_as or title).strip(), "start": start, "duration": duration or "30 minutes",
                "category": "task", "task_id": new_id}
        if day not in (None, ""):
            args["day"] = day
        out = self.pt.execute("add_event", args)
        if out.startswith(NOT_DONE_PREFIX):
            return f"{added}, but it isn't scheduled: {out[len(NOT_DONE_PREFIX):].lstrip(': ')} To schedule it, use task_id={new_id}."
        return f"{added}. {out}"

    def break_down_task(self, task: str, steps: Any, list: str = "", **_: Any) -> str:
        found, ticked_too = self._exact_task(str(task)), False
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
                    estimated: Any = False, note: str = "", finished_for_good: Any = False, new: Any = False, **_: Any) -> str:
        try:
            st = SpanStatus(str(status).strip().lower())
        except ValueError:
            raise ToolError("status must be 'done', 'partial' or 'skipped'.")
        for_good = str(finished_for_good).strip().lower() in ("true", "1", "yes")
        if st in (SpanStatus.PLANNED, SpanStatus.UNKNOWN):
            raise ToolError("status must be 'done', 'partial' or 'skipped'.")

        span = None
        try:
            span = self.pt._find_span(event)
        except OtherDaySpan as e:
            # Named by an id that's on another day's plan ("last night"): ids are unique, so that's where it goes
            saved = self.pt.day_plan
            self.pt.day_plan = e.plan
            try:
                result = self.log_outcome(event, status, started, finished, duration, estimated, note,
                                          finished_for_good, new)
            finally:
                self.pt.day_plan = saved
            return f"[{e.plan.target_date.strftime('%A, %B %d')}] {result}"
        except Exception:
            span = None
        if span is not None and str(event).strip() != span.id and span.source != "calendar":
            # Named by title while yesterday's copy is still open: which one isn't Python's guess to make (10-28:
            # "clearing yesterday's check-in" marked today's Email back GitHub skipped).
            yesterday = self.pt.plan_for(self.pt.day_plan.target_date - timedelta(days=1))
            older = [s for s in yesterday.spans if s.status == SpanStatus.PLANNED and s.id != span.id
                     and s.title.strip().lower() == span.title.strip().lower()]
            if older:
                raise ToolError(f"{span.title} is on this day's plan ({self.pt.describe(span)} [id {span.id}]) and still "
                                f"open on the day before ({self.pt.describe(older[0])} [id {older[0].id}, day "
                                f"{yesterday.target_date.strftime('%b %d')}]). Which one? Use the id (and that day).")
        task = None if span else self._exact_task(str(event))
        if span is None and task is not None:  # named by its list wording: it may be planned (10-30 sim: the choir
            # email, named with quotes the plan's lookup missed, was ticked in Joplin but nagged for hours as unmarked)
            span = next((s for s in self.pt.day_plan.spans if s.task_id == task.id
                         and s.status == SpanStatus.PLANNED), None)
            if span is not None:
                task = None
        if span is None and task is None:
            if ITEM_ID.fullmatch(str(event).strip()):
                raise ToolError(f"No item on any day's plan or list has the id {event}. Name it by its title, or "
                                "use an id from the schedule.")
            if st == SpanStatus.SKIPPED:
                raise ToolError(f"There is no schedule item or backlog task matching '{event}'.")
            if rank_tasks(self.s.tasks, str(event), limit=1) and not _as_flag(new):
                raise ToolError(str(self._no_exact_match(str(event), "schedule item or list item"))
                                + " If it's something else, call again with new=true to record it as done.")
            return self._done_unplanned(str(event), started, finished, duration, note)

        start_dt = self.pt._start(started) if started not in (None, "") else None
        end_dt = self.pt._end(finished, start_dt or self.pt.day_plan.day_start) if finished not in (None, "") else None
        if start_dt and end_dt is None and duration not in (None, ""):
            end_dt = start_dt + timedelta(minutes=parse_duration_minutes(duration))
        if end_dt and start_dt is None and duration not in (None, ""):
            start_dt = end_dt - timedelta(minutes=parse_duration_minutes(duration))
        guessed = str(estimated).strip().lower() in ("true", "1", "yes")
        if st != SpanStatus.SKIPPED:
            self._not_in_future(start_dt, end_dt)
        if span is not None and span.source == "calendar" and not span.tentative:
            # An event is something you'd mark done (soak 1): the outcome is Thirties', the calendar is untouched.
            from thirties_core.calendar_outcomes import log_calendar_outcome
            return log_calendar_outcome(self.pt, span, st, start_dt, end_dt, note, guessed)
        if (span is not None and st != SpanStatus.SKIPPED and start_dt is None and end_dt is None
                and duration in (None, "")):
            # Said with no time ("kitties are fed", a tick in the Agenda): it finished when it was said, as an
            # estimate (10-25: fed at 10:44, stored at the planned 10:00). Long after its time, nobody knows when:
            # the planned times stay, marked as planned. Ticked before its time, Thirties picks the times: on
            # blocks, and back to back with other early ticks (soak 3).
            now = self.pt._now()
            if now < span.start_dt and now >= span.start_dt - timedelta(hours=3) and not span.is_envelope:
                from thirties_core.placement import early_tick_times
                start_dt, end_dt = early_tick_times(self.pt.day_plan, span, now)
            elif span.start_dt - timedelta(hours=3) <= now <= span.end_dt + timedelta(hours=2):
                end_dt = now

        if span is None:
            # A backlog task done outside the plan: record it on the schedule if we know when.
            result = f"Marked '{task.title}' {st.value}"
            kind = self.kind_of(task)
            if st == SpanStatus.DONE and kind == ONGOING and not for_good:
                result += " " + self._worked_on(task)
                end_dt = end_dt or (None if start_dt else self.pt._now())  # the session is recorded, as said
            elif st == SpanStatus.DONE:
                result += " " + self._complete_backlog_task(task, for_good)
            elif st == SpanStatus.SKIPPED and self._db and put_off_counts(kind):
                self._db.record_task_deferred(task.id)
            if start_dt or end_dt:
                usual = self.pt._lasting().for_item(task.title).minutes if self._db else 0
                end_dt = end_dt or start_dt + timedelta(minutes=usual or 30)
                start_dt = start_dt or end_dt - timedelta(minutes=usual or 30)
                logged = EventSpan(new_span_id(), task.title, start_dt, end_dt, SpanCategory.TASK, task_id=task.id,
                                   status=st, actual_start=start_dt, actual_end=end_dt, notes=note,
                                   time_source="estimated" if guessed or not (started and finished) else "reported")
                self.pt.day_plan.spans.append(logged)
                self.pt._persist(logged)
                self._capture_context(logged)
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
                # "Done at 5:30" (an estimate, marked as one): ending within twice its planned length of the
                # planned start, it started on time and ran long; ending later, it started late and took as long
                # as planned. Always "on time" made a planned half-hour shower 3:00 – 5:50 (10-20).
                ran_long = span.start_dt < end_dt <= span.start_dt + timedelta(minutes=2 * planned_minutes)
                start_dt = span.start_dt if ran_long else end_dt - timedelta(minutes=planned_minutes)
            span.actual_start = start_dt or span.start_dt
            span.actual_end = end_dt or span.end_dt
            if (span.actual_start, span.actual_end) != (span.start_dt, span.end_dt):
                # The item now sits where it really happened, at its real times (the day view and lists show it
                # in the blocks it touched); the original plan stays on the record for statistics.
                if span.planned_start is None:
                    span.planned_start, span.planned_end = span.start_dt, span.end_dt
                span.start_dt, span.end_dt = span.actual_start, span.actual_end
            if started not in (None, "") and (finished not in (None, "") or duration not in (None, "")) and not guessed:
                span.time_source = "reported"
            elif start_dt or end_dt:
                span.time_source = "estimated"
            else:
                span.time_source = "planned"
        self.pt._persist(span)
        self._capture_context(span)
        result = f"Marked {span.title} {st.value}"
        if span.actual_start and st != SpanStatus.SKIPPED:
            result += f" ({format_clock(span.actual_start)} – {format_clock(span.actual_end)}, {span.time_source})"
        if not span.task_id and span.category in (SpanCategory.TASK, SpanCategory.ACTIVITY):
            same = self._exact_task(span.title)  # scheduled without its link, but worded exactly as a list item
            if same is not None:
                span.task_id = same.id
                self.pt._persist(span)
        kind = self.kind_of(span)
        if span.task_id:
            task = next((t for t in self.s.tasks if t.id == span.task_id), None)
            if st == SpanStatus.DONE and (self._routine_of(span.title) is not None
                                          or (task is not None and task.list_role == "routine")):
                # a routine note is a template: the day's ticks go on Today's copy, never into the routine
                self._tick_on_today(span.title)
                if self._db:
                    self._db.record_task_completed(span.task_id, self.pt.day_plan.target_date)
            elif st == SpanStatus.DONE and task and kind == ONGOING and not for_good:
                result += " " + self._worked_on(task)  # a session: the list item stays open (B2)
            elif st == SpanStatus.DONE and task:
                result += " " + self._complete_backlog_task(task, for_good)
            elif st == SpanStatus.SKIPPED and self._db and put_off_counts(kind):  # a skipped daily isn't put off
                count = self._db.record_task_deferred(span.task_id)
                result += f"; it has now been put off {count} times"
        elif st == SpanStatus.DONE and kind == ONGOING and not for_good:
            result += " (a session on it; it carries on)"
        elif st == SpanStatus.DONE:  # not on any list (a routine's extra step, a meal): its Today line is ticked too
            self._tick_on_today(span.title)
        return result + "."

    def _capture_context(self, span: EventSpan) -> None:
        """B7: what surrounded the outcome, kept as it is now (the plan changes later). Never blocks the outcome."""
        if self._db is None:
            return
        try:
            from thirties_core.day_times import night_sleep, wake_time
            from thirties_core.outcome_context import OutcomeContextStore
            plan = self.pt.day_plan
            history = self._db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
            bed = night_sleep(plan, history)
            OutcomeContextStore(self._db).record(plan, span, self.pt._now(), wake_time(plan, history),
                                                 bed.start_dt if bed else None)
        except Exception as e:
            logger.warning("Couldn't capture the outcome's context: %s", e)

    def log_day_times(self, woke: Any = None, slept: Any = None, work_started: Any = None, work_ended: Any = None,
                      estimated: Any = False, **_: Any) -> str:
        """B6: actual wake, sleep and work times, on the day's Sleep and Work (thirties_core.day_times)."""
        from thirties_core.day_times import DayTimesError, record_day_times
        plan = self.pt.day_plan
        history = self._db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_end) if self._db else None
        guessed = str(estimated).strip().lower() in ("true", "1", "yes")
        try:
            changed, said = record_day_times(plan, self.pt._now(), woke=woke, slept=slept, work_started=work_started,
                                             work_ended=work_ended, source="estimated" if guessed else "reported",
                                             history=history)
        except DayTimesError as e:
            raise ToolError(str(e))
        for span in changed:
            self.pt._persist(span)
            if plan.get_span(span.id) is None and span.overlaps(plan.day_start, plan.day_end):
                plan.spans.append(span)  # a late wake now reaches into this day: its morning isn't open time
                plan.spans.sort(key=lambda s: s.start_dt.timestamp())
        return "Recorded: " + ", ".join(said) + "."

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
        self.settle_moot(plan, now)  # what has gone moot is never asked about (B4)
        pending = unreviewed_spans(plan.spans, now, plan.day_start)
        if not pending:
            return "Everything that has ended so far has an outcome recorded."
        lines = [f"- {format_clock(s.start_dt)} – {format_clock(s.end_dt)}: {s.title} id={s.id}" for s in pending]
        out = "No outcome recorded yet for:\n" + "\n".join(lines)
        ctx = self._kind_context()
        tasks = [s for s in pending if s.task_id and rolls_over(self.kind_of(s, ctx))]
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
        """Yesterday's one-off items that ended with no outcome and haven't gone moot, each placed today: at the
        same time if that's open and still ahead, else in the first open stretch after that time long enough, else
        the first one at all (10-27: a 9 PM item was offered at 3); no start if nothing fits."""
        from datetime import datetime as _dt
        today = self._today()
        if self.pt.day_plan.target_date != today:
            return []
        yesterday = self.pt.plan_for(today - timedelta(days=1))
        now = self.pt._now()
        ctx = self._kind_context()
        self.settle_moot(yesterday, now, ctx)  # yesterday's breakfast isn't eaten today (10-29)
        done_ids = self._db.completed_since(today - timedelta(days=1)) if self._db else set()
        done_titles = {s.title.strip().lower() for p in (yesterday, self.pt.day_plan) for s in p.spans
                       if s.status == SpanStatus.DONE}
        pending = [s for s in unreviewed_spans(yesterday.spans, now, yesterday.day_start) if self._carries_over(s, ctx)
                   and s.task_id not in done_ids and s.title.strip().lower() not in done_titles]  # done another way
        floor = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
        windows = [(max(a, floor), b) for a, b in self.pt.open_windows() if b > floor]
        plan = []
        for s in pending:
            need = timedelta(minutes=max(30, s.duration_minutes))
            same = _dt.combine(today, s.start_dt.timetz())
            slot = next(((a, b) for a, b in windows if a <= same and same + need <= b), None)
            start = same if slot else None
            if slot is None:
                slot = next(((a, b) for a, b in windows if b > same and b - max(a, thirty_ceil(same)) >= need), None)
                start = max(slot[0], thirty_ceil(same)) if slot else None
            if slot is None:
                slot = next(((a, b) for a, b in windows if b - a >= need), None)
                start = slot[0] if slot else None
            if slot:
                i = windows.index(slot)
                windows[i:i + 1] = [w for w in ((slot[0], start), (start + need, slot[1])) if w[1] - w[0] >= timedelta(minutes=30)]
            plan.append({"span_id": s.id, "title": s.title, "minutes": int(need.total_seconds() // 60),
                         "start": start.isoformat() if start else None})
        return plan

    def _carries_over(self, span: EventSpan, ctx: Optional[Dict[str, Any]] = None) -> bool:
        """Only one-off things carry over: daily things (routine steps, meals, the user's daily anchors) and time on a
        project ("Torquio dev", a backlog's time) come round again anyway (kinds.rolls_over; 10-18, 10-27, 10-30)."""
        if span.title.strip().lower() in self.routines.daily_titles():  # anchors happen again by themselves
            return False
        return rolls_over(self.kind_of(span, ctx))

    def _rollover_key(self) -> str:
        return f"rollover:{self._today().isoformat()}"

    def today_carryover(self) -> Dict[str, List[str]]:
        """For the morning: what's still open on the Today note ("keep"), and pressing list items that aren't on it
        yet ("add": now, or due within two days; at most three)."""
        from thirties_core.backlog import DONE_HIDES_DAYS
        from thirties_core.today import note_items, with_done
        note = self._today_note()
        if note is None:
            return {"keep": [], "add": []}
        done = self._db.completed_since(self._today() - timedelta(days=DONE_HIDES_DAYS)) if self._db else set()
        routine = {n["title"].strip().lower() for n in self._routines()}
        routine |= {text.lower() for n in self._routines() for text, _ in note_items(n)}
        routine |= self.routines.daily_titles()  # anchors and a routine's extra steps don't carry over either
        done_on_plan = {t.lower() for t in self._done_on_plan(note)}
        ctx = self._kind_context()
        recent = [self._plan_peek(self._today() - timedelta(days=1)), self._plan_peek(self._today())]
        self.settle_moot(recent[0], ctx=ctx)
        moot = {s.title.strip().lower() for p in recent for s in p.spans if s.status == SpanStatus.UNKNOWN}
        keep = [text for text, ticked in with_done(note, done) if not ticked and text.lower() not in routine
                and text.lower() not in done_on_plan and text.lower() not in moot
                and rolls_over(self.kind_of(text, ctx))]  # never daily things or project time (R4)
        from thirties_core.today import group_items
        children = {c.lower() for k in keep for c in (group_items(note, k) or [])}
        keep = [k for k in keep if k.lower() not in children]  # 11-05: a group and each of its steps, as five lines
        on = {k.lower() for k in keep}
        today = self._today()

        def pressing(t: TaskItem) -> bool:
            if t.someday or t.id.startswith("media:") or t.list_role in ("today", "routine", "reference") \
                    or not rolls_over(self.kind_of(t, ctx)):
                return False
            if t.due:
                try:
                    return (date.fromisoformat(t.due) - today).days <= 2
                except ValueError:
                    return False
            return t.urgency == "now"
        add = [t.id for t in self.s.tasks if pressing(t) and t.title.lower() not in on][:3]
        return {"keep": keep, "add": add}

    def _done_on_plan(self, today: Dict[str, Any], day: Optional[date] = None) -> List[str]:
        """Open top-level lines on the Today note whose item is done on yesterday's or today's plan (10-25: "Charge
        watch", done the evening before, was offered again because its line was never ticked)."""
        from thirties_core.today import note_items
        day = day or self._today()
        recent = [self.pt.plan_for(day - timedelta(days=1)), self.pt.plan_for(day)]
        titles = {s.title.strip().lower() for p in recent for s in p.spans if s.status == SpanStatus.DONE}
        return [text for text, ticked in note_items(today) if not ticked and text.lower() in titles
                and re.search(r"^-\s*\[ \]\s*" + re.escape(text) + r"\s*$", today.get("body") or "", re.M)]

    def start_the_day(self, day: Optional[date] = None) -> str:
        """Each morning, before the proposal: routine notes start unticked (if the user allows it) and the Today
        note drops what was ticked off (the record keeps it). It runs once per day, at sunrise or before the first
        write to Today for that day, whichever is first (10-30: planned at 6:32 on a dark morning, the group
        written then was taken off at sunrise as "yesterday's"). Returns what was done, for the log."""
        from thirties_core.lists import ListCatalog
        from thirties_core.today import notes_with_role
        writer = self._writer()
        if writer is None or self._db is None or not writer.allowed:
            return ""
        day = day or self._today()
        key = f"day_started:{day.isoformat()}"
        if self._db.get_sync_meta(key) == "done":
            return ""
        from thirties_core.carry import settle
        leftovers = settle(self, day)  # before anything changes: what the morning asks about (carry.py)
        if not self._joplin_up():
            # Never queued: delivered later it would wipe ticks made since. Tried again while Joplin is closed.
            self._db.set_sync_meta(key, "pending")
            return ""
        self._db.set_sync_meta(key, "done")
        roles, notes = ListCatalog(self._db).roles(), self._joplin_notes()
        done = []
        if getattr(self.pt.scheduler.config.joplin, "reset_routines", True):
            for note in notes_with_role(roles, notes, "routine"):
                if re.search(r"^\s*-\s*\[[xX]\]", note.get("body") or "", re.M):
                    writer.submit({"op": "untick_all", "note_id": note["id"], "title": note["title"]})
                    done.append(f"unticked {note['title']}")
        today = self._today_note()
        if today:
            from thirties_core.today import group_items
            mine = self.routines.on_today_for(day)  # put on for this day already (Joplin was closed till now)
            for note in notes_with_role(roles, notes, "routine"):  # routines never carry over: yesterday's group goes
                if group_items(today, note["title"]) is not None and note["title"].strip().lower() not in mine:
                    writer.submit({"op": "remove", "note_id": today["id"], "title": note["title"].strip()})
                    done.append(f"took yesterday's {note['title']} off {today['title']}")
            from thirties_core.today import note_items
            daily = self.routines.daily_titles() - mine - {n["title"].strip().lower() for n in
                                                           notes_with_role(roles, notes, "routine")}
            # yesterday's anchors (Lunch) and a routine's extra steps happen again: they don't stay
            done_lines = set(self._done_on_plan(today, day))  # those go below
            for text, ticked in note_items(today):
                if not ticked and text.lower() in daily and text not in done_lines and re.search(
                        r"^-\s*\[ \]\s*" + re.escape(text) + r"\s*$", today.get("body") or "", re.M):
                    writer.submit({"op": "remove", "note_id": today["id"], "title": text})
                    self.s.tasks = [t for t in self.s.tasks if not (t.parent_note_id == today["id"] and t.title == text)]
                    done.append(f"took yesterday's {text} off {today['title']}")
        if today:  # done on the plan is done, even where its line was never ticked
            for text in self._done_on_plan(today, day):
                writer.submit({"op": "remove", "note_id": today["id"], "title": text})
                done.append(f"took {text} (done) off {today['title']}")
        if today:  # yesterday's leftovers wait on their own lists, or simply end: Today is today's plan (carry.py)
            from thirties_core.today import TodayLinks, note_items
            handled = {t.lower() for t in self._done_on_plan(today, day)} | self.routines.daily_titles() | {
                n["title"].strip().lower() for n in notes_with_role(roles, notes, "routine")}
            open_now = {t for t, ticked in note_items(today) if not ticked}
            links = TodayLinks(self._db)
            for text in leftovers.get("off_today", []):
                if text.lower() in handled or text.lower() in getattr(self, "_adding", set()) or text not in open_now:
                    continue
                writer.submit({"op": "remove", "note_id": today["id"], "title": text})
                self.s.tasks = [t for t in self.s.tasks if not (t.parent_note_id == today["id"] and t.title == text)]
                links.unlink(text)
                done.append(f"took {text} off {today['title']} (left from before)")
        if today and re.search(r"^\s*-\s*\[[xX]\]", today.get("body") or "", re.M):
            writer.submit({"op": "clear_ticked", "note_id": today["id"], "title": today["title"]})
            done.append(f"cleared what was done from {today['title']}")
        if today:  # what was planned ahead for this day reaches Today now, never before (O2)
            from thirties_core.day_lists import onto_today
            done += onto_today(self, day)
        return "; ".join(done)

    @property
    def before(self):
        from thirties_core.before_today import BeforeToday
        return BeforeToday(self)

    def rollover_proposal(self, **_: Any) -> str:
        """The morning's proposal for what's left from before (carry.py, before_today.py; 11-06: it takes effect
        by itself). Each one-off with no outcome gets Python's first guess, a natural horizon; a Keep for today
        goes on today's schedule now, the others wait at their horizon. The morning model may revise it
        (propose_leftovers), proposing drops; a drop is applied by itself once the day is over. Returns a short
        notice of what was done; the one question is only for what doesn't fit today. Never again per item."""
        import json
        from thirties_core.carry import OpenPool, default_when, mark_asked, settle, still_unknown
        if self._db is None:
            return "Nothing from yesterday is waiting: everything has an outcome or simply ended."
        day = self._today()
        left = settle(self, day)
        # what's on today's list with no time goes on today's schedule too (11-06: no "no time yet" any more)
        from thirties_core.day_lists import pencilled_in
        try:
            listed = pencilled_in(self)
        except Exception:
            listed = []
        tasks_by_title = {t.title.strip().lower(): t for t in self.s.tasks}
        _, list_overflow = self.before.place_today([
            {"title": t, "task_id": getattr(tasks_by_title.get(t.lower()), "id", ""), "start": day.isoformat(),
             "end": day.isoformat()} for t in listed])
        ask = still_unknown(self, day, left["ask"])
        if not ask:
            if list_overflow:
                from thirties_core.carry import notice_text
                return notice_text(0, 0, 0, [_short(o["title"]) for o in list_overflow])
            return "Nothing from yesterday is waiting: everything has an outcome or simply ended."
        mark_asked(self._db, ask, day)  # proposed now: never again
        week_start = getattr(self.pt.scheduler.config.general, "week_start", "mon") if self.pt.scheduler else "mon"
        by_title = {i["title"].lower(): i for i in left.get("pool_items", [])}
        keep = []
        for t in ask:
            item = by_title.get(t.lower()) or {"title": t, "task_id": "", "horizon": "week", "due": ""}
            keep.append({**item, **default_when(item.get("horizon", "week"), item.get("due", ""), day, week_start),
                         "why": "", "proposed_on": day.isoformat()})
        OpenPool(self._db).add(keep, day - timedelta(days=1))
        placed, overflow = self.before.place_today(keep)  # today's Keeps are on the schedule now
        overflow += list_overflow
        self._db.set_sync_meta(self._rollover_key(), json.dumps({
            "plan": [], "today": {"keep": [], "add": []}, "asked": ask, "state": "offered",
            "leftovers": {"keep": keep, "drop": []}, "overflow": [o["title"] for o in overflow],
            "offered_at": self.pt._now().isoformat()}))
        return self.leftover_notice()

    def leftover_notice(self) -> str:
        """The morning's word on the leftovers, as Python says it (the model says it in its own words)."""
        import json
        from thirties_core.carry import notice_text
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        data = json.loads(raw) if raw else {}
        lo = data.get("leftovers") or {}
        today = self._today().isoformat()
        on_today = [k for k in lo.get("keep", []) if k.get("end") and k["end"] <= today
                    and k["title"] not in data.get("overflow", [])]
        return notice_text(len(lo.get("keep", [])), len(on_today), len(lo.get("drop", [])),
                           [_short(t) for t in data.get("overflow", [])])

    def leftover_brief(self) -> str:
        """For the morning model: each leftover as proposed, with what it carries, and what doesn't fit today."""
        import json
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        data = json.loads(raw) if raw else {}
        lo = data.get("leftovers") or {}
        if not lo.get("keep") and not lo.get("drop"):
            return ""
        tasks = {t.id: t for t in self.s.tasks}
        lines = []
        for k in lo.get("keep", []):
            t = tasks.get(k.get("task_id") or "")
            facts = [f"kept: {k.get('when')}"] + ([f"due {t.due[:10]}"] if t is not None and t.due else []) + (
                [f"urgency {t.urgency}"] if t is not None and t.urgency else []) + (
                [f"list {t.parent_note_title}"] if t is not None and t.parent_note_title else ["no list"]) + (
                ["DOESN'T FIT TODAY"] if k["title"] in data.get("overflow", []) else [])
            lines.append(f"- {k['title']} ({'; '.join(facts)})")
        lines += [f"- {d['title']} (proposed drop: {d.get('why') or 'no reason given'})" for d in lo.get("drop", [])]
        return ("LEFTOVERS FROM BEFORE (unmarked: status unknown; Python's first guess, already in effect; today's "
                "are on the plan):\n" + "\n".join(lines))

    def propose_leftovers(self, keep: Any = None, drop: Any = None, **_: Any) -> str:
        """The model's version of the morning's leftover proposal: each item kept at a natural horizon (Python
        works out the days) or dropped. Kept items wait there at once; drops wait for the user's yes."""
        import json
        from thirties_core.carry import OpenPool, proposal_lines, resolve_when
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        data = json.loads(raw) if raw else {}
        pending = (data.get("leftovers") or {}).get("keep", []) + (data.get("leftovers") or {}).get("drop", [])
        if not pending:
            raise ToolError("There's no leftover proposal this morning.")
        by_title = {p["title"].lower(): p for p in pending}

        def find(name: Any) -> Dict[str, Any]:
            low = str(name or "").strip().lower()
            hit = by_title.get(low) or next((p for t, p in by_title.items() if low and (low in t or t in low)), None)
            if hit is None:
                raise ToolError(f"'{name}' isn't one of this morning's leftovers: " +
                                ", ".join(p["title"] for p in pending) + ".")
            return hit
        week_start = getattr(self.pt.scheduler.config.general, "week_start", "mon") if self.pt.scheduler else "mon"
        day, new_keep, new_drop, seen = self._today(), [], [], set()
        for k in (keep if isinstance(keep, list) else []):
            k = k if isinstance(k, dict) else {"item": k, "when": "this week"}
            item = find(k.get("item") or k.get("title"))
            try:
                window = resolve_when(k.get("when") or "this week", day, week_start)
            except ValueError as e:
                raise ToolError(f"{item['title']}: {e}")
            new_keep.append({**item, **window, "why": str(k.get("why") or ""), "pending_drop": False})
            seen.add(item["title"].lower())
        for d in (drop if isinstance(drop, list) else []):
            d = d if isinstance(d, dict) else {"item": d}
            item = find(d.get("item") or d.get("title"))
            new_drop.append({**item, "why": str(d.get("why") or ""), "pending_drop": True})
            seen.add(item["title"].lower())
        missing = [p["title"] for p in pending if p["title"].lower() not in seen]
        if missing:
            raise ToolError("Say what to do with each one; missing: " + ", ".join(missing) + ".")
        pool = OpenPool(self._db)
        for d in new_drop:
            d["proposed_on"] = day.isoformat()
        pool.add(new_keep + new_drop, day - timedelta(days=1))
        recs = pool.all()
        for r in new_drop + [k for k in new_keep if k["end"] > day.isoformat() or k["start"] > day.isoformat()]:
            rec = recs.get(r["title"].lower())
            if rec is not None and rec.get("slot"):
                self.before.take_off_today(rec)  # not today's any more: its slot goes
        placed, overflow = self.before.place_today([recs.get(k["title"].lower(), k) for k in new_keep])
        for k in new_keep:
            self._someday_in_joplin(recs.get(k["title"].lower(), k), k.get("when") == "someday")
        data["leftovers"] = {"keep": new_keep, "drop": new_drop}
        data["overflow"] = [o["title"] for o in overflow]
        self._db.set_sync_meta(self._rollover_key(), json.dumps(data))
        return ("In effect now, no approval needed: today's Keeps are on the plan" +
                (", except what doesn't fit: " + ", ".join(data["overflow"]) if data["overflow"] else "") +
                "; the rest wait at their horizon; drops are applied by themselves after today unless the user says "
                "otherwise. Say it as a short notice (see Before today), not a question:\n" +
                proposal_lines(new_keep, new_drop, _short))

    def apply_rollover(self, skip: Any = None, **_: Any) -> str:
        """Apply the last rollover proposal; items in ``skip`` (titles or words) go back to the lists (and off the
        Today note)."""
        import json
        from thirties_core.today import TodayLinks
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        if not raw:
            raise ToolError("There's no rollover proposal for today; ask for one first.")
        data = json.loads(raw)
        if data.get("state") == "applied":
            return "Yesterday's items were already rolled over."
        if "leftovers" in data or (not data.get("plan") and not (data.get("today") or {}).get("keep")
                                   and not (data.get("today") or {}).get("add")):
            dropped = self._drop_leftovers((data.get("leftovers") or {}).get("drop", []))
            from thirties_core.carry import OpenPool  # a yes accepts the rest as they stand: settled, not re-offered
            for k in (data.get("leftovers") or {}).get("keep", []):
                OpenPool(self._db).set(k["title"], settled=True, placed_by="user")
            self._db.set_sync_meta(self._rollover_key(), json.dumps({**data, "state": "applied"}))
            return (("Dropped: " + ", ".join(_short(t) for t in dropped) + ". " if dropped else "") +
                    "The rest wait where proposed.")  # copy-madlibs T3-009, T3-010
        skip_words = _stem_words(" ".join(skip) if isinstance(skip, list) else str(skip or ""))

        def skipped(title: str) -> bool:
            return bool(skip_words & _stem_words(title))
        from datetime import datetime as _dt
        yesterday = (self._today() - timedelta(days=1)).isoformat()
        moved, back = [], []
        for p in data["plan"]:
            if p["start"] and not skipped(p["title"]):
                start = _dt.fromisoformat(p["start"])
                out = self.pt.execute("update_event", {"event": p["span_id"], "day": yesterday, "to_day": "today",
                                                       "start": format_clock(start)})
                if not out.startswith(NOT_DONE_PREFIX):
                    moved.append(f"{format_clock(start)} – {p['title']}")
                    continue
            self.pt.execute("log_outcome", {"event": p["span_id"], "day": yesterday, "status": "skipped"})
            back.append(p["title"])
        carry = data.get("today") or {"keep": [], "add": []}
        homeless, dropped = [], []
        note = self._today_note() if carry["keep"] else None
        writer = self._writer() if note else None
        for title in carry["keep"]:
            if not skipped(title) or writer is None:
                continue
            writer.submit({"op": "remove", "note_id": note["id"], "title": title})
            self.s.tasks = [t for t in self.s.tasks if not (t.parent_note_id == note["id"] and t.title == title)]
            links = TodayLinks(self._db)
            (dropped if links.home_of(title) else homeless).append(title)
            links.unlink(title)
        tasks = {t.id: t for t in self.s.tasks}
        add = [tasks[t].title for t in carry["add"] if t in tasks and not skipped(tasks[t].title)]
        try:
            added = self.add_to_today(add) if add else ""
        except ToolError as e:  # the plan part is done already: say why the list part isn't
            added = f"(Couldn't put {', '.join(add)} on today's list: {e})"
        self._db.set_sync_meta(self._rollover_key(), json.dumps({**data, "state": "applied"}))
        parts = []
        if moved:
            parts.append("Rolled into today:\n" + "\n".join(f"- {m}" for m in moved))
        if back or dropped:
            parts.append("Back to your lists: " + ", ".join(back + dropped) + ".")
        if added:
            parts.append(added)
        if homeless:
            parts.append("Off today's list, with no other list to go back to: " + ", ".join(homeless)
                         + ". Want me to put " + ("it" if len(homeless) == 1 else "them") + " on one of your lists, "
                         "or let " + ("it" if len(homeless) == 1 else "them") + " go?")
        return "\n".join(parts) or "Today's list stays as it is."

    def place_leftover(self, item: str = "", horizon: str = "", **_: Any) -> str:
        """The user moved a leftover: to a natural horizon (today, tomorrow, this weekend, before friday, next
        month, someday) or dropped. Their word stands."""
        from thirties_core.carry import OpenPool, resolve_when
        text = str(horizon or "").strip().lower()
        pool = OpenPool(self._db) if self._db is not None else None
        if pool is None:
            raise ToolError("There's nothing waiting here.")
        if text in ("drop", "dropped", "let it go", "forget it"):
            rec = pool.place(str(item), "dropped")
            if rec is None:
                raise ToolError(f"'{item}' isn't waiting from an earlier day.")
            self._drop_leftovers([rec])
            return f"Dropped {rec['title']} (it stays on its own list)."
        week_start = getattr(self.pt.scheduler.config.general, "week_start", "mon") if self.pt.scheduler else "mon"
        try:
            window = resolve_when({"week": "this week", "deadline": "this month"}.get(text, text), self._today(),
                                  week_start)
        except ValueError as e:
            raise ToolError(str(e))
        h = "someday" if window["when"] == "someday" else ("today" if window["end"] == self._today().isoformat()
                                                            else "window")
        rec = pool.place(str(item), h, window)
        if rec is None:
            raise ToolError(f"'{item}' isn't waiting from an earlier day. To plan it, add it to the plan instead.")
        if h == "today":
            placed, overflow = self.before.place_today([rec])
            if overflow:
                return f"{rec['title']}: today, but there's no free time left for it."
        elif rec.get("slot"):
            self.before.take_off_today(rec)  # not today's any more
        self._someday_in_joplin(rec, h == "someday")
        return f"{rec['title']}: {window['when']}."

    def _drop_leftovers(self, items: List[Dict[str, Any]]) -> List[str]:
        """Applied drops (the user's word, or by themselves after their day): out of Thirties' pool, and in Joplin
        ticked and filed in the month's Dropped note beside the Done notes, with the reason (11-06, Matt: "that is
        the whole contract of the app"). An item with a home list is filed from there and its Today copy goes; one
        that's only on Today is filed from Today. A mere proposal never touches Joplin."""
        from thirties_core.carry import OpenPool
        from thirties_core.done_log import FILED_ROLES, file_dropped_op
        from thirties_core.today import TodayLinks, note_items
        titles = [i["title"] for i in items]
        if not titles or self._db is None:
            return []
        pool = OpenPool(self._db)
        recs = pool.all()
        for t in titles:
            if recs.get(t.lower()) is not None:
                pool.place(t, "dropped")
        note, writer = self._today_note(), self._writer()
        if writer is None or not getattr(writer, "allowed", True):
            return titles
        open_today = {x for x, ticked in note_items(note) if not ticked} if note is not None else set()
        links = TodayLinks(self._db)
        for t in titles:
            rec = recs.get(t.lower()) or {"title": t}
            if rec.get("filed_key"):
                continue  # filed already
            link = links.home_of(t)
            home_id = rec.get("task_id") or (link or {}).get("home_id") or ""
            home = next((x for x in self.s.tasks if x.id == home_id and x.list_role in FILED_ROLES), None) or \
                next((x for x in self.s.tasks if x.title.strip().lower() == t.strip().lower()
                      and x.list_role in FILED_ROLES and not x.id.startswith("media:")), None)
            op = None
            if home is not None:
                op = file_dropped_op(home.parent_note_id, home.title, home.parent_note_title or "", self._today(),
                                     0, rec.get("why") or "")
            elif note is not None and t in open_today:
                op = file_dropped_op(note["id"], t, note.get("title", ""), self._today(), 0, rec.get("why") or "")
            if op is not None:
                status, _ = writer.submit(op)
                if status in ("done", "queued"):
                    pool.set(t, filed_key=op["key"])
                    self.s.tasks = [x for x in self.s.tasks if not (home is not None and x.id == home.id)]
            if note is not None and t in open_today and (op is None or op["note_id"] != note["id"]):
                writer.submit({"op": "remove", "note_id": note["id"], "title": t})  # its Today copy
                self.s.tasks = [x for x in self.s.tasks if not (x.parent_note_id == note["id"] and x.title == t)]
                links.unlink(t)
        return titles

    def _someday_in_joplin(self, rec: Dict[str, Any], someday: bool) -> None:
        """Where the list already has a someday section, an item's place there follows its horizon (11-06): put
        to someday, it moves under that section; taken off someday, it moves back to the top. No new sections."""
        writer = self._writer()
        task = next((t for t in self.s.tasks if rec.get("task_id") and t.id == rec["task_id"]), None)
        if writer is None or task is None or task.list_role == "today" or bool(task.someday) == someday:
            return
        if someday:
            names = [n.lower() for n in getattr(self.pt.scheduler.config.joplin, "someday_sections", [])] \
                if self.pt.scheduler else []
            note = next((n for n in self._joplin_notes() if n["id"] == task.parent_note_id), None)
            from thirties_core.note_ops import _headings
            heading = next((h for h in _headings((note or {}).get("body") or "") if h.lower() in names), None)
            if heading is None:
                return
            writer.submit({"op": "move", "note_id": task.parent_note_id, "title": task.title, "section": heading,
                           "open_only": True})
        else:
            writer.submit({"op": "move", "note_id": task.parent_note_id, "title": task.title, "section": "",
                           "open_only": True})

    def set_item_note(self, item: str = "", note: str = "", **_: Any) -> str:
        """A description the user gives an item (11-06): in Thirties' records, by the item's id when it's on a list
        (so it follows the item), else by its wording."""
        from thirties_core.titles import ItemNotes, title_of
        if self._db is None:
            raise ToolError("Notes can't be kept here.")
        task = self._exact_task(str(item)) or next((t for t in self.s.tasks if t.id == str(item)), None)
        span = next((s for s in self.pt.day_plan.spans if s.id == str(item) or s.title.strip().lower()
                     == str(item).strip().lower()), None)
        title = task.title if task is not None else (span.title if span is not None else str(item))
        ItemNotes(self._db).set(note, task.id if task is not None else (span.task_id if span is not None and
                                                                          span.task_id else ""), title)
        return f"Noted for {title_of(title)}." if str(note).strip() else f"Cleared the note on {title_of(title)}."

    def _undrop_in_joplin(self, rec: Dict[str, Any]) -> str:
        """An applied drop taken back: the item goes back to its list, where it was, open again."""
        writer = self._writer()
        if not rec.get("filed_key") or writer is None:
            return ""
        status, msg = writer.submit({"op": "unfile_dropped", "key": rec["filed_key"], "title": rec["title"]})
        if status in ("done", "queued"):
            from thirties_core.carry import OpenPool
            OpenPool(self._db).set(rec["title"], filed_key="")
            return ""
        return f"(couldn't put it back in Joplin: {msg})"

    def rollover_pending(self) -> bool:
        """A proposal was offered today and not yet applied or declined."""
        import json
        raw = self._db.get_sync_meta(self._rollover_key()) if self._db else None
        if not raw:
            return False
        data = json.loads(raw)
        if data.get("state") != "offered":
            return False
        offered = data.get("offered_at")  # a morning offer left unanswered doesn't catch a "yes" at night
        return not offered or self.pt._now() - datetime.fromisoformat(offered) < timedelta(hours=4)

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
        from thirties_core.goals import GOAL_SECTIONS, dated, stamp_new_lines
        if str(replace).strip().lower() in ("true", "1", "yes"):
            content = str(text)
            if name in GOAL_SECTIONS:  # goals carry the day they were last known current (A7)
                content = stamp_new_lines(self.s.profile.sections().get(name, ""), content, self._today())
            self.s.profile.set_section(name, content)
            return f"Rewrote the '{name}' section of the profile."
        self.s.profile.append(name, dated(str(text), self._today()) if name in GOAL_SECTIONS else str(text))
        return f"Added to '{name}' in the profile."

    def check_goal(self, goal: str, state: str = "current", **_: Any) -> str:
        """A7: the user's answer about a stale goal: still current (dated today), done or dropped (taken out)."""
        from thirties_core.goals import check_goal
        try:
            section, content, said = check_goal(self.s.profile.sections(), str(goal), str(state), self._today())
        except ValueError as e:
            raise ToolError(str(e))
        self.s.profile.set_section(section, content)
        return said

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
        ctx = self._kind_context()
        deferred = sorted(((t.title, t.deferred_count) for t in self.s.tasks if t.deferred_count >= 2
                           and put_off_counts(self.kind_of(t, ctx))), key=lambda x: -x[1])
        facts = fact_lines(spans, now, self.s.energy.entries(since=now - timedelta(days=days)), deferred)
        kinds: Dict[tuple, str] = {}  # one reading per item, not per session (this runs for every prompt)

        def ongoing(s: EventSpan) -> bool:
            key = (s.title.strip().lower(), s.task_id)
            if key not in kinds:
                kinds[key] = self.kind_of(s, ctx)
            return kinds[key] == ONGOING
        facts += session_lines(spans, ongoing)
        from thirties_core.day_times import day_time_facts
        facts += day_time_facts(spans)
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

    def set_reminder(self, text: str, date: Any = None, time: Any = None, ping_only: Any = False,
                     task_id: str = "", **_: Any) -> str:
        from thirties_core.day_lists import explicit_ping
        if self.s.reminders is None:
            raise ToolError("Reminders aren't available here.")
        try:
            due = self.reminder_due(date, time)
        except (ClockError, ValueError) as e:
            raise ToolError(f"I couldn't read when: {e}")
        if due <= self.pt._now():
            raise ToolError(f"{format_clock(due)} on {due.strftime('%A')} has already passed.")
        when = f"{due.strftime('%A, %B %d')} at {format_clock(due)}"
        if str(ping_only).strip().lower() in ("true", "1", "yes") and explicit_ping(self.pt.user_message):
            rem = self.s.reminders.add(due, str(text))
            return f"Ping set for {when}: {rem.text} (id {rem.id})."
        # A thing to do (B5; 10-23: "It's a task, one for which completion should be trackable"): it goes on the plan,
        # linked to its list item if it is one, as a short item alongside whatever's there if the block is taken.
        # A bare ping is only for one asked for in those words. On a later day it reaches that day's list that morning.
        args = {"title": str(text).strip(), "start": format_clock(due), "duration": "15 min",
                "day": due.date().isoformat()}
        task = self._exact_task(str(task_id)) if str(task_id or "").strip() else None
        if task is not None and not task.id.startswith("media:"):
            args.update(category="task", task_id=task.id)
        out = self.pt.execute("add_event", args)
        if out.startswith(NOT_DONE_PREFIX):
            out = self.pt.execute("add_event", {**args, "alongside": True})
        if out.startswith(NOT_DONE_PREFIX):
            raise ToolError(out[len(NOT_DONE_PREFIX):].lstrip(": "))
        span_id = re.search(r"\[id (\w+)\]", out).group(1)
        listed = " and on today's list" if "also added to today's list" in out else ""
        if due.date() == self._today() and not listed:
            try:
                listed = " and on today's list" if self.add_to_today(
                    [task.title if task is not None else str(text).strip()]).startswith("Added") else ""
            except ToolError:
                pass
        rem = self.s.reminders.add(due, str(text), span_id=span_id)
        return f"On the plan for {when}{listed}, with a ping when it's time: {rem.text} (item id {span_id})."

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

    def set_urgency(self, task: str, urgency: str = "", due: Any = None, **_: Any) -> str:
        from thirties_core.lists import URGENCIES, ListCatalog, urgency_label
        found = self._exact_task(str(task))
        if found is None:
            raise self._no_exact_match(str(task))
        if self._db is None:
            raise ToolError("Urgency can't be saved here.")
        due_day = parse_day(str(due), self._today()) if due not in (None, "") else None
        urgency = str(urgency or "").strip().lower() or ("now" if due_day and (due_day - self._today()).days <= 1
                                                          else "soon" if due_day else "")
        if urgency not in URGENCIES:
            raise ToolError("urgency must be 'now', 'soon', 'whenever' or 'someday'.")
        ListCatalog(self._db).set_urgency(found.id, urgency, due_day, note_id=found.parent_note_id or found.id,
                                          title=found.title, source="user")
        found.urgency, found.due, found.someday = urgency, due_day.isoformat() if due_day else "", urgency == "someday"
        return f"Noted: '{found.title}' is {urgency_label(found)}."

    def handlers(self) -> Dict[str, Any]:
        return {
            "log_outcome": self.log_outcome,
            "log_energy": self.log_energy,
            "log_day_times": self.log_day_times,
            "review_day": self.review_day,
            "remember": self.remember,
            "forget": self.forget,
            "update_profile": self.update_profile,
            "check_goal": self.check_goal,
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
            "place_leftover": self.place_leftover,
            "propose_leftovers": self.propose_leftovers,
            "set_item_note": self.set_item_note,
            "leftover_action": lambda item="", action="", when="", **_: self.before.action(item, action, when),
            "set_reminder": self.set_reminder,
            "list_reminders": self.list_reminders,
            "cancel_reminder": self.cancel_reminder,
            "mute_checkins": self.mute_checkins,
            "set_urgency": self.set_urgency,
            "add_to_today": self.add_to_today,
            "put_routine": self.put_routine,
            "break_down_task": self.break_down_task,
            "week_overview": self.week_overview,
            **self.routines.handlers(),
            **self.proposals.handlers(),
            **horizon_handlers(self),
            "set_sun_notices": self.set_sun_notices,
            "set_calendar_mode": self.set_calendar_mode,
            **self.lists.handlers(),  # list structure, drop box, done items, triage, pencilled in (list_tools.py)
        }

    @property
    def lists(self):
        from thirties_core.list_tools import ListTools
        return ListTools(self)


__all__ = ["AssistantTools", "Services", "ASSISTANT_TOOL_SPECS", "ASSISTANT_READ_ONLY", "rank_tasks",
           "describe_task", "ToolError", "ClockError"]
