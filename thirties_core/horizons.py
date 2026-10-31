"""Planning across horizons: a day, a week, a month, a year (ROADMAP C2).

The user's words (10-30): "A day is a series of blocks in which things can be
dropped; a week is a series of days in which things can be dropped. A month is a
series of weeks (or days)...; a year is a series of months... long-term planning
[is] knowing what the time horizon for a given item... is and appropriately
pinning it to one of the horizon-level plans and then those plans properly
bubbling down into the lower horizons. A 'Goals' list is... literally just the
Joplin library."

So there is no Goals note. Three things live here:

- **A horizon per item, and per note as a default for its items**: day, week,
  month, year or someday. It generalises urgency (``lists.py``: now, soon,
  whenever, someday, or a date), which keeps working: urgency maps onto a horizon
  and back. Python defaults the obvious cases (a due date sets it; reference and
  someday notes are someday; a user's urgency stands), the chosen model guesses
  the rest (``brains/list_sorter.py``), and the user corrects it in chat
  (``set_horizon``) or in bulk (a whole note at once).
- **Horizon plans**: a week plan is items dropped into a week (optionally onto a
  day of it); a month plan, items dropped into a month; a year plan, into the
  year (optionally into a quarter). Dropping into a week of a month is that
  week's plan, and dropping into a month of a year is that month's plan: one
  place per thing, so plans can't disagree. Stored here, synced.
- **Bubbling down**: the candidates for a period come first from what's pinned
  to it, then from the plan one level up that isn't placed lower yet (and what's
  left over from the last period), then from items whose horizon says they
  belong now. Python lists them with their context (how full each day is, due
  dates, how often put off or carried); the model picks and explains.
  ``candidates_for_day`` and friends are the entry points for the morning
  proposal and the planning sessions.

AGENTS.md §7.3: the model brings judgement (which of these, and why); Python
brings the periods, the arithmetic of dates and the bookkeeping of what's placed.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

from thirties_core.models import TaskItem
from thirties_core.scheduler import StateDatabase, utc_now_iso

if TYPE_CHECKING:
    from thirties_core.plan_tools import PlanTools

logger = logging.getLogger(__name__)

HORIZONS = {
    "day": "today or tomorrow: it can't wait",
    "week": "this week",
    "month": "this month",
    "year": "sometime this year: no particular hurry",
    "someday": "maybe, someday: never pushed",
}
LEVELS = ("day", "week", "month", "year")  # the plannable horizons, finest first
_RANK = {h: i for i, h in enumerate(LEVELS + ("someday",))}
PARENT = {"day": "week", "week": "month", "month": "year"}

# Urgency is the older, coarser axis; each maps onto a horizon and back, so callers that read urgency keep working.
# "whenever" is "no particular hurry": the year, not the month (most items on a list are "whenever"; a month plan
# that pulled in all of them would be the whole backlog). Decision 10-30, change here if it reads wrong.
URGENCY_HORIZON = {"now": "day", "soon": "week", "whenever": "year", "someday": "someday"}
HORIZON_URGENCY = {"day": "now", "week": "soon", "month": "whenever", "year": "whenever", "someday": "someday"}
# A note's role gives the obvious default (lists.ROLE_URGENCY, in horizons).
ROLE_HORIZON = {"today": "day", "routine": "day", "reference": "someday", "backlog": "year"}
# Roles whose items are planned item by item (lists.SORTED_ROLES). Menu items are for free time: offered only when
# pinned or due. Routines (A2), Today, project backlogs (planned as time on the project) and reference never are.
PLANNED_ROLES = ("list", "project", "menu")
CANDIDATE_ROLES = ("list", "project", "")  # "" is a note not sorted yet: its items still count

# How many things a planning session proposes by default, beyond what's already pinned. The model picks its own
# when it plans with the user; this is only Python's starting proposal for the morning offer.
PICKS = {"day": 3, "week": 5, "month": 6, "year": 8}
# A session is offered on the period's first morning; if the morning is missed (or another session took it), it's
# still offered this many days in. One session a morning, the biggest first.
SESSION_GRACE_DAYS = {"week": 2, "month": 4, "year": 10}
PROPOSAL_FRESH_HOURS = 4  # like the day's rollover: a morning offer left unanswered doesn't catch a "yes" at night

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


# --- periods -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    """A day, a week, a month, a quarter (only as a slot in a year plan) or a year: its first and last day."""
    level: str
    start: date
    end: date

    @property
    def key(self) -> str:
        return f"{self.level}:{self.start.isoformat()}"

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def overlaps(self, other: "Period") -> bool:
        return self.start <= other.end and other.start <= self.end

    def days(self) -> List[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def week_start_index(config=None) -> int:
    """0 for Monday ... 6 for Sunday, from the user's setting (general.week_start), Monday by default."""
    name = str(getattr(getattr(config, "general", None), "week_start", "mon") or "mon").strip().lower()[:3]
    return _WEEKDAYS.index(name) if name in _WEEKDAYS else 0


def period_of(level: str, d: date, week_start: int = 0) -> Period:
    """The period at ``level`` that contains the day ``d``."""
    if level == "day":
        return Period("day", d, d)
    if level == "week":
        start = d - timedelta(days=(d.weekday() - week_start) % 7)
        return Period("week", start, start + timedelta(days=6))
    if level == "month":
        start = d.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return Period("month", start, nxt - timedelta(days=1))
    if level == "quarter":
        start = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
        nxt = (start + timedelta(days=95)).replace(day=1)
        return Period("quarter", start, nxt - timedelta(days=1))
    if level == "year":
        return Period("year", date(d.year, 1, 1), date(d.year, 12, 31))
    raise ValueError(f"unknown level {level!r}")


def period_from_key(key: str, week_start: int = 0) -> Period:
    level, _, start = key.partition(":")
    return period_of(level, date.fromisoformat(start), week_start)


def next_period(p: Period, week_start: int = 0) -> Period:
    return period_of(p.level, p.end + timedelta(days=1), week_start)


def previous_period(p: Period, week_start: int = 0) -> Period:
    return period_of(p.level, p.start - timedelta(days=1), week_start)


def parent_period(p: Period, week_start: int = 0) -> Optional[Period]:
    """The period one level up that holds this one (a week across two months belongs to the month it starts in;
    plans one level up are matched by overlap, so the other month's plan still reaches it)."""
    up = PARENT.get(p.level)
    return period_of(up, p.start, week_start) if up else None


