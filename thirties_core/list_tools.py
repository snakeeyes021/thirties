"""Tools for the user's lists (0.2 block 2): structure edits, the drop box, done items, triage, pencilled-in items.

They sit beside AssistantTools and share its contract (natural-language arguments,
short plain results, ToolError when nothing changed). The logic lives in
placement_lists.py, note_edits.py / note_ops.py, done_log.py and day_lists.py;
this is the thin layer the brains call. Every Joplin change goes through
JoplinWriter, so it's exact (an item's own wording and copy, in a known note) and
logged with the message that caused it.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from thirties_core.assistant_tools import ToolError, _checkbox_id, _stem, _words
from thirties_core.models import SpanStatus, TaskItem
from thirties_core.scheduler import utc_now_iso

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools

LIST_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "edit_list_item",
        "description": "Change an item on one of the user's lists, only when they ask: reword it, split one line into "
                       "several, add sub-items under it, or move it to another section or list. One change per call.",
        "parameters": {"item": "The item (task_id, or its exact wording).",
                       "list": "The note it's in, if the same wording is in more than one.",
                       "rename_to": "New wording.",
                       "split_into": "The lines it becomes, as a list.",
                       "add_under": "Sub-items to put under it, as a list.",
                       "to_section": "A heading to move it under (in to_list, or in its own note); 'top' for above "
                                     "the first heading.",
                       "to_list": "Another note to move it to.",
                       "reason": "Why it goes there, in a few words (for a move)."},
        "required": ["item"],
    },
    {
        "name": "set_list_role",
        "description": "Record what one of the user's notes is for, when they say so (\"Assorted Quickies is my drop "
                       "box\"). A drop box is a quick place for things that don't have an obvious list yet.",
        "parameters": {"list": "The note.",
                       "role": "list, project, menu, backlog, dropbox, routine, today, reference or ignore.",
                       "scope": "For a drop box: 'notebook' (for its own notebook's lists) or 'all'."},
        "required": ["list", "role"],
    },
    {
        "name": "dropbox_review",
        "description": "What's waiting in the drop box, each item with the lists it likely belongs on. Read-only.",
        "parameters": {},
        "required": [],
    },
    {
        "name": "reopen_item",
        "description": "Undo a done: the user says something marked done isn't. It goes back on its list (out of the "
                       "month's Done note) and back to planned.",
        "parameters": {"item": "The item, as worded on its list (or its plan id)."},
        "required": ["item"],
    },
    {
        "name": "triage_list",
        "description": "For a list sorted by time instead of by area: its items, and the lists already beside it, "
                       "for proposing a split by area. Read-only.",
        "parameters": {"list": "The note."},
        "required": ["list"],
    },
    {
        "name": "apply_triage_group",
        "description": "Move one agreed group of items out of a list sorted by time into a list for their area "
                       "(an existing one, or a new note beside it). Each item keeps its urgency.",
        "parameters": {"list": "The note being split.", "items": "The items, as task_ids or exact wording.",
                       "to_list": "An existing note to move them to.", "new_list": "Or: a new note's title."},
        "required": ["list", "items"],
    },
    {
        "name": "pencil_in",
        "description": "Put things on a day's list with no time (\"pencil it in\", \"maybe today\"); if one is on "
                       "the plan, it comes off the plan but stays on the list. Give it a time later with add_event.",
        "parameters": {"items": "The things, as a list (titles, task_ids or plan ids)."},
        "required": ["items"],
    },
    {
        "name": "pencilled_in",
        "description": "What's on the day's list with no time yet. Read-only.",
        "parameters": {},
        "required": [],
    },
]
LIST_READ_ONLY = {"dropbox_review", "triage_list", "pencilled_in"}


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                return [str(x).strip() for x in json.loads(text) if str(x).strip()]
            except ValueError:
                pass
        return [x.strip() for x in re.split(r"\s*(?:;|\n|\|)\s*", text) if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def rekey(db, plans, tasks: List[TaskItem], old: TaskItem, new_id: str, new_title: str, new_note_id: str,
          new_note_title: str) -> None:
    """An item's id is its note, wording and copy, so a reworded or moved item has a new one: what Thirties keeps
    about it (plan items, Today links, urgency, history) follows it."""
    if old.id == new_id:
        return
    if db is not None:
        with db._get_connection() as conn:
            _rekey_rows(conn, old, new_id, new_title, new_note_id)
    for plan in plans:
        for span in plan.spans:
            if span.task_id == old.id:
                span.task_id = new_id
    old.id, old.title, old.parent_note_id, old.parent_note_title = new_id, new_title, new_note_id, new_note_title


def _rekey_rows(conn, old: TaskItem, new_id: str, new_title: str, new_note_id: str) -> None:
    for row in conn.execute("SELECT id, data FROM spans WHERE data LIKE ?", (f'%"{old.id}"%',)).fetchall():
        data = json.loads(row["data"])
        if data.get("task_id") == old.id:
            data["task_id"] = new_id
            conn.execute("UPDATE spans SET data = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(data), utc_now_iso(), row["id"]))
    updates = [("UPDATE today_links SET home_id = ?, home_note_id = ?, home_title = ? WHERE home_id = ?",
                (new_id, new_note_id, new_title, old.id)),
               ("UPDATE OR REPLACE item_urgency SET item_id = ?, note_id = ?, title = ? WHERE item_id = ?",
                (new_id, new_note_id, new_title, old.id)),
               ("UPDATE OR REPLACE task_history SET task_id = ? WHERE task_id = ?", (new_id, old.id))]
    for sql, args in updates:
        try:  # a table another part of Thirties makes lazily may not be there yet
            conn.execute(sql, args)
        except Exception:
            pass


class ListTools:
    def __init__(self, assistant: "AssistantTools") -> None:
        self.a = assistant
        self.pt = assistant.pt

    # --- helpers ---

    def _placement(self):
        from thirties_core.placement_lists import Placement
        return Placement.for_assistant(self.a)

    def _writer(self):
        writer = self.a._writer()
        if writer is None:
            raise ToolError("Thirties isn't connected to Joplin, so it can't change your lists yet. "
                            "Connect it in Preferences → Tasks.")
        return writer

    def _submit(self, op: Dict[str, Any]) -> str:
        status, msg = self._writer().submit(op)
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        return " (Joplin will get it when it's open)" if status == "queued" else ""

    def _plans(self):
        return [self.pt.day_plan] + list(self.pt._other_days.values())

    def _user_asked_about(self, note_title: str) -> bool:
        said = (self.pt.user_message or "").lower()
        return self.pt.user_message is None or "routine" in said or note_title.lower() in said

    def _item(self, ref: str, list_name: str = "") -> TaskItem:
        """The list item this is, exactly: an open item by id or wording (in the named note, if given), else any
        line, open or ticked, worded exactly so (routine notes keep theirs ticked)."""
        found = self.a._exact_task(ref)
        if found is not None and list_name:
            want = {_stem(w) for w in _words(list_name)}
            if not want & {_stem(w) for w in _words(found.parent_note_title or found.title)}:
                found = None
        if found is None:
            found = self.a._note_item(ref, list_name)
        if found is None:
            raise self.a._no_exact_match(ref)
        if not found.is_checkbox_item:
            raise ToolError(f"{found.title} is a whole note, not a line in one.")
        return found

    # --- structure (J7) ---

    def edit_list_item(self, item: str, list: str = "", rename_to: str = "", split_into: Any = None,
                       add_under: Any = None, to_section: str = "", to_list: str = "", reason: str = "",
                       **_: Any) -> str:
        split, under = _as_list(split_into), _as_list(add_under)
        actions = [bool(str(rename_to).strip()), bool(split), bool(under), bool(to_section or to_list)]
        if sum(actions) != 1:
            raise ToolError("Give exactly one change: rename_to, split_into, add_under, or to_section / to_list.")
        found = self._item(str(item), list)
        from thirties_core.lists import ListCatalog
        role = ListCatalog(self.a._db).role(found.parent_note_id) if self.a._db else None
        if role == "routine" and not self._user_asked_about(found.parent_note_title or ""):
            raise ToolError(f"{found.parent_note_title} is a routine; its note changes only when the user asks to "
                            "change the routine itself.")
        note_id, note_title = found.parent_note_id, found.parent_note_title or ""
        occ = found.occurrence
        if str(rename_to).strip():
            new = str(rename_to).strip()
            queued = self._submit({"op": "rename", "note_id": note_id, "title": found.title, "new_title": new,
                                   "occurrence": occ})
            old_title = found.title
            rekey(self.a._db, self._plans(), self.a.s.tasks, found, _checkbox_id(note_id, new), new, note_id, note_title)
            return f"Reworded '{old_title}' to '{new}' in {note_title}{queued}."
        if split:
            if len(split) < 2:
                raise ToolError("Split it into at least two lines.")
            queued = self._submit({"op": "split", "note_id": note_id, "title": found.title, "parts": split,
                                   "occurrence": occ})
            old_title = found.title
            for part in split[1:]:
                self.a.s.tasks.append(TaskItem(id=_checkbox_id(note_id, part), source_notebook=found.source_notebook,
                                               title=part, is_checkbox_item=True, parent_note_title=note_title,
                                               parent_note_id=note_id, section=found.section,
                                               parent_task_id=found.parent_task_id, list_role=found.list_role))
            rekey(self.a._db, self._plans(), self.a.s.tasks, found, _checkbox_id(note_id, split[0]), split[0], note_id,
                  note_title)
            return f"Split '{old_title}' in {note_title} into: " + "; ".join(split) + f"{queued}."
        if under:
            queued = self._submit({"op": "break_down", "note_id": note_id, "parent_title": found.title,
                                   "steps": under, "occurrence": occ, "ticked_too": True})
            for step in under:
                self.a.s.tasks.append(TaskItem(id=_checkbox_id(note_id, step), source_notebook=found.source_notebook,
                                               title=step, is_checkbox_item=True, parent_note_title=note_title,
                                               parent_note_id=note_id, section=found.section,
                                               parent_task_id=found.id, list_role=found.list_role))
            return f"Added under '{found.title}' in {note_title}: " + "; ".join(under) + f"{queued}."
        return self._move(found, to_list, to_section, reason)

    def _move(self, found: TaskItem, to_list: str, to_section: str, reason: str) -> str:
        placement = self._placement()
        source = next((n for n in placement.notes if n["id"] == found.parent_note_id), None)
        target = placement.resolve(to_list) if to_list else source
        if target is None:
            raise ToolError(f"Which list should {found.title} go to?")
        if target["id"] != found.parent_note_id:
            why = placement.refusal(target)
            if why:
                raise ToolError(why + " Pick another list.")
        self._fill_body(target)
        dated = bool(found.due) or found.urgency == "now"
        section, where = placement.choose_section(target, to_section or None, found.title, dated)
        if target["id"] == found.parent_note_id and to_section in (None, ""):
            raise ToolError("Say which section to move it to.")
        queued = self._submit({"op": "move", "note_id": found.parent_note_id, "title": found.title,
                               "occurrence": found.occurrence, "to_note_id": target["id"], "section": section,
                               "ticked_too": True, "reason": str(reason or "")[:200]})
        old_note = found.parent_note_title
        rekey(self.a._db, self._plans(), self.a.s.tasks, found, _checkbox_id(target["id"], found.title), found.title,
              target["id"], target["title"])
        found.section = section or ""
        found.list_role = placement.role(target)
        if target["id"] == (source or {}).get("id"):
            return f"Moved '{found.title}'{where} in {target['title']}{queued}."
        return f"Moved '{found.title}' from {old_note} to {target['title']}{where}{queued}."

    def _fill_body(self, note: Dict[str, Any]) -> None:
        """A note known only from its items: read its body, so its sections are known."""
        if note.get("body") is None:
            try:
                live = self.a.s.joplin.api_client.get_note(note["id"], fields="id,body") if self.a.s.joplin else None
            except Exception:
                live = None
            if live and "body" in live:
                note["body"] = live["body"]

    # --- roles and the drop box ---

    def set_list_role(self, list: str, role: str, scope: str = "", **_: Any) -> str:
        from thirties_core.lists import ROLES, ListCatalog
        from thirties_core.placement_lists import DropBoxScopes
        role = str(role or "").strip().lower().replace(" ", "")
        role = {"drop": "dropbox", "inbox": "dropbox", "grabbag": "dropbox"}.get(role, role)
        if role not in ROLES:
            raise ToolError("role must be one of: " + ", ".join(ROLES) + ".")
        if self.a._db is None:
            raise ToolError("Roles can't be saved here.")
        note = self._placement().resolve(list)
        ListCatalog(self.a._db).set_role(note["id"], role, note["title"], source="user")
        for t in self.a.s.tasks:
            if t.parent_note_id == note["id"]:
                t.list_role = role
        out = f"Noted: {note['title']} is {ROLES[role].split(':')[0]}."
        if role == "dropbox" and str(scope).strip():
            scope = "all" if str(scope).strip().lower() in ("all", "everything", "every notebook") else "notebook"
            DropBoxScopes(self.a._db).set(note["id"], scope)
            out = out[:-1] + (" for everything." if scope == "all" else f" for {note.get('source_notebook', 'its notebook')}.")
        return out

    def dropbox_review(self, **_: Any) -> str:
        return self._placement().dropbox_review()

    def dropbox_offer(self, now) -> str:
        """For the background service: the occasional offer to empty a drop box, or ''."""
        from thirties_core.placement_lists import dropbox_offer
        if self.a._db is None:
            return ""
        return dropbox_offer(self._placement(), self.a._db, now)

    # --- done ---

    def reopen_item(self, item: str, **_: Any) -> str:
        from thirties_core.done_log import DoneLog, clear_completed
        from thirties_core.lists import ListCatalog
        if self.a._db is None:
            raise ToolError("Nothing to undo here.")
        ref = str(item).strip()
        span = None
        try:
            span = self.pt._find_span(ref)
        except ToolError:
            span = None
        title = span.title if span is not None else ref
        task = next((t for t in self.a.s.tasks if span is not None and span.task_id and t.id == span.task_id), None)
        names = [x for x in {title, task.title if task else ""} if x]
        log = DoneLog(self.a._db)
        rec = next((r for r in (log.latest(n) for n in names) if r), None)  # by the list item's own wording
        parts = []
        if rec is not None:
            queued = self._submit({"op": "unfile_done", "key": rec["key"], "title": rec["title"],
                                   "note_id": rec["source_note_id"]})
            new_id = _checkbox_id(rec["source_note_id"], rec["title"])
            clear_completed(self.a._db, new_id)
            role = ListCatalog(self.a._db).role(rec["source_note_id"]) or ""
            if not any(t.id == new_id for t in self.a.s.tasks):
                self.a.s.tasks.append(TaskItem(id=new_id, source_notebook="", title=rec["title"], is_checkbox_item=True,
                                               parent_note_title=rec["source_title"],
                                               parent_note_id=rec["source_note_id"], section=rec["section"] or "",
                                               list_role=role))
            parts.append(f"put '{rec['title']}' back on {rec['source_title']}{queued}")
        else:
            ticked = self._ticked_line(names)
            if ticked is not None:
                note, text = ticked
                queued = self._submit({"op": "untick", "note_id": note["id"], "title": text})
                clear_completed(self.a._db, _checkbox_id(note["id"], text))
                parts.append(f"unticked '{text}' in {note['title']}{queued}")
                today = self.a._today_note()
                if today is not None and today["id"] != note["id"]:
                    on_today = self._ticked_line(names, only=today)
                    if on_today is not None:
                        self._submit({"op": "untick", "note_id": today["id"], "title": on_today[1]})
        if span is not None and span.status != SpanStatus.PLANNED:
            span.status = SpanStatus.PLANNED
            if span.planned_start is not None:
                span.start_dt, span.end_dt = span.planned_start, span.planned_end
            span.actual_start = span.actual_end = None
            self.pt._persist(span)
            if span.task_id:
                clear_completed(self.a._db, span.task_id)
            parts.append(f"{span.title} is planned again")
        if not parts:
            raise ToolError(f"Nothing called '{ref}' is marked done on the plan or in the lists.")
        return "Undone: " + "; ".join(parts) + "."

    def _ticked_line(self, names: List[str], only: Optional[Dict[str, Any]] = None):
        from thirties_core.joplin_engine import DONE_CHECKBOX_REGEX
        want = {n.strip().lower() for n in names}
        for note in [only] if only else self.a._joplin_notes():
            for line in (note.get("body") or "").splitlines():
                m = DONE_CHECKBOX_REGEX.match(line)
                if m and m.group(2).strip().lower() in want:
                    return note, m.group(2).strip()
        return None

    # --- triage ---

    def triage_list(self, list: str, **_: Any) -> str:
        placement = self._placement()
        note = placement.resolve(list)
        self._fill_body(note)
        if self.a._db is not None:
            self.a._db.set_sync_meta(f"triage:{note['id']}", "offered")  # offered once
        return placement.triage_text(note)

    def apply_triage_group(self, list: str, items: Any, to_list: str = "", new_list: str = "", **_: Any) -> str:
        from thirties_core.lists import ListCatalog
        from thirties_core.placement_lists import horizon_urgency
        placement = self._placement()
        source = placement.resolve(list)
        refs = _as_list(items)
        if not refs:
            raise ToolError("Which items?")
        found = []
        for ref in refs:
            t = self.a._exact_task(ref)
            if t is None or t.parent_note_id != source["id"]:
                raise ToolError(f"'{ref}' isn't an item of {source['title']}. Use its task_id from triage_list.")
            found.append(t)
        op: Dict[str, Any] = {"op": "move_many", "note_id": source["id"], "title": source["title"],
                              "items": [{"title": t.title, "occurrence": t.occurrence} for t in found]}
        if to_list:
            target = placement.resolve(to_list)
            why = placement.refusal(target)
            if why or target["id"] == source["id"]:
                raise ToolError((why or "That's the same list.") + " Pick another list, or give new_list.")
            self._fill_body(target)
            op["to_note_id"] = target["id"]
            op["section"], _ = placement.choose_section(target, None, " ".join(t.title for t in found), dated=False)
            target_id, target_title = target["id"], target["title"]
        elif str(new_list).strip():
            op["new_title"] = target_title = str(new_list).strip()
            target_id = None
        else:
            raise ToolError("Give to_list (an existing note) or new_list (a new note's title).")
        status, msg = self._writer().submit(op)
        if status in ("not_allowed", "failed"):
            raise ToolError(msg)
        if target_id is None and msg.startswith("note:"):
            target_id = msg[5:]
        catalog = ListCatalog(self.a._db) if self.a._db else None
        if target_id and catalog is not None:
            if new_list:
                catalog.set_role(target_id, "list", target_title, source="user")
            for t in found:  # the time it was filed under becomes its urgency
                new_id = _checkbox_id(target_id, t.title)
                catalog.set_urgency(new_id, horizon_urgency(t.section), note_id=target_id, title=t.title, source="user")
                rekey(self.a._db, self._plans(), self.a.s.tasks, t, new_id, t.title, target_id, target_title)
                t.section, t.urgency = "", horizon_urgency(t.section)
        later = " (Joplin will get it when it's open)" if status == "queued" else ""
        return f"Moved {len(found)} items from {source['title']} to {target_title}{later}: " + \
            ", ".join(t.title for t in found) + "."

    # --- pencilled in (O3) ---

    def pencil_in(self, items: Any, **_: Any) -> str:
        from thirties_core.day_lists import add_ahead
        names = _as_list(items)
        if not names:
            raise ToolError("Which things?")
        plan = self.pt.day_plan
        off, keep = [], []
        for name in names:
            span = None
            try:
                span = self.pt._find_span(name)
            except ToolError:
                span = None
            if span is not None and (span.is_envelope or span.source == "calendar"):
                raise ToolError(f"{span.title} can't be pencilled in.")
            if span is not None and span.status == SpanStatus.PLANNED:
                task = next((t for t in self.a.s.tasks if span.task_id and t.id == span.task_id), None)
                keep.append(task.id if task is not None else span.title)
                self.pt._drop(span)
                off.append(span.title)
            else:
                keep.append(name)
        if off:
            self.pt._save_plan_after_change()
        if self.pt.is_current(plan):
            self.a._day_begun(off + keep)
            out = self.a.add_to_today(keep)
            listed = [x for x in re.findall(r"(?:Added to today's list|Already on it): ([^.]+)", out)]
            names_out = ", ".join(listed) or ", ".join(keep)
            said = f"On today's list, no time yet: {names_out}."
        elif plan.target_date > self.pt.today():
            said = add_ahead(self.a, keep)
        else:
            raise ToolError("That day is over.")
        return said + (f" Off the plan: {', '.join(off)}." if off else "")

    def pencilled_in(self, **_: Any) -> str:
        from thirties_core.day_lists import pencilled_in
        items = pencilled_in(self.a)
        if not items:
            return "Nothing on the list without a time."
        return "On the list, no time yet:\n" + "\n".join(f"- {t}" for t in items)

    def handlers(self) -> Dict[str, Any]:
        return {"edit_list_item": self.edit_list_item, "set_list_role": self.set_list_role,
                "dropbox_review": self.dropbox_review, "reopen_item": self.reopen_item,
                "triage_list": self.triage_list, "apply_triage_group": self.apply_triage_group,
                "pencil_in": self.pencil_in, "pencilled_in": self.pencilled_in}


__all__ = ["ListTools", "LIST_TOOL_SPECS", "LIST_READ_ONLY", "rekey"]
