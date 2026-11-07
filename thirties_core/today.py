"""The day's list: the Today note in the user's task backend, plus their routines.

The Today note is the user's own list of what they mean to do today; they may edit
it by hand like any other note. Thirties writes to it only with permission
(write-back), and keeps one thing of its own about it: which Today item came from
which item on a home list ("Call the bank" from Errands), so ticking either one
ticks both. A routine note is a template: when a routine is on the day's plan it
goes on the Today note as one item, named after the routine, with its steps
nested under it. Nothing is written back into the routine note, and the group
leaves the Today note the next morning (routines never carry over).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX
from thirties_core.models import TaskItem
from thirties_core.scheduler import StateDatabase, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS today_links (
    today_title TEXT PRIMARY KEY,
    home_id TEXT NOT NULL,
    home_note_id TEXT NOT NULL DEFAULT '',
    home_title TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


def note_items(note: Optional[Dict[str, Any]]) -> List[Tuple[str, bool]]:
    """A note's checklist items, top level and nested, as (text, ticked), in order."""
    out = []
    for line in ((note or {}).get("body") or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        d = None if m else DONE_CHECKBOX_REGEX.match(line)
        if m or d:
            out.append(((m or d).group(2).strip(), bool(d)))
    return out


def with_done(note: Optional[Dict[str, Any]], done_ids: set) -> List[Tuple[str, bool]]:
    """note_items, with an open line counted as done when Thirties recorded it done: the record wins over a
    tick that didn't reach Joplin yet (or never will), so a done item isn't offered again (Zein's text, 10-24)."""
    from thirties_core.joplin_engine import item_id
    out, seen = [], {}
    for text, ticked in note_items(note):
        if not ticked:
            n = seen.get(text, 0)
            seen[text] = n + 1
            ticked = item_id((note or {}).get("id", ""), text, n) in done_ids
        out.append((text, ticked))
    return out


def group_items(note: Optional[Dict[str, Any]], name: str) -> Optional[List[str]]:
    """The items nested under the item named ``name`` (open or ticked), or None if there's no such item."""
    lines = ((note or {}).get("body") or "").splitlines()
    want = name.strip().lower()
    for i, line in enumerate(lines):
        m = CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)
        if not m or m.group(2).strip().lower() != want:
            continue
        width = len(m.group(1).replace("\t", "    "))
        children = []
        for child in lines[i + 1:]:
            c = CHECKBOX_REGEX.match(child) or DONE_CHECKBOX_REGEX.match(child)
            if c and len(c.group(1).replace("\t", "    ")) > width:
                children.append(c.group(2).strip())
            elif c or child.strip():
                break
        return children
    return None


def notes_with_role(roles: Dict[str, Dict[str, str]], notes: List[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    return [n for n in notes if (roles.get(n["id"]) or {}).get("role") == role]


class TodayLinks:
    """Which Today item came from which home item (keyed by the Today item's text)."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def link(self, today_title: str, home: TaskItem) -> None:
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO today_links (today_title, home_id, home_note_id, home_title, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(today_title) DO UPDATE SET home_id = excluded.home_id, home_note_id = excluded.home_note_id,
                home_title = excluded.home_title, updated_at = excluded.updated_at
            """, (today_title.strip().lower(), home.id, home.parent_note_id or home.id, home.title, utc_now_iso()))

    def unlink(self, today_title: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("DELETE FROM today_links WHERE today_title = ?", (today_title.strip().lower(),))

    def home_of(self, today_title: str) -> Optional[Dict[str, str]]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM today_links WHERE today_title = ?",
                               (today_title.strip().lower(),)).fetchone()
        return dict(row) if row else None

    def today_title_of(self, home_id: str) -> Optional[str]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT today_title FROM today_links WHERE home_id = ?", (home_id,)).fetchone()
        return row["today_title"] if row else None

    def all(self) -> List[Dict[str, str]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM today_links").fetchall()]

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        return self.all()

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM today_links WHERE today_title = ?",
                               (record["today_title"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO today_links (today_title, home_id, home_note_id, home_title, updated_at)"
                         " VALUES (?, ?, ?, ?, ?)", (record["today_title"], record["home_id"],
                                                     record.get("home_note_id", ""), record.get("home_title", ""),
                                                     record["updated_at"]))
        return True


__all__ = ["TodayLinks", "note_items", "notes_with_role"]
