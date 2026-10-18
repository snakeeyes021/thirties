"""What each of the user's lists is for, and how pressing each item on them is.

Lists are organized by area of life (Home, Errands, Medical...), and urgency
varies within one list, so there are two separate axes:

- a **role** per note: how Thirties uses it (a routine feeds each morning, the
  Today note is the day's list, a menu is offered when there's free time, a
  reference list is never pushed...);
- an **urgency** per item: now, soon, whenever or someday, or a date.

A model guesses both (``brains/list_sorter.py``); the user confirms or corrects
them (Profile → Your lists, or in conversation). Guesses never overwrite what the
user chose. Nothing here is written into the notes themselves: the notes stay
plain checklists, and this is Thirties' own knowledge about them, synced between
devices like the profile.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from thirties_core.models import TaskItem
from thirties_core.scheduler import StateDatabase, utc_now_iso

ROLES = {
    "routine": "a routine: its items are done every day, in order",
    "today": "the day's own to-do list",
    "menu": "a menu of things to pick from when there's free time",
    "list": "a list of things to get done",
    "project": "a one-off project with an end",
    "reference": "reference or wishlist: never pushed, only mentioned when asked",
    "ignore": "not a to-do list: Thirties doesn't read it",
}
URGENCIES = {
    "now": "should happen today or tomorrow",
    "soon": "within the next week or two",
    "whenever": "no particular hurry",
    "someday": "maybe, someday: never pushed",
}
# Roles whose items get an urgency each; the others' items take it from the role.
SORTED_ROLES = ("list", "project", "menu")
ROLE_URGENCY = {"today": "now", "routine": "now", "reference": "someday"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS list_roles (
    note_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'guess',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_urgency (
    item_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL,
    due TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'guess',
    updated_at TEXT NOT NULL
);
"""


