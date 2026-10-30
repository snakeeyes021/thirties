"""Contextual messages: "have you left yet?", "are you on your way?", only for items that matter.

Most items need no question at all (C1: "checking in to see if I'm doing it is insane"). The few that do are the
ones where being late costs something set by someone else's clock. The default rule (After 0.1, item 4; the user
may refine it, and every threshold is a named constant here):

- **A fixed-time item with travel before it**: a travel item ("Drive to dentist", "Uber to the airport") that ends
  at most ``TRAVEL_GAP`` before a fixed item starts. Once the travel's time has begun (plus ``AFTER_START``) and
  nothing is said about it: "Are you on your way to X?".
- **A fixed-time item with a "leave by" time** in its title or notes ("Dentist (leave by 2:40)"), or a "Leave for
  X" item just before it: at the leave time (plus ``AFTER_START``): "Have you left for X?".
- **Anything the user flags** ("ask me if I've started the taxes"; the check_on tool): once its time has begun
  (plus ``AFTER_START_FLAGGED``): "Have you started X?" (or "Are you on your way?" for travel).

At most one such message per item, ever (kept by item id). They respect message levels: none at "off"; at "light"
and up they're sent, since they're the messages that matter most. Nothing while asleep. Python picks the moment and
the item; there's no model in the loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from thirties_core.clock import format_clock
from thirties_core.facts import looks_fixed
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus
from thirties_core.scheduler import StateDatabase, utc_now_iso

TRAVEL_GAP = timedelta(minutes=15)          # travel ending this close before a fixed item is travel *to* it
AFTER_START = timedelta(minutes=5)          # ask this long after the travel (or leave time) begins
AFTER_START_FLAGGED = timedelta(minutes=10)
LEVELS_ON = ("light", "medium", "heavy")    # message levels at which these are sent

TRAVEL_RE = re.compile(r"\b(travel|drive|driving|commute|walk(ing)? (to|over)|bike (to|over)|cycle to|"
                       r"head(ing)? (out|over|to)|leave|leaving|depart|uber|lyft|taxi|cab|ride to|bus to|train to|"
                       r"get(ting)? to|on the way|en route|transit)\b", re.IGNORECASE)
LEAVE_BY_RE = re.compile(r"\bleave (?:by|at)\s+(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)", re.IGNORECASE)
LEAVE_ITEM_RE = re.compile(r"^\s*(leave|head out|depart)\b", re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contextual_flags (
    span_id TEXT PRIMARY KEY,
    flagged INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


class ContextualFlags:
    """Items the user asked to be checked on ("ask me if I've left for the vet"). Synced like other item facts."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def set(self, span_id: str, flagged: bool = True) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO contextual_flags (span_id, flagged, updated_at) VALUES (?, ?, ?)",
                         (span_id, int(flagged), utc_now_iso()))

    def flagged(self) -> set:
        with self.db._get_connection() as conn:
            return {r["span_id"] for r in conn.execute("SELECT span_id FROM contextual_flags WHERE flagged = 1")}

    # sync: last write wins
    def all_records(self) -> List[Dict]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM contextual_flags").fetchall()]

    def merge_record(self, record: Dict) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM contextual_flags WHERE span_id = ?",
                               (record["span_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO contextual_flags (span_id, flagged, updated_at) VALUES (?, ?, ?)",
                         (record["span_id"], int(record.get("flagged", 1)), record["updated_at"]))
        return True


@dataclass
class Moment:
    """One contextual message: about ``key`` (an item id, for "at most once"), from ``at`` until ``until``."""
    key: str
    at: datetime
    until: datetime
    text: str


def _is_travel(span: EventSpan) -> bool:
    return bool(TRAVEL_RE.search(LEAVE_BY_RE.sub("", span.title)))  # "Vet (leave by 4:40)" is the vet, not travel


def _leave_by(span: EventSpan, plan: DayPlan) -> Optional[datetime]:
    """The leave time written with the item, read as the one in the four hours before it ("leave by 2:40" for a
    3:00 appointment is 2:40 PM, whether or not PM was written)."""
    m = LEAVE_BY_RE.search(f"{span.title} {span.notes or ''}")
    if not m:
        return None
    raw = m.group(1).lower().replace(".", "").replace(" ", "")
    hm = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)?$", raw)
    if not hm:
        return None
    hour, minute, meridiem = int(hm.group(1)), int(hm.group(2) or 0), hm.group(3)
    if hour > 23 or minute > 59:
        return None
    hours = [hour % 12 + (12 if meridiem == "pm" else 0)] if meridiem else ([hour] if hour > 12 else [hour % 12, hour % 12 + 12])
    for day in (span.start_dt.date(), span.start_dt.date() - timedelta(days=1)):
        for h in hours:
            t = span.start_dt.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=minute, second=0,
                                      microsecond=0)
            if span.start_dt - timedelta(hours=4) <= t < span.start_dt:
                return t
    return None


