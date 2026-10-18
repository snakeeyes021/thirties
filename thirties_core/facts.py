"""Facts about planned items that Python enforces when it redraws the day.

The model records what the user says ("nothing outside until after the shower",
"not before she leaves at 3", "the walk needs daylight", "lunch stays at 12:30");
the redraw (PlanTools.redraw) keeps every one of them. Facts belong to a planned
item (a span) and move with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from thirties_core.scheduler import StateDatabase, utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS span_facts (
    span_id TEXT PRIMARY KEY,
    after TEXT NOT NULL DEFAULT '[]',
    not_before TEXT NOT NULL DEFAULT '',
    daylight INTEGER NOT NULL DEFAULT 0,
    anchored INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class ItemFacts:
    after: List[str] = field(default_factory=list)  # span ids it must come after
    not_before: Optional[datetime] = None
    daylight: bool = False                           # must happen between sunrise and sunset
    anchored: bool = False                           # stays at its time when the day is redrawn

    def describe(self, titles: Dict[str, str]) -> List[str]:
        out = []
        if self.after:
            out.append("after " + " and ".join(titles.get(a, "something else") for a in self.after))
        if self.not_before:
            from thirties_core.clock import format_clock
            out.append(f"not before {format_clock(self.not_before)}")
        if self.daylight:
            out.append("in daylight")
        if self.anchored:
            out.append("stays put")
        return out


class SpanFacts:
    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def get(self, span_id: str) -> ItemFacts:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM span_facts WHERE span_id = ?", (span_id,)).fetchone()
        if not row:
            return ItemFacts()
        return ItemFacts(after=json.loads(row["after"]),
                         not_before=datetime.fromisoformat(row["not_before"]) if row["not_before"] else None,
                         daylight=bool(row["daylight"]), anchored=bool(row["anchored"]))

    def set(self, span_id: str, facts: ItemFacts) -> None:
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO span_facts (span_id, after, not_before, daylight, anchored, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (span_id, json.dumps(facts.after), facts.not_before.isoformat() if facts.not_before else "",
                  int(facts.daylight), int(facts.anchored), utc_now_iso()))

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM span_facts").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM span_facts WHERE span_id = ?", (record["span_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO span_facts (span_id, after, not_before, daylight, anchored, updated_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (record["span_id"], record.get("after", "[]"),
                                                        record.get("not_before", ""), record.get("daylight", 0),
                                                        record.get("anchored", 0), record["updated_at"]))
        return True


__all__ = ["ItemFacts", "SpanFacts"]