class ListCatalog:
    """Roles of notes and urgencies of items, each a guess or the user's choice."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    # --- roles ---

    def roles(self) -> Dict[str, Dict[str, str]]:
        with self.db._get_connection() as conn:
            return {r["note_id"]: dict(r) for r in conn.execute("SELECT * FROM list_roles").fetchall()}

    def role(self, note_id: str) -> Optional[str]:
        row = self.roles().get(note_id)
        return row["role"] if row else None

    def set_role(self, note_id: str, role: str, title: str = "", source: str = "user") -> bool:
        """Record a role. A guess never replaces the user's choice; returns whether anything changed."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        current = self.roles().get(note_id)
        if source == "guess" and current and current["source"] == "user":
            return False
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO list_roles (note_id, title, role, source, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET title = excluded.title, role = excluded.role,
                source = excluded.source, updated_at = excluded.updated_at
            """, (note_id, title, role, source, utc_now_iso()))
        return True

    def clear_role(self, note_id: str) -> None:
        """Forget a note's role, so it's guessed again."""
        with self.db._get_connection() as conn:
            conn.execute("DELETE FROM list_roles WHERE note_id = ?", (note_id,))

    # --- urgency ---

    def urgencies(self) -> Dict[str, Dict[str, str]]:
        with self.db._get_connection() as conn:
            return {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM item_urgency").fetchall()}

    def set_urgency(self, item_id: str, urgency: str, due: Optional[date] = None, note_id: str = "", title: str = "",
                    source: str = "user") -> bool:
        if urgency not in URGENCIES:
            raise ValueError(f"unknown urgency {urgency!r}")
        current = self.urgencies().get(item_id)
        if source == "guess" and current and current["source"] == "user":
            return False
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO item_urgency (item_id, note_id, title, urgency, due, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET note_id = excluded.note_id, title = excluded.title,
                urgency = excluded.urgency, due = excluded.due, source = excluded.source,
                updated_at = excluded.updated_at
            """, (item_id, note_id, title, urgency, due.isoformat() if due else "", source, utc_now_iso()))
        return True

    # --- what still needs a guess ---

    def notes_without_role(self, notes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        known = self.roles()
        return [n for n in notes if n["id"] not in known]

    def items_without_urgency(self, tasks: Iterable[TaskItem]) -> Dict[str, List[TaskItem]]:
        """Checklist items in sorted lists (list, project, menu) with no urgency yet, by note."""
        roles, known = self.roles(), self.urgencies()
        out: Dict[str, List[TaskItem]] = {}
        for t in tasks:
            note = t.parent_note_id if t.is_checkbox_item else t.id
            role = roles.get(note or "", {}).get("role")
            if role in SORTED_ROLES and t.id not in known and not t.id.startswith("media:"):
                out.setdefault(note, []).append(t)
        return out

    # --- applying it to the backlog ---

    def apply(self, tasks: List[TaskItem]) -> List[TaskItem]:
        """Set each task's list role and urgency; drop the items of ignored notes. Someday items are marked
        someday, so every brain already leaves them alone."""
        roles, urgencies = self.roles(), self.urgencies()
        kept = []
        for t in tasks:
            note = t.parent_note_id if t.is_checkbox_item else t.id
            role = roles.get(note or "", {}).get("role", "")
            if role == "ignore":
                continue
            t.list_role = role
            u = urgencies.get(t.id)
            t.urgency = u["urgency"] if u else ROLE_URGENCY.get(role, "")
            t.due = u["due"] if u else ""
            if t.urgency == "someday":
                t.someday = True
            kept.append(t)
        return kept

    # --- sync (last write wins, like the profile) ---

    def all_records(self) -> List[Dict[str, Any]]:
        return ([dict(r, kind="role") for r in self.roles().values()]
                + [dict(r, kind="urgency") for r in self.urgencies().values()])

    def merge_record(self, record: Dict[str, Any]) -> bool:
        table, key = ("list_roles", "note_id") if record.get("kind") == "role" else ("item_urgency", "item_id")
        cols = (["note_id", "title", "role", "source", "updated_at"] if table == "list_roles"
                else ["item_id", "note_id", "title", "urgency", "due", "source", "updated_at"])
        with self.db._get_connection() as conn:
            row = conn.execute(f"SELECT updated_at FROM {table} WHERE {key} = ?", (record[key],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [record.get(c, "") for c in cols])
        return True


def sort_lists(catalog: ListCatalog, notes: List[Dict[str, Any]], tasks: List[TaskItem], sorter, today: date,
               max_notes: Optional[int] = None, with_items: bool = True) -> Dict[str, int]:
    """Guess what's missing: the role of each new note, then (with ``with_items``) the urgency of each new item
    in sorted lists, a whole note at a time. A note the model couldn't answer for is tried again next time."""
    counts = {"roles": 0, "items": 0}
    for note in catalog.notes_without_role(notes)[:max_notes]:
        role = sorter.role(note)
        if role is not None:
            catalog.set_role(note["id"], role, note.get("title", ""), source="guess")
            counts["roles"] += 1
    if not with_items:
        return counts
    by_id, roles = {n["id"]: n for n in notes}, catalog.roles()
    for note_id, items in list(catalog.items_without_urgency(tasks).items())[:max_notes]:
        if note_id not in by_id:
            continue
        guesses = sorter.urgencies(by_id[note_id], roles[note_id]["role"], items, today)
        if not guesses:
            continue
        for t in items:
            urgency, due = guesses.get(t.id, ("whenever", None))  # one it skipped: don't keep asking
            catalog.set_urgency(t.id, urgency, due, note_id=note_id, title=t.title, source="guess")
            counts["items"] += 1
    return counts


def urgency_label(task: TaskItem) -> str:
    """How a task's urgency reads to a model or the user: 'soon', 'by Oct 30', or ''."""
    if task.due:
        try:
            return f"by {date.fromisoformat(task.due).strftime('%b %-d')}"
        except ValueError:
            pass
    return task.urgency or ""


__all__ = ["ListCatalog", "ROLES", "URGENCIES", "SORTED_ROLES", "ROLE_URGENCY", "sort_lists", "urgency_label"]