def parse_period(text: Any, today: date, week_start: int = 0) -> Period:
    """'this week', 'next week', 'the week of Nov 9', 'Thursday', 'Oct 30', 'this month', 'November',
    'November 2027', 'Q1', 'this year', 'next year', '2027', 'this weekend' → a Period. A bare month or quarter
    is the next one that isn't over. Raises ValueError with a plain message."""
    from thirties_core.clock import ClockError, parse_day
    if isinstance(text, Period):
        return text
    if isinstance(text, date):
        return period_of("day", text, week_start)
    raw = re.sub(r"[,.!?]", " ", str(text or "").strip().lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"^some ?time\s+", "", raw)
    raw = re.sub(r"^(?:in|on|for|during|over|within|by the end of|end of|the end of)\s+", "", raw)
    raw = re.sub(r"^(?:the rest of|rest of)\s+", "", raw)
    if raw in ("", "today", "tonight"):
        return period_of("day", today, week_start)
    m = re.fullmatch(r"(this|the|next|last|coming)? ?(week|month|year)", raw)
    if m:
        p = period_of(m.group(2), today, week_start)
        if m.group(1) in ("next", "coming"):
            return next_period(p, week_start)
        if m.group(1) == "last":
            return previous_period(p, week_start)
        return p
    m = re.fullmatch(r"(?:the )?week (?:of|starting|beginning) (.+)", raw)
    if m:
        try:
            return period_of("week", parse_day(m.group(1), today), week_start)
        except ClockError as e:
            raise ValueError(str(e)) from None
    if re.fullmatch(r"(?:this |the |next )?weekend", raw):
        return period_of("day", parse_day("next saturday" if raw.startswith("next") else "saturday", today),
                         week_start)
    m = re.fullmatch(r"q([1-4])(?: (\d{4}))?", raw)
    if m:
        q = int(m.group(1))
        year = int(m.group(2)) if m.group(2) else today.year
        p = period_of("quarter", date(year, 3 * q - 2, 1), week_start)
        if not m.group(2) and p.end < today:
            p = period_of("quarter", date(year + 1, 3 * q - 2, 1), week_start)
        return p
    m = re.fullmatch(r"(\d{4})", raw)
    if m:
        return period_of("year", date(int(m.group(1)), 1, 1), week_start)
    m = re.fullmatch(r"(?:the month of )?([a-z]+)(?: (\d{4}))?", raw)
    month = None
    if m and len(m.group(1)) >= 3 and m.group(1) not in ("mon", "monday"):
        month = next((i + 1 for i, name in enumerate(_MONTHS) if name.startswith(m.group(1))), None)
    if month:
        year = int(m.group(2)) if m.group(2) else today.year
        if not m.group(2) and month < today.month:
            year += 1
        return period_of("month", date(year, month, 1), week_start)
    try:
        return period_of("day", parse_day(raw, today), week_start)
    except ClockError:
        raise ValueError(f"Could not read the period '{text}'. Say e.g. 'this week', 'Thursday', 'next week', "
                         "'November', 'Q1' or 'next year'.") from None


def period_label(p: Period, today: date, week_start: int = 0) -> str:
    """How a period reads to the user: 'today', 'Thursday', 'this week', 'the week of Nov 9', 'November', '2027'."""
    if p.level == "day":
        if p.start == today:
            return "today"
        if p.start == today + timedelta(days=1):
            return "tomorrow"
        if 0 < (p.start - today).days < 7:
            return p.start.strftime("%A")
        return p.start.strftime("%a %b %-d")
    if p.level == "week":
        here = period_of("week", today, week_start)
        if p == here:
            return "this week"
        if p == next_period(here, week_start):
            return "next week"
        return f"the week of {p.start.strftime('%b %-d')}"
    if p.level == "month":
        name = p.start.strftime("%B")
        if p.start == today.replace(day=1):
            return f"this month ({name})"
        return name if p.start.year == today.year or (p.start - today).days < 300 else f"{name} {p.start.year}"
    if p.level == "quarter":
        return f"Q{(p.start.month - 1) // 3 + 1} {p.start.year}"
    if p.level == "year":
        return "this year" if p.start.year == today.year else str(p.start.year)
    return p.key


def due_horizon(due: date, today: date, week_start: int = 0) -> str:
    """The horizon a due date puts an item at, today: by tomorrow is the day; by the end of this week (or within a
    week) the week; by the end of the month (or within a month) the month; anything later, the year."""
    days = (due - today).days
    if days <= 1:
        return "day"
    if due <= period_of("week", today, week_start).end or days <= 7:
        return "week"
    if due <= period_of("month", today, week_start).end or days <= 31:
        return "month"
    return "year"


def finer(a: str, b: str) -> bool:
    """Horizon ``a`` is at least as near as ``b`` (day ⊂ week ⊂ month ⊂ year ⊂ someday)."""
    return _RANK.get(a, 99) <= _RANK.get(b, 99)


