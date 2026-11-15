"""Tools for projects, epics, estimates and the lists themselves.

The thin layer the brains call; the logic and the data live in project_facts.py, project_pieces.py,
list_settings.py, efforts.py and note_ops.py. Same contract as the other tools: natural-language arguments,
short plain results, ToolError when nothing changed. Every Joplin change goes through JoplinWriter.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from thirties_core.models import SpanStatus

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools

logger = logging.getLogger(__name__)

PROJECT_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "set_project_part",
        "description": "Record that something the user works on is part of one of their projects, when it isn't a "
                       "line in the project's list (\"the trellis is part of Back Garden\"). Time on it then counts as "
                       "time on the project, and it's planned with the project's other pieces.",
        "parameters": {"name": "The part, in the user's words.", "project": "The project's list (its note's name).",
                       "remove": "true to forget it."},
        "required": ["name"],
    },
    {
        "name": "set_epic",
        "description": "Group items from the user's lists into an epic (a named larger goal spanning lists), or "
                       "change one. Items are exact list items (title or task_id); a whole list can belong too.",
        "parameters": {"epic": "The epic's name.", "items": "Items to add, as a list.",
                       "lists": "Whole lists to add (their note names), as a list.",
                       "remove": "Items or lists to take out of it, as a list.",
                       "delete": "true to forget the epic (the items stay on their lists)."},
        "required": ["epic"],
    },
    {
        "name": "epic_report",
        "description": "How an epic is going: what's done and open, time spent on it in a period, and what's next. "
                       "With no epic, every epic in one line each.",
        "parameters": {"epic": "The epic's name (none: all of them).", "period": "e.g. 'this week' (the default), "
                                                                                  "'this month'."},
        "required": [],
    },
    {
        "name": "plan_epic",
        "description": "Plan an epic into a period: its next open items (in their lists' order) are dropped into it, "
                       "like pin_to_period.",
        "parameters": {"epic": "The epic's name.", "period": "e.g. 'this week', 'next month'.",
                       "count": "How many of its open items (default 3)."},
        "required": ["epic", "period"],
    },
    {
        "name": "set_estimate",
        "description": "Record the user's own estimate of how much work an item needs, in their words and unit "
                       "(\"about 3 hours\", \"2 thirties\", \"5 points\"). It's theirs: guesses never replace it.",
        "parameters": {"task": "The item (title or task_id).",
                       "estimate": "Their estimate, e.g. '3 hours', '90 min', '2 thirties', '1.5 days', '5 points'; "
                                   "a bare number is in their usual unit. Empty to forget it."},
        "required": ["task"],
    },
    {
        "name": "set_effort_unit",
        "description": "The unit the user estimates in (their choice): minutes, thirties, hours, days or points, "
                       "and for points or days how long one is.",
        "parameters": {"unit": "'minutes', 'thirties', 'hours', 'days' or 'points'.",
                       "length": "For points or days: how long one is, e.g. '1 hour', '6 hours'."},
        "required": ["unit"],
    },
    {
        "name": "done_by_someone",
        "description": "Someone else at home did one of the user's items (\"Sam took out the trash\"): it's "
                       "finished, ticked on its list like any done item, never rolled over, and not counted as the "
                       "user's time.",
        "parameters": {"event": "The schedule item (id or title) or list item.", "who": "Who did it."},
        "required": ["event"],
    },
    {
        "name": "create_list",
        "description": "Make a new list (a Joplin note) in one of the notebooks Thirties reads, when the user asks "
                       "for one or agrees to one you suggested. Optionally with its first items.",
        "parameters": {"title": "The new list's name.", "notebook": "Which notebook it goes in.",
                       "role": "What it's for: 'list', 'project', 'menu', 'backlog', 'dropbox' or 'reference'.",
                       "items": "Its first items, as a list."},
        "required": ["title", "notebook"],
    },
    {
        "name": "set_list_settings",
        "description": "How Thirties treats the lists, when the user says: one-off notes (a to-do note with no "
                       "checklist in it) as 'item' (planned), 'someday' (kept, never pushed) or 'ignore' (not "
                       "read); and the note done items are filed in (an existing note), or 'monthly' for Thirties' "
                       "own monthly Done notes.",
        "parameters": {"one_off_notes": "'item', 'someday' or 'ignore'.",
                       "done_note": "The note's name, or 'monthly'."},
        "required": [],
    },
]
PROJECT_READ_ONLY = {"epic_report"}

AGREED_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|please|do it|sounds good|make|create|start|"
                       r"set up|new (?:list|note))\b", re.I)


def _tool_error(msg: str):
    from thirties_core.assistant_tools import ToolError
    return ToolError(msg)


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json
            try:
                return [str(x) for x in json.loads(text)]
            except ValueError:
                pass
        return [x.strip() for x in re.split(r"\n|;|,(?![^()]*\))", text) if x.strip()]
    return [str(x) for x in value if str(x).strip()]


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


class ProjectTools:

    def __init__(self, assistant: "AssistantTools") -> None:
        self.a = assistant

    @property
    def db(self):
        return self.a._db

    def _facts(self):
        from thirties_core.project_facts import ProjectFacts
        if self.db is None:
            raise _tool_error("This can't be kept here.")
        return ProjectFacts(self.db)

    def _note(self, ref: str, roles: Optional[tuple] = None) -> Dict[str, Any]:
        from thirties_core.placement_lists import Placement
        placement = Placement.for_assistant(self.a)
        note = placement.resolve(ref)
        if roles and placement.role(note) not in roles:
            raise _tool_error(f"{note['title']} isn't a project's list (it's {placement.role(note) or 'not sorted'}).")
        return note

    def _task(self, ref: str):
        found = self.a._exact_task(str(ref))
        if found is None:
            raise self.a._no_exact_match(str(ref))
        return found

    # --- a part of a project ---

    def set_project_part(self, name: str = "", project: str = "", remove: Any = False, **_: Any) -> str:
        from thirties_core.project_facts import norm
        key = norm(name)
        if not key:
            raise _tool_error("Say the part's name.")
        facts = self._facts()
        if _truthy(remove):
            if facts.get("part", key) is None:
                raise _tool_error(f"'{name}' isn't recorded as part of a project.")
            facts.delete("part", key)
            return f"Forgot that {name.strip()} is part of a project."
        if not str(project or "").strip():
            raise _tool_error("Say which project it's part of.")
        note = self._note(project, ("project", "backlog"))
        facts.set("part", key, {"name": name.strip(), "note_id": note["id"], "project": note["title"]})
        return f"Noted: {name.strip()} is part of {note['title']}. Time on it counts as time on {note['title']}."

    # --- epics ---

    def _epic(self, name: str) -> Dict[str, Any]:
        from thirties_core.project_facts import norm
        epic = self._facts().get("epic", norm(name))
        if epic is None:
            names = ", ".join(e["name"] for e in self._facts().all("epic").values()) or "none yet"
            raise _tool_error(f"There's no epic '{name}'. Epics: {names}.")
        return epic

    def members(self, epic: Dict[str, Any]) -> List[Any]:
        """The epic's items still on the lists, in their lists' order: its own items, then its lists' items."""
        items, lists = set(epic.get("items") or []), set(epic.get("lists") or [])
        return [t for t in self.a.s.tasks if not t.id.startswith("media:")
                and (t.id in items or (t.parent_note_id in lists and t.list_role != "today"))]

    def set_epic(self, epic: str = "", items: Any = None, lists: Any = None, remove: Any = None,
                 delete: Any = False, **_: Any) -> str:
        from thirties_core.project_facts import norm
        key = norm(epic)
        if not key:
            raise _tool_error("Say the epic's name.")
        facts = self._facts()
        if _truthy(delete):
            if facts.get("epic", key) is None:
                raise _tool_error(f"There's no epic '{epic}'.")
            facts.delete("epic", key)
            return f"Forgot the epic {epic.strip()} (its items stay on their lists)."
        rec = facts.get("epic", key) or {"name": epic.strip(), "items": [], "lists": []}
        added, taken = [], []
        for ref in _as_list(items):
            t = self._task(ref)
            if t.id not in rec["items"]:
                rec["items"].append(t.id)
                added.append(t.title)
        for ref in _as_list(lists):
            note = self._note(ref)
            if note["id"] not in rec["lists"]:
                rec["lists"].append(note["id"])
                added.append(f"all of {note['title']}")
        for ref in _as_list(remove):
            t = self.a._exact_task(ref)
            if t is not None and t.id in rec["items"]:
                rec["items"].remove(t.id)
                taken.append(t.title)
                continue
            try:
                note = self._note(ref)
            except Exception:
                note = None
            if note is not None and note["id"] in rec["lists"]:
                rec["lists"].remove(note["id"])
                taken.append(note["title"])
        if not added and not taken:
            raise _tool_error("Give items or lists to add to the epic, or to remove from it.")
        facts.set("epic", key, rec)
        out = f"Epic {rec['name']}"
        if added:
            out += ": added " + "; ".join(added)
        if taken:
            out += (", and" if added else ":") + " took out " + "; ".join(taken)
        return out + "."

    def _period(self, text: Any):
        from thirties_core.horizons import HorizonPlanner
        planner = HorizonPlanner(self.a.pt)
        return planner, planner.period(text or "this week")

    def _minutes_on(self, titles: set, ids: set, start: date, end: date) -> int:
        pt = self.a.pt
        try:
            a, b = pt.plan_for(start).day_start, pt.plan_for(end).day_end
        except Exception:
            return 0
        total = 0
        for sp in pt._state_db.get_spans_between(a, b) if pt._state_db is not None else []:
            if sp.status not in (SpanStatus.DONE, SpanStatus.PARTIAL):
                continue
            if (sp.task_id and sp.task_id in ids) or sp.title.strip().lower() in titles:
                s, e = sp.actual_start or sp.start_dt, sp.actual_end or sp.end_dt
                total += max(0, int((e - s).total_seconds() // 60))
        return total

    def epic_report(self, epic: str = "", period: Any = None, **_: Any) -> str:
        from thirties_core.clock import format_duration
        facts = self._facts()
        epics = [self._epic(epic)] if str(epic or "").strip() else list(facts.all("epic").values())
        if not epics:
            return "No epics yet."
        planner, p = self._period(period)
        done_ids = self.db.completed_since(p.start - timedelta(days=366))
        done_in = self.db.completed_since(p.start)
        lines = []
        for e in epics:
            members = self.members(e)
            ids = {t.id for t in members} | set(e.get("items") or [])
            open_ = [t for t in members if t.id not in done_ids]
            finished = [i for i in ids if i in done_ids]
            minutes = self._minutes_on({t.title.strip().lower() for t in members}, ids, p.start, min(p.end, planner.today()))
            nxt = ", ".join(t.title for t in open_[:3])
            line = (f"{e['name']}: {len(finished)} done, {len(open_)} open; "
                    f"{format_duration(minutes) if minutes else 'no time'} on it {planner.label(p)}, "
                    f"{len([i for i in ids if i in done_in])} done then")
            if nxt and str(epic or "").strip():
                line += f". Next: {nxt}"
            lines.append(line + ".")
        return "\n".join(lines)

    def plan_epic(self, epic: str = "", period: Any = None, count: Any = 3, **_: Any) -> str:
        e = self._epic(epic)
        planner, p = self._period(period)
        try:
            n = max(1, min(10, int(count)))
        except (TypeError, ValueError):
            n = 3
        done = self.db.completed_since(planner.today() - timedelta(days=366))
        pinned = {r["item_id"] for r in planner.plans.rows()}
        todo = [t for t in self.members(e) if t.id not in done and not t.someday and t.id not in pinned][:n]
        if not todo:
            return f"Nothing in {e['name']} is left to plan (its open items are planned already, or it's all done)."
        for t in todo:
            planner.pin(t, p)
        return f"Planned {e['name']} into {planner.label(p)}: " + "; ".join(t.title for t in todo) + "."

    # --- estimates ---

    def set_estimate(self, task: str = "", estimate: Any = None, **_: Any) -> str:
        from thirties_core.efforts import describe, parse_estimate
        t = self._task(task)
        facts = self._facts()
        if estimate in (None, "") or str(estimate).strip().lower() in ("none", "forget", "clear"):
            facts.delete("estimate", t.id)
            return f"Forgot the estimate for {t.title}."
        try:
            rec = parse_estimate(self.db, str(estimate))
        except ValueError as err:
            raise _tool_error(str(err))
        rec["title"] = t.title
        facts.set("estimate", t.id, rec)
        return f"Noted: {t.title} is about {describe(self.db, rec)} of work (your estimate)."

    def set_effort_unit(self, unit: str = "", length: Any = None, **_: Any) -> str:
        from thirties_core.efforts import set_unit
        try:
            return set_unit(self.db, unit, length)
        except ValueError as err:
            raise _tool_error(str(err))

    # --- done by someone else at home ---

    def done_by_someone(self, event: Any = "", who: str = "", **_: Any) -> str:
        who = str(who or "").strip() or "someone else"
        out = self.a.log_outcome(event, "done", estimated=True, note=f"done by {who}")
        return f"{out} Done by {who}: finished, not rolled over, and not counted as the user's time."

    # --- a new list ---

    def create_list(self, title: str = "", notebook: str = "", role: str = "list", items: Any = None,
                    **_: Any) -> str:
        from thirties_core.lists import ROLES, ListCatalog
        from thirties_core.backlog_tickets import reads_like_ticket
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        role = str(role or "list").strip().lower()
        if not title:
            raise _tool_error("Say the new list's name.")
        if role not in ROLES or role in ("routine", "today", "ignore"):
            raise _tool_error("role must be 'list', 'project', 'menu', 'backlog', 'dropbox' or 'reference' (a "
                              "routine or today's list is set up in Profile → Lists).")
        said = self.a.pt.user_message
        if said is not None and not AGREED_RE.search(said) and not re.search(re.escape(title.lower()), said.lower()):
            raise _tool_error("Make a new list only when the user asks for one or agrees: suggest it first, in one "
                              "short question.")
        entries = [x for x in _as_list(items) if not reads_like_ticket(x)]
        writer = self.a._writer()
        if writer is None:
            raise _tool_error("Thirties isn't connected to Joplin, so it can't make lists yet. Connect it in "
                              "Preferences → Tasks.")
        from thirties_core.note_ops import create_list_op
        op = create_list_op(title, str(notebook or ""), entries)
        status, msg = writer.submit(op)
        if status in ("not_allowed", "failed"):
            raise _tool_error(msg)
        if status == "queued":
            return f"{title} will be made in {notebook} as soon as Joplin is open."
        note_id = msg[5:] if msg.startswith("note:") else ""
        if note_id and self.db is not None:
            ListCatalog(self.db).set_role(note_id, role, title, source="user")
        return f"Made the list {title} in {notebook}" + (f", with {', '.join(entries)}" if entries else "") + "."

    # --- list settings ---

    def set_list_settings(self, one_off_notes: Any = None, done_note: Any = None, **_: Any) -> str:
        from thirties_core import list_settings as ls
        if self.db is None:
            raise _tool_error("Settings can't be kept here.")
        said = []
        if one_off_notes not in (None, ""):
            try:
                ls.set_one_off_mode(self.db, str(one_off_notes))
            except ValueError as err:
                raise _tool_error(str(err))
            said.append(f"one-off notes: {ls.ONE_OFF_CHOICES[ls.one_off_mode(self.db)]}")
        if done_note not in (None, ""):
            if str(done_note).strip().lower() in ("monthly", "default", "none", "thirties"):
                ls.set_done_note(self.db)
                said.append("done items go to a Done note for each month")
            else:
                note = self._note(str(done_note))
                from thirties_core.lists import ListCatalog
                had = ListCatalog(self.db).roles().get(note["id"]) or {}
                if had.get("role") not in ("ignore", "reference", None):
                    raise _tool_error(f"{note['title']} is one of the lists ({had.get('role')}); done items go in a "
                                      "note of their own. Pick another note.")
                ls.set_done_note(self.db, note["id"], note["title"])
                said.append(f"done items go to {note['title']}")
        if not said:
            raise _tool_error("Say what to change: one_off_notes or done_note.")
        return "Set: " + "; ".join(said) + "."

    def handlers(self) -> Dict[str, Any]:
        return {spec["name"]: getattr(self, spec["name"]) for spec in PROJECT_TOOL_SPECS}


__all__ = ["PROJECT_READ_ONLY", "PROJECT_TOOL_SPECS", "ProjectTools"]
