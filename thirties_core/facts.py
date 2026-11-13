"""Facts about planned items that Python enforces when it redraws the day.

The model records what the user says ("nothing outside until after the shower",
"not before she leaves at 3", "the walk needs daylight", "lunch stays at 12:30");
the redraw (PlanTools.redraw) keeps every one of them. Facts belong to a planned
item (a span) and move with it.
"""

from __future__ import annotations

import json
import re
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
_NEWER = (("before", "TEXT NOT NULL DEFAULT '[]'"), ("done_by", "TEXT NOT NULL DEFAULT ''"))  # added in 0.3


# Things set by someone else's clock: they keep the time they were given and never slide. The obvious ones are
# recognised here; the model says fixed=true/false for the rest, and the item keeps the answer (as "stays put").
FIXED_RE = re.compile(
    r"\b(appointments?|appts?|doctor|dr\.?|dentist|therap(y|ist)|vet|clinic|hospital|surgery|"
    r"flights?|fly(ing)?|airport|boarding|uber|lyft|taxi|cab|ride|shuttle|train|bus|ferry|"
    r"concerts?|shows?|gig|recital|play|theat(er|re)|movie|cinema|game|match|ceremony|wedding|funeral|"
    r"meetings?|interview|class|lecture|lesson|exam|reservation|rsvp|"
    r"pick ?up|drop ?off|depart(ure)?|leave for|check[- ]?(in|out)|haircut|massage|"
    r"(call|meet|lunch|dinner|coffee|drinks|date) with)\b", re.IGNORECASE)


def looks_fixed(title: str) -> bool:
    """Worded like something set by someone else's clock (an appointment, a flight, a ride, a show, a meeting).
    Only a suggestion: on its own it never makes an item fixed (gave_time says when it does)."""
    return bool(FIXED_RE.search(title or ""))


_SAID_TIME_RE = re.compile(r"(?:\b(?:at|@|by|from)\s*)(\d{1,2})(?::(\d{2}))?\b|\b(\d{1,2})(?::(\d{2}))?\s*"
                           r"(?:am|pm|a\.m\.|p\.m\.|o'?clock)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE)


def gave_time(message, start) -> bool:
    """The user's message gives this start time ("at 3", "3:15", "3pm"): a time from someone else's clock that
    the user passed on, which is what makes an appointment-like item fixed."""
    for m in _SAID_TIME_RE.finditer(message or ""):
        h = m.group(1) or m.group(3) or m.group(5)
        mm = m.group(2) or m.group(4) or m.group(6) or "0"
        if h and int(h) % 12 == start.hour % 12 and int(mm) == start.minute:
            return True
    return False


@dataclass
class ItemFacts:
    after: List[str] = field(default_factory=list)  # span ids it must come after
    not_before: Optional[datetime] = None
    daylight: bool = False                           # must happen between sunrise and sunset
    anchored: bool = False                           # stays at its time when the day is redrawn
    goes_with: List[str] = field(default_factory=list)  # span ids it goes right after (item_facts; never stored)
    before: List[str] = field(default_factory=list)  # span ids it must be done before
    done_by: Optional[datetime] = None               # it must end by then

    def describe(self, titles: Dict[str, str]) -> List[str]:
        out = []
        if self.after:
            out.append("after " + " and ".join(titles.get(a, "something else") for a in self.after))
        if self.before:
            out.append("before " + " and ".join(titles.get(a, "something else") for a in self.before))
        if self.not_before:
            from thirties_core.clock import format_clock
            out.append(f"not before {format_clock(self.not_before)}")
        if self.done_by:
            from thirties_core.clock import format_clock
            out.append(f"done by {format_clock(self.done_by)}")
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
            have = {r[1] for r in conn.execute("PRAGMA table_info(span_facts)").fetchall()}
            for name, kind in _NEWER:
                if name not in have:
                    conn.execute(f"ALTER TABLE span_facts ADD COLUMN {name} {kind}")

    def get(self, span_id: str) -> ItemFacts:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM span_facts WHERE span_id = ?", (span_id,)).fetchone()
        if not row:
            return ItemFacts()
        return ItemFacts(after=json.loads(row["after"]),
                         not_before=datetime.fromisoformat(row["not_before"]) if row["not_before"] else None,
                         daylight=bool(row["daylight"]), anchored=bool(row["anchored"]),
                         before=json.loads(row["before"] or "[]"),
                         done_by=datetime.fromisoformat(row["done_by"]) if row["done_by"] else None)

    def set(self, span_id: str, facts: ItemFacts) -> None:
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO span_facts (span_id, after, not_before, daylight, anchored, updated_at, before,
                                               done_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (span_id, json.dumps(facts.after), facts.not_before.isoformat() if facts.not_before else "",
                  int(facts.daylight), int(facts.anchored), utc_now_iso(), json.dumps(facts.before),
                  facts.done_by.isoformat() if facts.done_by else ""))

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM span_facts").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM span_facts WHERE span_id = ?", (record["span_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO span_facts (span_id, after, not_before, daylight, anchored, updated_at,"
                         " before, done_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (record["span_id"], record.get("after", "[]"), record.get("not_before", ""),
                          record.get("daylight", 0), record.get("anchored", 0), record["updated_at"],
                          record.get("before", "[]"), record.get("done_by", "")))
        return True


__all__ = ["ItemFacts", "SpanFacts"]