# --- horizons of items and notes -------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS horizon_marks (
    ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    note_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    horizon TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'guess',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS horizon_plan (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note_id TEXT NOT NULL DEFAULT '',
    list_title TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL,
    period TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'in',
    updated_at TEXT NOT NULL
);
"""


class HorizonCatalog:
    """Each item's and each note's horizon, a guess or the user's choice (a guess never replaces the user's).
    Kept in Thirties, never written into the notes; synced like the list roles."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def _marks(self, kind: str) -> Dict[str, Dict[str, str]]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM horizon_marks WHERE kind = ?", (kind,)).fetchall()
        return {r["ref"].split(":", 1)[1]: dict(r) for r in rows}

    def items(self) -> Dict[str, Dict[str, str]]:
        return self._marks("item")

    def notes(self) -> Dict[str, Dict[str, str]]:
        return self._marks("note")

    def _set(self, kind: str, ref: str, horizon: str, note_id: str, title: str, source: str) -> bool:
        if horizon not in HORIZONS:
            raise ValueError(f"unknown horizon {horizon!r}")
        current = self._marks(kind).get(ref)
        if source == "guess" and current and current["source"] == "user":
            return False
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO horizon_marks (ref, kind, note_id, title, horizon, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET note_id = excluded.note_id, title = excluded.title,
                horizon = excluded.horizon, source = excluded.source, updated_at = excluded.updated_at
            """, (f"{kind}:{ref}", kind, note_id, title, horizon, source, utc_now_iso()))
        return True

    def set_item(self, item_id: str, horizon: str, note_id: str = "", title: str = "", source: str = "user") -> bool:
        return self._set("item", item_id, horizon, note_id, title, source)

    def set_note(self, note_id: str, horizon: str, title: str = "", source: str = "user") -> bool:
        return self._set("note", note_id, horizon, note_id, title, source)

    def clear_note(self, note_id: str) -> None:
        """Forget a note's horizon, so it's guessed again."""
        with self.db._get_connection() as conn:
            conn.execute("DELETE FROM horizon_marks WHERE ref = ?", (f"note:{note_id}",))

    def clear_item_guesses(self, note_id: str) -> int:
        """Forget the guessed horizons of a note's items, so the note's own horizon shows through (a bulk change:
        "everything on Home is for this year"). The user's own choices stay."""
        with self.db._get_connection() as conn:
            cur = conn.execute("DELETE FROM horizon_marks WHERE kind = 'item' AND note_id = ? AND source = 'guess'",
                               (note_id,))
            return cur.rowcount

    # --- the horizon an item is at ---

    def effective(self, t: TaskItem, today: date, week_start: int = 0,
                  items: Optional[Dict[str, Dict[str, str]]] = None,
                  notes: Optional[Dict[str, Dict[str, str]]] = None,
                  urgencies: Optional[Dict[str, Dict[str, str]]] = None) -> str:
        """The horizon a task is at today. In order: the user's choice for the item; its due date (Python's
        default, and it tightens as the date nears); the urgency the user gave; a Someday heading; the model's
        guess for the item; a pressing urgency guess; the note's horizon; what the urgency or the role says.
        "whenever" carries no information about the item, so the note's horizon fills in for it."""
        items = self.items() if items is None else items
        notes = self.notes() if notes is None else notes
        urgencies = _urgencies(self.db) if urgencies is None else urgencies
        mark, u = items.get(t.id), urgencies.get(t.id)
        if mark and mark["source"] == "user":
            return mark["horizon"]
        due = _as_date(t.due or (u or {}).get("due"))
        if due:
            return due_horizon(due, today, week_start)
        if u and u["source"] == "user":
            return URGENCY_HORIZON.get(u["urgency"], "year")
        if t.someday:
            return "someday"
        if mark:
            return mark["horizon"]
        urgency = (u or {}).get("urgency") or t.urgency
        if urgency in ("now", "soon", "someday"):
            return URGENCY_HORIZON[urgency]
        note = notes.get(t.parent_note_id if t.is_checkbox_item else t.id or "")
        if note:
            return note["horizon"]
        if urgency in URGENCY_HORIZON:
            return URGENCY_HORIZON[urgency]
        return ROLE_HORIZON.get(t.list_role, "year")

    def apply(self, tasks: List[TaskItem], today: date, week_start: int = 0) -> List[TaskItem]:
        """Set each task's horizon (TaskItem.horizon), for every brain to read."""
        items, notes, urgencies = self.items(), self.notes(), _urgencies(self.db)
        for t in tasks:
            t.horizon = self.effective(t, today, week_start, items, notes, urgencies)
        return tasks

    # --- what still needs a guess ---

    def notes_without_horizon(self, notes: Iterable[Dict[str, Any]], roles: Dict[str, Dict[str, str]]
                              ) -> List[Dict[str, Any]]:
        """Notes whose items get planned (lists, projects, menus) with no horizon yet. Reference notes are someday
        and routines are daily: Python's defaults, no guess."""
        known = self.notes()
        return [n for n in notes if (roles.get(n["id"]) or {}).get("role") in PLANNED_ROLES and n["id"] not in known]

    def items_without_horizon(self, tasks: Iterable[TaskItem], roles: Dict[str, Dict[str, str]]
                              ) -> Dict[str, List[TaskItem]]:
        """Items the model should judge, by note: in planned roles, with no horizon yet, and nothing Python can
        default from (a due date, the user's urgency, a Someday heading, an urgency guess of now or someday)."""
        known, urgencies = self.items(), _urgencies(self.db)
        out: Dict[str, List[TaskItem]] = {}
        for t in tasks:
            note = t.parent_note_id if t.is_checkbox_item else t.id
            if (roles.get(note or "") or {}).get("role") not in PLANNED_ROLES or t.id in known:
                continue
            u = urgencies.get(t.id) or {}
            if t.id.startswith("media:") or t.someday or t.due or u.get("due") or u.get("source") == "user" \
                    or u.get("urgency") in ("now", "someday"):
                continue
            out.setdefault(note, []).append(t)
        return out

    # --- sync (last write wins) ---

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM horizon_marks").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        cols = ["ref", "kind", "note_id", "title", "horizon", "source", "updated_at"]
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM horizon_marks WHERE ref = ?", (record["ref"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO horizon_marks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [record.get(c, "") for c in cols])
        return True


def _urgencies(db: StateDatabase) -> Dict[str, Dict[str, str]]:
    from thirties_core.lists import ListCatalog
    return ListCatalog(db).urgencies()


def _as_date(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def sort_horizons(catalog: HorizonCatalog, notes: List[Dict[str, Any]], tasks: List[TaskItem], sorter, today: date,
                  max_notes: Optional[int] = None) -> Dict[str, int]:
    """Guess what's missing, like lists.sort_lists: each planned note's horizon (one small choice per note), then
    the horizon of each item Python can't default, a note at a time. A sorter without horizon guessing (or a note
    the model couldn't answer for) is simply tried again next time."""
    from thirties_core.lists import ListCatalog
    counts = {"notes": 0, "items": 0}
    if not hasattr(sorter, "note_horizon"):
        return counts
    roles = ListCatalog(catalog.db).roles()
    for note in catalog.notes_without_horizon(notes, roles)[:max_notes]:
        horizon = sorter.note_horizon(note, roles[note["id"]]["role"])
        if horizon in HORIZONS:
            catalog.set_note(note["id"], horizon, note.get("title", ""), source="guess")
            counts["notes"] += 1
    by_id, note_marks = {n["id"]: n for n in notes}, catalog.notes()
    for note_id, items in list(catalog.items_without_horizon(tasks, roles).items())[:max_notes]:
        if note_id not in by_id:
            continue
        default = (note_marks.get(note_id) or {}).get("horizon", "year")
        guesses = sorter.horizons(by_id[note_id], roles[note_id]["role"], items, today, default)
        if not guesses:
            continue
        for t in items:  # one it skipped takes the note's horizon: don't keep asking
            catalog.set_item(t.id, guesses.get(t.id, default), note_id=note_id, title=t.title, source="guess")
            counts["items"] += 1
    return counts


# --- horizon plans ----------------------------------------------------------------------------------------------------

class HorizonPlans:
    """What's dropped into each week, month and year (a day is a slot of a week plan; a quarter, of a year plan).
    One row per item and period; taking an item out keeps the row as 'out', so a removal syncs too."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def rows(self, level: Optional[str] = None, item_id: Optional[str] = None, active: bool = True
             ) -> List[Dict[str, str]]:
        sql, args = "SELECT * FROM horizon_plan WHERE 1 = 1", []
        if level:
            sql, args = sql + " AND level = ?", args + [level]
        if item_id:
            sql, args = sql + " AND item_id = ?", args + [item_id]
        if active:
            sql += " AND state = 'in'"
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY period, slot, updated_at", args).fetchall()]

    def in_period(self, p: Period) -> List[Dict[str, str]]:
        return [r for r in self.rows(p.level) if r["period"] == p.start.isoformat()]

    def put(self, item_id: str, title: str, note_id: str, list_title: str, p: Period, slot: str = "") -> None:
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO horizon_plan (id, item_id, title, note_id, list_title, level, period, slot, state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in', ?)
            ON CONFLICT(id) DO UPDATE SET title = excluded.title, note_id = excluded.note_id,
                list_title = excluded.list_title, slot = excluded.slot, state = 'in', updated_at = excluded.updated_at
            """, (f"{p.key}|{item_id}", item_id, title, note_id, list_title, p.level, p.start.isoformat(), slot,
                  utc_now_iso()))

    def take_out(self, row_id: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("UPDATE horizon_plan SET state = 'out', updated_at = ? WHERE id = ?", (utc_now_iso(), row_id))

    def all_records(self) -> List[Dict[str, Any]]:
        return self.rows(active=False)

    def merge_record(self, record: Dict[str, Any]) -> bool:
        cols = ["id", "item_id", "title", "note_id", "list_title", "level", "period", "slot", "state", "updated_at"]
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM horizon_plan WHERE id = ?", (record["id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO horizon_plan ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [record.get(c, "") for c in cols])
        return True


def row_period(r: Dict[str, str], week_start: int = 0) -> Period:
    return period_of(r["level"], date.fromisoformat(r["period"]), week_start)


def slot_period(r: Dict[str, str], week_start: int = 0) -> Optional[Period]:
    """A week plan's day, or a year plan's quarter; None when the item is in the period as a whole."""
    if not r.get("slot"):
        return None
    level = "day" if r["level"] == "week" else "quarter"
    return period_of(level, date.fromisoformat(r["slot"]), week_start)


# --- candidates: bubbling down ---------------------------------------------------------------------------------------

@dataclass
class Candidate:
    """One thing that could go into a period, with why it's here and what Python knows about it."""
    task_id: str
    title: str
    list: str = ""
    source: str = ""        # pinned, due, carried, from_plan, belongs, upper (strongest first)
    why: str = ""           # the reason in plain words: "pinned to Thursday", "from the November plan"
    horizon: str = ""
    due: str = ""
    deferred: int = 0       # times put off on a day (task history)
    carried: int = 0        # earlier periods at this level it was planned for and not done
    slot: str = ""          # for a week: the day it's pinned to (ISO date)
    score: float = 0.0
    task: Optional[TaskItem] = field(default=None, repr=False, compare=False)

    def facts(self) -> str:
        bits = [self.why]
        if self.due and "due" not in self.why:
            bits.append(f"due {_as_date(self.due).strftime('%a %b %-d')}" if _as_date(self.due) else "")
        if self.deferred:
            bits.append(f"put off {self.deferred}x")
        if self.carried and self.source != "carried":
            bits.append(f"carried {self.carried}x")
        return "; ".join(b for b in bits if b)

    def line(self) -> str:
        """For a model: '- Call the ENT [Medical] (due Tue Nov 3; from the November plan) id=t_ent'."""
        where = f" [{self.list}]" if self.list else ""
        return f"- {self.title}{where} ({self.facts()}) id={self.task_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "task"}


_SOURCE_SCORE = {"pinned": 100.0, "due": 80.0, "carried": 65.0, "from_plan": 55.0, "belongs": 40.0, "upper": 20.0}


class HorizonPlanner:
    """Plans across horizons for one PlanTools: periods, plans, candidates, proposals. Stateless beyond the
    database: make one when needed (``AssistantTools`` does, per call)."""

    def __init__(self, pt: "PlanTools", tasks: Optional[List[TaskItem]] = None) -> None:
        self.pt = pt
        services = getattr(pt, "services", None)
        self.tasks = list(tasks if tasks is not None else (services.tasks if services else []))
        self.db: StateDatabase = pt.scheduler.state_db
        self.week_start = week_start_index(pt.scheduler.config)
        self.catalog = HorizonCatalog(self.db)
        self.plans = HorizonPlans(self.db)

    # --- basics ---

    def today(self) -> date:
        return self.pt.today()

    def period(self, text: Any) -> Period:
        try:
            p = parse_period(text, self.today(), self.week_start)
        except ValueError as e:
            from thirties_core.assistant_tools import ToolError
            raise ToolError(str(e)) from None
        return p

    def label(self, p: Period) -> str:
        return period_label(p, self.today(), self.week_start)

    def where_pinned(self, r: Dict[str, str]) -> Period:
        """The finest period a plan row places its item in: the day of a week plan, the quarter of a year plan."""
        return slot_period(r, self.week_start) or row_period(r, self.week_start)

    def _horizon_maps(self):
        return self.catalog.items(), self.catalog.notes(), _urgencies(self.db)

    def horizon_of(self, t: TaskItem, maps=None) -> str:
        return self.catalog.effective(t, self.today(), self.week_start, *(maps or self._horizon_maps()))

    def _done_since(self, since: date) -> set:
        return self.db.completed_since(since)

    def _scheduled(self, p: Period) -> Dict[str, date]:
        """Items on the schedule within the period: task id -> the day (planning a week, an item already put on
        Thursday's plan is placed)."""
        from datetime import time as _time
        tz = self.pt.day_plan.day_start.tzinfo
        start = datetime.combine(p.start, _time(0), tzinfo=tz)
        end = datetime.combine(p.end + timedelta(days=1), _time(0), tzinfo=tz) + timedelta(hours=6)
        out: Dict[str, date] = {}
        for s in self.db.get_spans_between(start, end):
            if s.task_id and s.start_dt.date() <= p.end and s.start_dt >= start:
                out.setdefault(s.task_id, s.start_dt.date())
        return out

    # --- placing things ---

    def find_task(self, ref: str) -> TaskItem:
        """The list item, by id or its exact wording (never a near match: AssistantTools._exact_task)."""
        assistant = getattr(self.pt, "assistant", None)
        found = assistant._exact_task(str(ref)) if assistant else next(
            (t for t in self.tasks if t.id == ref or t.title.strip().lower() == str(ref).strip().lower()), None)
        if found is None:
            if assistant:
                err = assistant._no_exact_match(str(ref))
                raise type(err)(f"{err} To plan something that isn't on a list, add it with add_task first.")
            from thirties_core.assistant_tools import ToolError
            raise ToolError(f"There is no item '{ref}' on the user's lists.")
        return found

    def pin(self, task: TaskItem, p: Period) -> Tuple[Period, str]:
        """Drop an item into a period: a day goes into its week's plan on that day; a quarter into its year's plan;
        a week, month or year into its own plan. Its other placements that this one replaces (another period at
        the same level, or anything that doesn't overlap the new one) are taken out: one place per thing."""
        if p.level == "day":
            plan, slot = period_of("week", p.start, self.week_start), p.start.isoformat()
        elif p.level == "quarter":
            plan, slot = period_of("year", p.start, self.week_start), p.start.isoformat()
        else:
            plan, slot = p, ""
        for r in self.plans.rows(item_id=task.id):
            other = self.where_pinned(r)
            if r["id"] == f"{plan.key}|{task.id}":
                continue
            if (r["level"] == plan.level and r["period"] != plan.start.isoformat()) or not other.overlaps(p):
                self.plans.take_out(r["id"])
        note_id = task.parent_note_id if task.is_checkbox_item else task.id
        self.plans.put(task.id, task.title, note_id or "", task.parent_note_title or "", plan, slot)
        return plan, slot

    def unpin(self, task_id: str, p: Optional[Period] = None) -> List[Dict[str, str]]:
        """Take an item out of a period's plan (and out of any finer plan inside it), or out of every plan."""
        out = []
        for r in self.plans.rows(item_id=task_id):
            where = self.where_pinned(r)
            if p is None or (p.start <= where.start and where.end <= p.end):
                self.plans.take_out(r["id"])
                out.append(r)
        return out

    # --- the plan of a period, with what happened ---

    def plan_items(self, p: Period) -> List[Dict[str, Any]]:
        """What's pinned to a period (for a day: its week plan's items on that day), each with its status: open,
        done, scheduled (on a day's plan within it) or gone (no longer on the user's lists)."""
        if p.level == "day":
            week = period_of("week", p.start, self.week_start)
            rows = [r for r in self.plans.in_period(week) if r["slot"] == p.start.isoformat()]
        elif p.level == "quarter":
            year = period_of("year", p.start, self.week_start)
            rows = [r for r in self.plans.in_period(year) if r["slot"] == p.start.isoformat()]
        else:
            rows = self.plans.in_period(p)
        done = self._done_since(p.start)
        scheduled = self._scheduled(p)
        open_ids = {t.id for t in self.tasks}
        out = []
        for r in rows:
            status = ("done" if r["item_id"] in done else "scheduled" if r["item_id"] in scheduled
                      else "gone" if self.tasks and r["item_id"] not in open_ids else "open")
            out.append({**r, "status": status, "on": scheduled.get(r["item_id"])})
        return out

    def describe_plan(self, p: Period, with_context: bool = True) -> str:
        items = self.plan_items(p)
        head = f"{self.label(p).capitalize()} ({_span_text(p)})"
        lines = []
        for r in items:
            slot = slot_period(r, self.week_start)
            where = f"{self.label(slot)}: " if slot and p.level != "day" else ""
            status = {"done": " (done)", "gone": " (no longer on the lists)",
                      "scheduled": f" (on the plan {r['on'].strftime('%a')})" if r.get("on") else " (on the plan)"
                      }.get(r["status"], "")
            lines.append(f"- {where}{r['title']}{status}")
        from thirties_core.on_track import track_line
        how = track_line(self, p) if p.level != "day" else ""  # how on track it is (C2), counted by Python
        out = head + ":\n" + (how + "\n" if how else "") + (
            "\n".join(lines) if lines else "Nothing is planned for it yet.")
        if with_context:
            context = self.context(p)
            if context:
                out += "\n" + "\n".join(context)
        return out

    # --- context: how full things are ---

    def context(self, p: Period) -> List[str]:
        """Facts about the period's room (AGENTS.md §7: record context): per day for a day or week (work, hours
        open, items planned, items pinned), per week for a month, per month for a year."""
        today = self.today()
        if p.level in ("day", "week"):
            pins: Dict[str, int] = {}
            for r in self.plans.in_period(period_of("week", p.start, self.week_start)):
                if r["slot"]:
                    pins[r["slot"]] = pins.get(r["slot"], 0) + 1
            lines = []
            for d in p.days():
                if d < today:
                    continue
                lines.append(self._day_line(d, pins.get(d.isoformat(), 0)))
            return [line for line in lines if line]
        if p.level == "month":
            lines, w = [], period_of("week", p.start, self.week_start)
            while w.start <= p.end:
                if w.end >= today:
                    n = len(self.plans.in_period(w))
                    lines.append(f"{self.label(w).capitalize()}: {_brief(self, w) or f'{n} planned'}" if n else
                                 f"{self.label(w).capitalize()}: nothing planned yet")
                w = next_period(w, self.week_start)
            return lines
        if p.level == "year":
            lines = []
            for m in range(1, 13):
                month = period_of("month", date(p.start.year, m, 1), self.week_start)
                if month.end < today:
                    continue
                n = len(self.plans.in_period(month))
                if n:
                    lines.append(f"{month.start.strftime('%B')}: {_brief(self, month) or f'{n} planned'}")
            return lines
        return []

    def _day_line(self, d: date, pinned: int) -> str:
        from thirties_core.clock import format_clock
        from thirties_core.models import SpanCategory, SpanStatus
        try:
            plan = self.pt.plan_for(d)
        except Exception:
            return ""
        items = [s for s in plan.spans_in_view() if not s.is_envelope and not s.tentative]
        work = next((s for s in plan.active_spans() if s.category == SpanCategory.WORK), None)
        free = sum((b - a).total_seconds() for a, b in self.pt.open_windows_for(plan)
                   if (b - a).total_seconds() >= 1800 and a.hour >= 6) / 3600
        open_items = sum(1 for s in items if s.status == SpanStatus.PLANNED)
        bits = [f"work {format_clock(work.start_dt)}–{format_clock(work.end_dt)}" if work else "no work",
                f"~{free:.0f}h open", f"{open_items} on the plan"]
        if pinned:
            bits.append(f"{pinned} pinned from the week")
        return f"{d.strftime('%a %b %-d')}: " + ", ".join(bits)

    # --- candidates ---

    def candidates(self, p: Period, limit: Optional[int] = 12) -> List[Candidate]:
        """What could go into the period, best first. Pinned to it; due by its end; left over from earlier periods
        at this level; in the plan one level up and not placed lower yet; then what belongs at this horizon (and,
        last, at the one above). Never: what's done, gone, someday, routine, reference, or already placed in
        another period, or already on the schedule within this one."""
        today = self.today()
        level = "week" if p.level == "day" else p.level  # a day is planned out of its week plan
        maps = self._horizon_maps()
        rows = self.plans.rows()
        pinned_ids = {r["item_id"] for r in rows}
        tasks = {t.id: t for t in self.tasks if not t.id.startswith("media:") or t.id in pinned_ids}
        by_title: Dict[str, List[TaskItem]] = {}
        for t in tasks.values():
            by_title.setdefault(t.title.strip().lower(), []).append(t)
        for r in rows:
            if r["item_id"] not in tasks:
                # Moved or reworded outside Thirties (in Joplin): the same wording, once, is the same item. Else
                # it's said in the log, never dropped silently (10-31: the weekend's documentary).
                same = by_title.get((r["title"] or "").strip().lower(), [])
                if len(same) == 1:
                    tasks[r["item_id"]] = same[0]
                else:
                    logger.warning("A plan item can't be found on any list: %s (%s %s)", r["title"], r["level"],
                                   r["period"])
        # Done lately (since the week before this period, or this week's): an item done long ago and open again
        # in the list is a new round of it (backlog.DONE_HIDES_DAYS hides the recent ones the same way).
        done = self._done_since(min(p.start, period_of("week", today, self.week_start).start) - timedelta(days=7))
        scheduled = self._scheduled(p)
        on_today = self._on_today() if p.level == "day" else set()
        out: Dict[str, Candidate] = {}

        def add(t: TaskItem, source: str, why: str, slot: str = "") -> None:
            if t.id in done or t.id in scheduled or t.id in on_today:
                return
            c = out.get(t.id)
            if c is not None and _SOURCE_SCORE[c.source] >= _SOURCE_SCORE[source]:
                return
            out[t.id] = Candidate(t.id, t.title, t.parent_note_title or "", source, why,
                                  self.horizon_of(t, maps), t.due or "", t.deferred_count, 0, slot, task=t)

        placed_elsewhere = set()
        for r in rows:
            where = self.where_pinned(r)
            if where.end >= today and not where.overlaps(p):
                placed_elsewhere.add(r["item_id"])  # it has a current or future place outside this period

        # 1. pinned to this period (for a day: its week plan's items on that day)
        for r in rows:
            t = tasks.get(r["item_id"])
            if t is None:
                continue
            where = self.where_pinned(r)
            if p.level == "day":
                if r["level"] == "week" and r["slot"] == p.start.isoformat():
                    add(t, "pinned", f"pinned to {self.label(p)}", r["slot"])
            elif r["level"] == p.level and r["period"] == p.start.isoformat():
                slot = r["slot"]
                add(t, "pinned", f"pinned to {self.label(where) if slot else self.label(p)}", slot)

        # 2. due by the end of the period (or overdue); for a day, within two days (what today_carryover calls
        # pressing): due tomorrow means today is when there's still room for it
        due_by = p.end + timedelta(days=2 if p.level == "day" else 0)
        for t in tasks.values():
            due = _as_date(t.due)
            if due and due <= due_by and t.id not in placed_elsewhere and self._plannable(t):
                add(t, "due", "overdue" if due < today else f"due {self.label(period_of('day', due))}")

        # 3. left over: planned for an earlier period at this level and not done
        carried: Dict[str, int] = {}
        for r in rows:
            if r["level"] == level and self.where_pinned(r).end < max(p.start, today):
                carried[r["item_id"]] = carried.get(r["item_id"], 0) + 1
        for item_id, n in carried.items():
            t = tasks.get(item_id)
            if t is not None and item_id not in placed_elsewhere:
                add(t, "carried", f"left over from {'an earlier day' if p.level == 'day' else 'an earlier ' + level}"
                    + (f" ({n}x)" if n > 1 else ""))

        # 4. in the plan one level up, not placed lower yet
        up = "week" if p.level == "day" else PARENT.get(p.level)
        if up:
            for r in self.plans.rows(level=up):
                t = tasks.get(r["item_id"])
                if t is None or t.id in placed_elsewhere:
                    continue
                if not row_period(r, self.week_start).overlaps(p):
                    continue
                slot = slot_period(r, self.week_start)
                if slot is not None and not slot.overlaps(p):
                    continue  # a week plan's item for another day, or a year's item for another quarter
                if p.level == "day" and slot is not None:
                    continue  # pinned to this very day: step 1
                where = row_period(r, self.week_start)
                if p.level == "day" and where.end <= p.end + timedelta(days=1):
                    # The plan's period ends today or tomorrow ("this weekend", asked on Friday night): there's
                    # still room only now, so it's pressing, like something due (10-31)
                    left = "today" if where.end <= p.end else "tomorrow"
                    add(t, "due", f"from the plan for {self.label(where)}, which ends {left}")
                else:
                    add(t, "from_plan", f"from the plan for {self.label(where)}")

        # 5. what belongs at this horizon now, then at the one above
        here = "day" if p.level == "day" else p.level
        above = PARENT.get(here)
        for t in tasks.values():
            if t.id in placed_elsewhere or not self._plannable(t):
                continue
            h = self.horizon_of(t, maps)
            if finer(h, here):
                add(t, "belongs", HORIZON_WHY[h])
            elif above and h == above:
                add(t, "upper", HORIZON_WHY[h])

        for c in out.values():
            c.carried = carried.get(c.task_id, 0)
            c.score = _score(c, today)
        ranked = sorted(out.values(), key=lambda c: (-c.score, c.title.lower()))
        return ranked[:limit] if limit else ranked

    @staticmethod
    def _plannable(t: TaskItem) -> bool:
        """Not pinned: only items from lists and projects (and, when due, menus), never someday."""
        if t.someday or t.id.startswith("media:"):
            return False
        if t.list_role not in CANDIDATE_ROLES and not (t.list_role == "menu" and t.due):
            return False
        return True

    def _on_today(self) -> set:
        """Items already on the Today note (linked to their home lists)."""
        from thirties_core.today import TodayLinks
        return {r["home_id"] for r in TodayLinks(self.db).all()}

    # --- proposals: one offer, applied on "yes" ---

    def _proposal_key(self, p: Period) -> str:
        return f"horizon_proposal:{p.key}"

    def propose(self, p: Period, remember: bool = True) -> Tuple[str, List[Candidate]]:
        """Python's starting proposal for a period: everything pinned, then the best of the rest up to PICKS. The
        text lists the candidates with their facts and the suggestion; a 'yes' applies the suggestion."""
        if p.level not in ("week", "month", "year"):
            from thirties_core.assistant_tools import ToolError
            raise ToolError("Planning sessions are for a week, a month or a year; a day is planned on its schedule.")
        cands = self.candidates(p, limit=None)
        pinned = [c for c in cands if c.source == "pinned"]
        rest = [c for c in cands if c.source != "pinned"]
        picks = pinned + rest[:PICKS[p.level]]
        if remember and picks:
            self.db.set_sync_meta(self._proposal_key(p), json.dumps({
                "picks": [c.to_dict() for c in picks if c.source != "pinned"], "state": "offered",
                "offered_at": self.pt._now().isoformat()}))
            self.db.set_sync_meta("horizon_pending", p.key)
        head = f"Planning {self.label(p)} ({_span_text(p)})."
        if not cands:
            return head + " Nothing is waiting for it: no item is pinned, due, carried over or at this horizon.", []
        lines = [head, "Candidates (best first; facts from Python):"] + [c.line() for c in cands[:15]]
        if len(cands) > 15:
            lines.append(f"(and {len(cands) - 15} more at this horizon)")
        context = self.context(p)
        if context:
            lines += ["Room:"] + context
        lines.append("Suggested: " + "; ".join(c.title for c in picks) + ". apply_period_proposal applies this "
                     "suggestion (skip what the user doesn't want, or pass items to apply your own picks).")
        return "\n".join(lines), picks

    def session_text(self, p: Period, picks: List[Candidate]) -> str:
        """The offer at the natural moment, in the user's words: short, one proposal."""
        opening = {"week": "A new week", "month": f"A new month ({p.start.strftime('%B')})",
                   "year": f"A new year ({p.start.year})"}[p.level]
        from thirties_core.on_track import track_line
        before = previous_period(p, self.week_start)
        last = track_line(self, before, {"week": "last week", "month": before.start.strftime("%B"),
                                         "year": str(before.start.year)}[p.level])  # how the last one went (C2)
        lines = [f"{opening}." + (f" {last}" if last else "") + f" For {self.label(p)} I'd plan:"]
        for c in picks:
            why = c.facts()
            lines.append(f"- {c.title}" + (f" ({why})" if why else ""))
        lines.append(f"Want {self.label(p)} like that?")
        return "\n".join(lines)

    def pending(self) -> Optional[Period]:
        """The period whose proposal was offered and not yet answered (and is still fresh), if any."""
        key = self.db.get_sync_meta("horizon_pending")
        if not key:
            return None
        raw = self.db.get_sync_meta(f"horizon_proposal:{key}")
        data = json.loads(raw) if raw else {}
        if data.get("state") != "offered":
            return None
        offered = data.get("offered_at")
        if offered and self.pt._now() - datetime.fromisoformat(offered) > timedelta(hours=PROPOSAL_FRESH_HOURS):
            return None
        return period_from_key(key, self.week_start)

    def apply(self, p: Optional[Period] = None, skip: Any = None, items: Any = None) -> str:
        """Pin the remembered proposal's picks (minus ``skip``), or the model's own ``items``, into the period."""
        from thirties_core.assistant_tools import ToolError, _stem_words
        p = p or self.pending()
        if p is None:
            raise ToolError("There's no planning proposal waiting; make one with propose_period first.")
        raw = self.db.get_sync_meta(self._proposal_key(p))
        data = json.loads(raw) if raw else {}
        if items:
            chosen = []
            for entry in (items if isinstance(items, list) else [items]):
                ref, when = (entry.get("task") or entry.get("item") or "", entry.get("when")) if isinstance(entry, dict) \
                    else (str(entry), None)
                t = self.find_task(ref)
                chosen.append((t, self.period(when) if when else p))
        else:
            if not data:
                raise ToolError(f"There's no proposal for {self.label(p)}; make one with propose_period first.")
            if data.get("state") == "applied":
                return f"{self.label(p).capitalize()} is already planned like that."
            words = _stem_words(" ".join(map(str, skip)) if isinstance(skip, list) else str(skip or ""))
            by_id = {t.id: t for t in self.tasks}
            chosen = []
            for c in data.get("picks", []):
                if words and words & _stem_words(c["title"]):
                    continue
                t = by_id.get(c["task_id"]) or TaskItem(id=c["task_id"], source_notebook="", title=c["title"],
                                                          parent_note_title=c.get("list") or None)
                slot = period_of("day", date.fromisoformat(c["slot"])) if c.get("slot") else p
                chosen.append((t, slot if p.level == "week" else p))
        names = []
        for t, where in chosen:
            if not where.overlaps(p):
                raise ToolError(f"{self.label(where)} isn't within {self.label(p)}.")
            self.pin(t, where)
            names.append(t.title + (f" ({self.label(where)})" if where != p else ""))
        if data:
            self.db.set_sync_meta(self._proposal_key(p), json.dumps({**data, "state": "applied"}))
        if self.db.get_sync_meta("horizon_pending") == p.key:
            self.db.set_sync_meta("horizon_pending", "")
        pinned = [r["title"] for r in self.plans.in_period(p if p.level != "day" else period_of("week", p.start))]
        if not names:
            return f"Nothing added; {self.label(p)} holds: " + (", ".join(pinned) or "nothing yet") + "."
        return f"Planned for {self.label(p)}: " + ", ".join(names) + "."

    def decline(self) -> None:
        p = self.pending()
        if p is None:
            return
        raw = self.db.get_sync_meta(self._proposal_key(p))
        if raw:
            self.db.set_sync_meta(self._proposal_key(p), json.dumps({**json.loads(raw), "state": "declined"}))
        self.db.set_sync_meta("horizon_pending", "")

    # --- sessions at the natural moment ---

    def session_due(self) -> Optional[Period]:
        """The planning session to offer this morning, if any: the year's in its first days, else the month's,
        else the week's; one a morning, each once per period."""
        today = self.today()
        if self.db.get_sync_meta("horizon_session_day") == today.isoformat():
            return None
        for level in ("year", "month", "week"):
            p = period_of(level, today, self.week_start)
            if (today - p.start).days <= SESSION_GRACE_DAYS[level] \
                    and not self.db.get_sync_meta(f"horizon_session:{p.key}"):
                return p
        return None

    def offer_session(self) -> Optional[str]:
        """Called by the background service each tick in the morning: the text to send, once, or None. A period
        with nothing to plan is marked offered and skipped silently."""
        p = self.session_due()
        if p is None:
            return None
        self.db.set_sync_meta(f"horizon_session:{p.key}", self.pt._now().isoformat())
        _, picks = self.propose(p)  # remembered: a "yes" applies exactly this
        if not picks:
            return None
        self.db.set_sync_meta("horizon_session_day", self.today().isoformat())
        return self.session_text(p, picks)

    # --- for the Frontier prompt ---

    def prompt_section(self) -> str:
        """This week's, month's and year's plans, open items only, for the model to plan days and weeks from."""
        today = self.today()
        lines = []
        for level in ("week", "month", "year"):
            p = period_of(level, today, self.week_start)
            rows = [r for r in self.plan_items(p) if r["status"] in ("open", "scheduled")]
            if not rows:
                continue
            from thirties_core.on_track import summary, track
            how = summary(track(self, p))
            lines.append(f"{self.label(p).capitalize()}" + (f" ({how})" if how else "") + ":")
            for r in rows[:15]:
                slot = slot_period(r, self.week_start)
                lines.append(f"- {r['title']}" + (f" ({self.label(slot)})" if slot else "")
                             + (" (on the plan)" if r["status"] == "scheduled" else "") + f" id={r['item_id']}")
        if not lines:
            return ""
        return ("PLANS FOR THE WEEK, MONTH AND YEAR (items the user dropped into them; plan days from the week's, "
                "weeks from the month's):\n" + "\n".join(lines))


HORIZON_WHY = {"day": "can't wait", "week": "for this week", "month": "for this month", "year": "for this year",
               "someday": "someday"}


def _score(c: Candidate, today: date) -> float:
    score = _SOURCE_SCORE.get(c.source, 0.0)
    due = _as_date(c.due)
    if due:
        score += max(0, 14 - (due - today).days)  # nearer due, higher
    score += {"day": 6, "week": 3, "month": 1}.get(c.horizon, 0)
    score += min(c.deferred, 5) * 0.5 + min(c.carried, 3)
    return score


def _brief(planner: "HorizonPlanner", p: Period) -> str:
    from thirties_core.on_track import summary, track
    return summary(track(planner, p))


def _span_text(p: Period) -> str:
    if p.level == "day":
        return p.start.strftime("%a %b %-d")
    if p.level == "year":
        return str(p.start.year)
    if p.start.month == p.end.month:
        return f"{p.start.strftime('%b %-d')} – {p.end.strftime('%-d')}"
    return f"{p.start.strftime('%b %-d')} – {p.end.strftime('%b %-d')}"


# --- entry points for other parts of the core ------------------------------------------------------------------------

def candidates_for_day(pt: "PlanTools", day: Optional[date] = None, limit: Optional[int] = 12) -> List[Candidate]:
    """For the morning day proposal: what the day could hold beyond its routines, best first. Pinned to the day in
    the week plan, due, left over from an earlier day of the plan, the week plan's unplaced items, then what can't
    wait and what's for this week. Excludes what's done, already on the day's schedule or on the Today note.
    Each Candidate has .line() for a prompt, .why, .source, .task (the TaskItem) and .score."""
    planner = HorizonPlanner(pt)
    return planner.candidates(period_of("day", day or planner.today()), limit)


def candidates_for_week(pt: "PlanTools", day: Optional[date] = None, limit: Optional[int] = 12) -> List[Candidate]:
    """The same one level up: for the week containing ``day`` (default: this week)."""
    planner = HorizonPlanner(pt)
    return planner.candidates(period_of("week", day or planner.today(), planner.week_start), limit)


def candidates_for_month(pt: "PlanTools", day: Optional[date] = None, limit: Optional[int] = 12) -> List[Candidate]:
    planner = HorizonPlanner(pt)
    return planner.candidates(period_of("month", day or planner.today()), limit)


def candidates_for_year(pt: "PlanTools", day: Optional[date] = None, limit: Optional[int] = 12) -> List[Candidate]:
    planner = HorizonPlanner(pt)
    return planner.candidates(period_of("year", day or planner.today()), limit)


def day_context(pt: "PlanTools", day: Optional[date] = None) -> List[str]:
    """How full a day is (work, hours open, items planned, items pinned from the week), as fact lines."""
    planner = HorizonPlanner(pt)
    return planner.context(period_of("day", day or planner.today()))


# --- tools for the model ---------------------------------------------------------------------------------------------

_PERIOD_HELP = "'this week', 'Thursday', 'next week', 'the week of Nov 9', 'this month', 'November', 'Q1', 'next year'"

HORIZON_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "set_horizon",
        "description": "Record when something on the user's lists belongs, when they say so (\"that's a this-year "
                       "thing\", \"I want to get to it this month\", \"everything on Home is someday\"). For a whole "
                       "list, give list instead of task: its items follow it unless the user said otherwise for one. "
                       "It sticks: guesses never override it.",
        "parameters": {"task": "The item (title or task_id).",
                       "list": "Instead of task: a whole list (note title).",
                       "horizon": "'day' (can't wait), 'week', 'month', 'year' or 'someday'."},
        "required": ["horizon"],
    },
    {
        "name": "pin_to_period",
        "description": "Drop an item from the user's lists into a period's plan: a day (it goes into that week's "
                       "plan on that day), a week, a month, a quarter or a year. It then comes up when that period "
                       "is planned. Moving it to another period takes it out of the old one.",
        "parameters": {"task": "The item (title or task_id).", "when": f"The period: {_PERIOD_HELP}."},
        "required": ["task", "when"],
    },
    {
        "name": "unpin_from_period",
        "description": "Take an item out of a period's plan (or out of every plan, without when).",
        "parameters": {"task": "The item (title or task_id).", "when": "The period (optional)."},
        "required": ["task"],
    },
    {
        "name": "show_period",
        "description": "What's planned for a week, month or year (\"what's on for this week?\"), with what's done "
                       "and how full each day or week is. Read-only.",
        "parameters": {"when": f"The period: {_PERIOD_HELP} (default this week)."},
        "required": [],
    },
    {
        "name": "propose_period",
        "description": "Start planning a week, month or year: the candidates (pinned, due, left over, from the "
                       "plan one level up, and what belongs at that horizon), each with its facts, how full the "
                       "period is, and a suggestion. Read-only; remembered so apply_period_proposal applies it.",
        "parameters": {"when": "The period (default this week)."},
        "required": [],
    },
    {
        "name": "apply_period_proposal",
        "description": "Apply a planning proposal for a week, month or year after the user agrees: the suggestion "
                       "minus skip, or your own picks as items.",
        "parameters": {"when": "The period (default: the proposal just offered).",
                       "skip": "Titles or words of suggested items to leave out.",
                       "items": "Instead of the suggestion: the items to plan, as task titles or ids, or "
                                "{task, when} to put one on a day or in a week within the period."},
        "required": [],
    },
]
HORIZON_READ_ONLY = {"show_period", "propose_period"}