def moments(plan: DayPlan, is_fixed: Callable[[EventSpan], bool], flagged: set) -> List[Moment]:
    """Every contextual message the day's plan calls for, by the default rule above."""
    spans = sorted((s for s in plan.active_spans() if s.category != SpanCategory.SLEEP), key=lambda s: s.start_dt)
    out: List[Moment] = []
    claimed = set()

    def fixed(s: EventSpan) -> bool:
        return is_fixed(s) or looks_fixed(s.title)

    for f in spans:
        if not fixed(f) or f.status != SpanStatus.PLANNED or _is_travel(f) and not f.is_firm:
            continue
        when = format_clock(f.start_dt)
        leave = _leave_by(f, plan)
        if leave is not None:
            out.append(Moment(f.id, leave + AFTER_START, f.start_dt, f"🚪 Have you left for {f.title}? It's at {when}."))
            claimed.add(f.id)
            continue
        before = [t for t in spans if t.id != f.id and t.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
                  and f.start_dt - TRAVEL_GAP <= t.end_dt <= f.start_dt and t.start_dt < f.start_dt
                  and (_is_travel(t) or LEAVE_ITEM_RE.search(t.title))]
        if before:
            t = before[-1]
            if t.status != SpanStatus.PLANNED:
                claimed.add(t.id)
                continue
            text = (f"🚪 Have you left for {f.title}? It's at {when}." if LEAVE_ITEM_RE.search(t.title) else
                    f"🚗 Are you on your way to {f.title}? It's at {when}.")
            out.append(Moment(t.id, t.start_dt + AFTER_START, f.start_dt, text))
            claimed.add(t.id)
    for s in spans:
        if s.id in flagged and s.id not in claimed and s.status == SpanStatus.PLANNED:
            text = (f"🚗 Are you on your way? ({s.title})" if _is_travel(s) else f"👀 Have you started {s.title}?")
            out.append(Moment(s.id, s.start_dt + AFTER_START_FLAGGED, s.end_dt, text))
    return out


class ContextualSent:
    """Which items had their one contextual message (by id, kept a few days)."""
    KEY = "contextual_sent"

    def __init__(self, db: StateDatabase) -> None:
        self.db = db

    def _load(self) -> Dict[str, str]:
        try:
            return json.loads(self.db.get_sync_meta(self.KEY) or "{}")
        except ValueError:
            return {}

    def sent(self, key: str) -> bool:
        return key in self._load()

    def mark(self, key: str, now: datetime) -> None:
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        data = {k: v for k, v in self._load().items() if v >= cutoff}
        data[key] = now.date().isoformat()
        self.db.set_sync_meta(self.KEY, json.dumps(data))


def due(plan: DayPlan, now: datetime, is_fixed: Callable[[EventSpan], bool], db: StateDatabase,
        level: str) -> List[Moment]:
    """The contextual messages to send now (and remember them as sent)."""
    if level not in LEVELS_ON:
        return []
    sent = ContextualSent(db)
    out = [m for m in moments(plan, is_fixed, ContextualFlags(db).flagged())
           if m.at <= now < m.until and not sent.sent(m.key)]
    for m in out:
        sent.mark(m.key, now)
    return out


__all__ = ["ContextualFlags", "ContextualSent", "Moment", "due", "moments", "TRAVEL_GAP", "AFTER_START",
           "AFTER_START_FLAGGED", "LEVELS_ON"]
