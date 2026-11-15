"""People to keep in touch with: the last time the user met, messaged, called or spent real time with each one,
and a gentle reminder once someone is overdue for a catch-up.

Everything lives in the app's own database (synced like the rest of the assistant's state when the sync
registers ``People``); nothing is written to the user's notes. The model only names the person and how they were
in touch; Python keeps the dates, works out how long it's been, and decides who's due:

- a person is due when their last contact of any kind is older than how often the user wants to be in touch
  with them (``every``; a month when they haven't said), and they weren't nudged about in the last week;
- a person the user says to stop tracking keeps their history but is never nudged.

The nudge goes out once a day at most, after the user is up, in a free moment (``background_duty``, called from
the background service's tick), naming up to three people, longest overdue first.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("seen", "texted", "called", "quality")
KIND_SAID = {"seen": "saw in person", "texted": "texted", "called": "talked on the phone",
             "quality": "had quality time"}
KIND_LAST = {"seen": "seen in person", "texted": "texted", "called": "on the phone", "quality": "quality time"}
_KIND_WORDS = {
    "seen": ("seen", "saw", "see", "in person", "visit", "visited", "met", "meet", "hung out", "ran into", "lunch",
             "dinner", "coffee"),
    "texted": ("text", "texted", "message", "messaged", "dm", "chat", "chatted", "emailed", "email", "wrote"),
    "called": ("call", "called", "phone", "talked", "spoke", "video", "facetime", "zoom", "rang"),
    "quality": ("quality", "quality time", "day out", "trip", "weekend", "evening together", "date"),
}
DEFAULT_EVERY_DAYS = 30
RENUDGE_DAYS = 7
NUDGE_MAX = 3
NUDGE_AFTER_HOUR = 10   # not before mid-morning
SENT_KEY = "people_nudged"   # sync meta: {person id: day nudged}, and "_day": the last day a nudge went out
SETTING = "people.nudges"    # on unless the user turns it off

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    every_days INTEGER NOT NULL DEFAULT 0,
    tracking INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_contacts (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    on_day TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS people_contacts_by_person ON people_contacts (person_id, on_day);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Person:
    id: str
    name: str
    every_days: int = 0          # 0: the default (DEFAULT_EVERY_DAYS)
    tracking: bool = True
    note: str = ""
    last: Dict[str, date] = field(default_factory=dict)   # kind -> last day

    @property
    def every(self) -> int:
        return self.every_days or DEFAULT_EVERY_DAYS

    @property
    def last_any(self) -> Optional[date]:
        return max(self.last.values()) if self.last else None


def kind_of(how: str) -> Optional[str]:
    """The kind of contact from the words used ("texted", "had coffee", "a long call")."""
    text = (how or "").strip().lower()
    if text in KINDS:
        return text
    for kind in ("quality", "called", "texted", "seen"):  # most specific first: "quality time in person"
        if any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in _KIND_WORDS[kind]):
            return kind
    return None


def parse_every(text: Any) -> Optional[int]:
    """Days from "every 2 weeks", "monthly", "weekly", "every 10 days", "3 weeks"."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    fixed = {"daily": 1, "weekly": 7, "fortnightly": 14, "biweekly": 14, "monthly": 30, "quarterly": 91,
             "yearly": 365, "every week": 7, "every month": 30, "every day": 1, "every year": 365,
             "every other week": 14, "every couple of weeks": 14, "every few weeks": 21, "every few months": 91}
    if t in fixed:
        return fixed[t]
    m = re.search(r"(\d+)\s*(day|week|month|year)s?", t)
    if m:
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
    if t.isdigit():
        return int(t)
    return None


def ago(day: date, today: date) -> str:
    days = (today - day).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{round(days / 7)} weeks ago"
    return f"{round(days / 30.4)} months ago"


