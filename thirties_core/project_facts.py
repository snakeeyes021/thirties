"""Thirties' own facts about projects and items that aren't in the notes, synced like the list roles.

- **parts**: a name the user works on that's part of a project ("the tuner rewrite is part of Lighthouse
  Radio"), which isn't a line in the project's note. Keyed by the name; the value names the project's note.
- **epics**: a named group of items across lists, to plan and report by. Keyed by the epic's name; the value
  lists its items (by their ids: exact links, never words) and whole lists (note ids) that belong to it.
- **estimates**: the user's own estimate of the work an item needs, in the unit they choose. Keyed by the item's
  id; the value keeps what they said, its unit and the minutes it comes to.

Nothing here is written into the notes. Last write wins between devices.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from thirties_core.scheduler import utc_now_iso

KINDS = ("part", "epic", "estimate")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_facts (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


class ProjectFacts:

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def all(self, kind: str) -> Dict[str, Dict[str, Any]]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM project_facts WHERE kind = ?", (kind,)).fetchall()
        out = {}
        for r in rows:
            try:
                value = json.loads(r["value"])
            except ValueError:
                continue
            if value:  # an empty value is a deletion (kept so it syncs)
                out[r["key"]] = value
        return out

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        return self.all(kind).get(key)

    def set(self, kind: str, key: str, value: Optional[Dict[str, Any]]) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO project_facts (kind, key, value, updated_at) VALUES (?, ?, ?, ?)",
                         (kind, key, json.dumps(value or {}), utc_now_iso()))

    def delete(self, kind: str, key: str) -> None:
        self.set(kind, key, None)

    # --- sync ---

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM project_facts").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM project_facts WHERE kind = ? AND key = ?",
                               (record["kind"], record["key"])).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO project_facts (kind, key, value, updated_at) VALUES (?, ?, ?, ?)",
                         (record["kind"], record["key"], record.get("value") or "{}", record["updated_at"]))
        return True


__all__ = ["KINDS", "ProjectFacts", "norm"]