def horizon_handlers(assistant) -> Dict[str, Callable[..., str]]:
    """The tool handlers, bound to an AssistantTools (its PlanTools and backlog)."""
    from thirties_core.assistant_tools import ToolError

    def planner() -> HorizonPlanner:
        if assistant.pt.scheduler is None:
            raise ToolError("Plans beyond the day aren't available here.")
        return HorizonPlanner(assistant.pt, assistant.s.tasks)

    def set_horizon(horizon: str = "", task: str = "", list: str = "", **_: Any) -> str:
        h = str(horizon or "").strip().lower()
        h = {"today": "day", "now": "day", "this week": "week", "this month": "month", "this year": "year",
             "whenever": "year", "soon": "week", "never": "someday", "maybe": "someday"}.get(h, h)
        if h not in HORIZONS:
            raise ToolError("horizon must be 'day', 'week', 'month', 'year' or 'someday'.")
        hp = planner()
        if list and not task:
            key = str(list).strip().lower()
            note = next(({"id": nid, "title": title} for nid, title in assistant._notes().items()
                         if nid == list or (title or "").strip().lower() == key), None)
            if note is None:
                raise ToolError(f"There's no list called '{list}'.")
            hp.catalog.set_note(note["id"], h, note["title"], source="user")
            cleared = hp.catalog.clear_item_guesses(note["id"])
            for t in assistant.s.tasks:
                if t.parent_note_id == note["id"]:
                    t.horizon = hp.horizon_of(t)
            return f"Noted: {note['title']} is {HORIZON_WHY[h]}" + (
                f" (its {cleared} guessed item{'s' if cleared != 1 else ''} follow it)." if cleared else ".")
        if not task:
            raise ToolError("Say which item (task) or which list.")
        t = hp.find_task(task)
        hp.catalog.set_item(t.id, h, t.parent_note_id or t.id, t.title, source="user")
        from thirties_core.lists import ListCatalog  # the older axis agrees, so urgency's readers see it too
        due = _as_date(t.due)
        ListCatalog(assistant._db).set_urgency(t.id, HORIZON_URGENCY[h], due, note_id=t.parent_note_id or t.id,
                                               title=t.title, source="user")
        t.horizon, t.urgency, t.someday = h, HORIZON_URGENCY[h], h == "someday"
        return f"Noted: '{t.title}' is {HORIZON_WHY[h]}."

    def pin_to_period(task: str = "", when: Any = "", **_: Any) -> str:
        hp = planner()
        t = hp.find_task(task)
        p = hp.period(when)
        if p.end < hp.today():
            raise ToolError(f"{hp.label(p)} is over.")
        hp.pin(t, p)
        return f"'{t.title}' is planned for {hp.label(p)}."

    def unpin_from_period(task: str = "", when: Any = None, **_: Any) -> str:
        hp = planner()
        t = hp.find_task(task)
        p = hp.period(when) if when else None
        out = hp.unpin(t.id, p)
        if not out:
            raise ToolError(f"'{t.title}' isn't planned for {hp.label(p) if p else 'any period'}.")
        return f"Took '{t.title}' out of " + ", ".join(sorted({hp.label(row_period(r, hp.week_start)) for r in out})) + "."

    def show_period(when: Any = "this week", **_: Any) -> str:
        hp = planner()
        return hp.describe_plan(hp.period(when or "this week"))

    def propose_period(when: Any = "this week", **_: Any) -> str:
        hp = planner()
        text, _ = hp.propose(hp.period(when or "this week"))
        return text

    def apply_period_proposal(when: Any = None, skip: Any = None, items: Any = None, **_: Any) -> str:
        hp = planner()
        return hp.apply(hp.period(when) if when else None, skip=skip, items=items)

    return {"set_horizon": set_horizon, "pin_to_period": pin_to_period, "unpin_from_period": unpin_from_period,
            "show_period": show_period, "propose_period": propose_period,
            "apply_period_proposal": apply_period_proposal}


__all__ = ["HORIZONS", "LEVELS", "URGENCY_HORIZON", "HORIZON_URGENCY", "Period", "period_of", "parse_period",
           "period_label", "due_horizon", "HorizonCatalog", "HorizonPlans", "HorizonPlanner", "Candidate",
           "sort_horizons", "candidates_for_day", "candidates_for_week", "candidates_for_month",
           "candidates_for_year", "day_context", "HORIZON_TOOL_SPECS", "HORIZON_READ_ONLY", "horizon_handlers"]