class People:
    """The people and every contact with them."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    # --- people ---

    def all(self, tracking_only: bool = False) -> List[Person]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE").fetchall()
            last = conn.execute("SELECT person_id, kind, MAX(on_day) AS day FROM people_contacts "
                                "GROUP BY person_id, kind").fetchall()
        by: Dict[str, Dict[str, date]] = {}
        for r in last:
            by.setdefault(r["person_id"], {})[r["kind"]] = date.fromisoformat(r["day"])
        out = [Person(r["id"], r["name"], r["every_days"], bool(r["tracking"]), r["note"], by.get(r["id"], {}))
               for r in rows]
        return [p for p in out if p.tracking] if tracking_only else out

    def find(self, name: str) -> List[Person]:
        """By name: exact (case aside), else a first name or a part of one."""
        want = re.sub(r"\s+", " ", (name or "").strip().lower())
        if not want:
            return []
        everyone = self.all()
        exact = [p for p in everyone if p.name.lower() == want]
        if exact:
            return exact
        return [p for p in everyone if want in p.name.lower().split() or p.name.lower().startswith(want)]

    def add(self, name: str) -> Person:
        p = Person(uuid.uuid4().hex[:12], re.sub(r"\s+", " ", name.strip()))
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO people (id, name, every_days, tracking, note, updated_at) VALUES (?, ?, 0, 1, '', ?)",
                         (p.id, p.name, _now()))
        return p

    def update(self, person: Person, **fields: Any) -> None:
        cols = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()
                if k in ("name", "every_days", "tracking", "note")}
        if not cols:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self.db._get_connection() as conn:
            conn.execute(f"UPDATE people SET {sets}, updated_at = ? WHERE id = ?", (*cols.values(), _now(), person.id))

    # --- contacts ---

    def log(self, person: Person, kind: str, on_day: date, note: str = "") -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO people_contacts (id, person_id, kind, on_day, note, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex[:12], person.id, kind, on_day.isoformat(),
                                                      note.strip(), _now()))

    # --- who's due ---

    def due(self, today: date, nudged: Optional[Dict[str, str]] = None) -> List[Tuple[Person, int]]:
        """(person, days overdue) for everyone tracked whose last contact is older than they want, longest
        overdue first; a person nudged about in the last week is left out."""
        nudged = nudged or {}
        out = []
        for p in self.all(tracking_only=True):
            if p.last_any is None:
                continue  # never logged: nothing to count from
            over = (today - p.last_any).days - p.every
            when = nudged.get(p.id)
            if when and (today - date.fromisoformat(when)).days < RENUDGE_DAYS:
                continue
            if over >= 0:
                out.append((p, over))
        return sorted(out, key=lambda x: -x[1])

    # --- sync (sync_engine registers stores by name) ---

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            people = [dict(r) | {"table": "people"} for r in conn.execute("SELECT * FROM people").fetchall()]
            contacts = [dict(r) | {"table": "people_contacts"}
                        for r in conn.execute("SELECT * FROM people_contacts").fetchall()]
        return people + contacts

    def merge_record(self, record: Dict[str, Any]) -> bool:
        table = record.get("table")
        cols = {"people": ("id", "name", "every_days", "tracking", "note", "updated_at"),
                "people_contacts": ("id", "person_id", "kind", "on_day", "note", "updated_at")}.get(table)
        if not cols or not record.get("id"):
            return False
        with self.db._get_connection() as conn:
            row = conn.execute(f"SELECT updated_at FROM {table} WHERE id = ?", (record["id"],)).fetchone()
            if row is not None and row["updated_at"] >= str(record.get("updated_at") or ""):
                return False
            values = [record.get(c, 0 if c in ("every_days", "tracking") else "") for c in cols]
            conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         values)
        return True


def facts(p: Person, today: date) -> str:
    """One plain line: who, and when each kind of contact last happened."""
    parts = [f"{KIND_LAST[k]} {ago(p.last[k], today)}" for k in KINDS if k in p.last]
    every = f"; wants to be in touch every {p.every} days" if p.every_days else ""
    stop = "; not tracked" if not p.tracking else ""
    return f"{p.name}: " + (", ".join(parts) if parts else "nothing logged yet") + every + stop


# --- tools ---

PEOPLE_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "log_contact",
        "description": "Record that the user was in touch with someone they keep in touch with: saw them in person, "
                       "texted, talked on the phone, or had quality time. Use whenever they mention it. A new name "
                       "starts being kept.",
        "parameters": {"person": "Their name, as the user says it.",
                       "how": "seen, texted, called or quality (or the user's own words: 'had coffee', 'a long call').",
                       "when": "The day (default today): 'yesterday', 'Saturday', 'Oct 20'.",
                       "note": "Anything worth keeping about it (optional)."},
        "required": ["person", "how"],
    },
    {
        "name": "keep_in_touch",
        "description": "How often the user wants to be in touch with someone ('every 2 weeks', 'monthly'), or stop "
                       "tracking them (stop=true). Starts keeping a new name.",
        "parameters": {"person": "Their name.", "every": "How often (e.g. 'every 2 weeks').",
                       "stop": "true to stop the nudges for them (their history stays)."},
        "required": ["person"],
    },
    {
        "name": "people_overview",
        "description": "When the user was last in touch with each person they keep track of (seen, texted, called, "
                       "quality time), and who's due a message. Give person for just one. Read-only.",
        "parameters": {"person": "One person's name (leave out for everyone)."},
        "required": [],
    },
]
PEOPLE_READ_ONLY = {"people_overview"}


def _one(store: People, name: Any, create: bool = False) -> Tuple[Person, bool]:
    from thirties_core.assistant_tools import ToolError
    name = str(name or "").strip()
    if not name:
        raise ToolError("Say who.")
    found = store.find(name)
    if len(found) > 1:
        raise ToolError(f"'{name}' could be " + " or ".join(p.name for p in found) + ". Ask which one.")
    if found:
        return found[0], False
    if not create:
        raise ToolError(f"No one called '{name}' is kept yet.")
    return store.add(name), True


def log_contact(assistant, person: Any = "", how: Any = "", when: Any = None, note: Any = "") -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import ClockError, parse_day
    db = assistant._db
    if db is None:
        raise ToolError("This needs the app's storage.")
    kind = kind_of(str(how))
    if kind is None:
        raise ToolError("How were they in touch: seen in person, texted, called, or quality time?")
    today = assistant._today()
    try:
        day = parse_day(when, today) if when not in (None, "") else today
    except (ClockError, ValueError):
        raise ToolError(f"Which day is '{when}'?")
    if day > today:
        raise ToolError("That day hasn't happened yet.")
    store = People(db)
    p, new = _one(store, person, create=True)
    store.log(p, kind, day, str(note or ""))
    said = f"Noted: {KIND_SAID[kind]} with {p.name} {ago(day, today) if day != today else 'today'}."
    return said + (f" {p.name} is now among the people you keep in touch with." if new else "")


def keep_in_touch(assistant, person: Any = "", every: Any = None, stop: Any = None) -> str:
    from thirties_core.assistant_tools import ToolError
    db = assistant._db
    if db is None:
        raise ToolError("This needs the app's storage.")
    store = People(db)
    p, new = _one(store, person, create=True)
    if str(stop).strip().lower() in ("true", "1", "yes"):
        store.update(p, tracking=False)
        return f"No more nudges about {p.name}; what's logged stays."
    days = parse_every(every)
    if every not in (None, "") and days is None:
        raise ToolError(f"How often is '{every}'? Say it like 'every 2 weeks'.")
    store.update(p, tracking=True, **({"every_days": days} if days else {}))
    often = f"every {days} days" if days else f"every {DEFAULT_EVERY_DAYS} days unless you say"
    return f"Keeping in touch with {p.name}: a nudge if it's been more than {often}."


def overview(assistant, person: Any = "") -> str:
    db = assistant._db
    if db is None:
        return "No one is kept yet."
    store = People(db)
    today = assistant._today()
    if person not in (None, ""):
        p, _ = _one(store, person)
        return facts(p, today)
    everyone = store.all()
    if not everyone:
        return "No one is kept yet."
    due = {p.id for p, _ in store.due(today)}
    return "\n".join(facts(p, today) + ("  (due a message)" if p.id in due else "") for p in everyone)


def prompt_section(db, today: date) -> str:
    """Who's due a message, for the prompt (only when someone is)."""
    if db is None:
        return ""
    due = People(db).due(today)
    if not due:
        return ""
    return ("PEOPLE DUE A MESSAGE (it's been longer than the user wants; mention one when it fits, e.g. in a free "
            "moment, never as a lecture; log_contact when they say they were in touch)\n"
            + "\n".join(f"- {facts(p, today)}" for p, _ in due[:NUDGE_MAX]))


# --- the nudge, from the background service ---

def nudge_text(due: List[Tuple[Person, int]], today: date) -> str:
    people = [p for p, _ in due[:NUDGE_MAX]]
    if len(people) == 1:
        p = people[0]
        last = max(p.last, key=lambda k: p.last[k])
        return (f"🤝 It's been a while since you were in touch with {p.name} ({KIND_LAST[last]} "
                f"{ago(p.last[last], today)}). Maybe send a text?")
    lines = []
    for p in people:
        last = max(p.last, key=lambda k: p.last[k])
        lines.append(f"{p.name} ({KIND_LAST[last]} {ago(p.last[last], today)})")
    return "🤝 It's been a while since you were in touch with:\n" + "\n".join(lines)


def background_duty(service, now: datetime) -> Optional[str]:
    """One nudge a day at most, when someone's due: called from BackgroundService.tick (after the user is up).
    Returns the text sent, if any."""
    db = service.scheduler.state_db
    from thirties_core.settings import Settings
    if not Settings(db).get(SETTING, True) or now.hour < NUDGE_AFTER_HOUR:
        return None
    try:
        sent = json.loads(db.get_sync_meta(SENT_KEY) or "{}")
    except ValueError:
        sent = {}
    today = now.date()
    if sent.get("_day") == today.isoformat():
        return None
    if service._quiet(now) or service._talking(now):
        return None
    due = People(db).due(today, {k: v for k, v in sent.items() if not k.startswith("_")})
    if not due:
        return None
    text = nudge_text(due, today)
    service.send(text, "people", now=now)
    for p, _ in due[:NUDGE_MAX]:
        sent[p.id] = today.isoformat()
    sent["_day"] = today.isoformat()
    cutoff = (today - timedelta(days=RENUDGE_DAYS * 2)).isoformat()
    db.set_sync_meta(SENT_KEY, json.dumps({k: v for k, v in sent.items() if v >= cutoff}))
    return text


__all__ = ["People", "Person", "KINDS", "kind_of", "parse_every", "facts", "log_contact", "keep_in_touch",
           "overview", "prompt_section", "background_duty", "nudge_text", "PEOPLE_TOOL_SPECS", "PEOPLE_READ_ONLY",
           "DEFAULT_EVERY_DAYS", "SETTING"]
