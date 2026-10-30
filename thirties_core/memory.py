"""Long-lived assistant state: conversations, the user profile, memory notes, energy log.

All of it lives in the same local SQLite database as the schedule (``StateDatabase``)
and is shared by every brain and client:

- **Conversations** belong to a diurnal day. A day can have several; the most
  recent one is the "current" conversation everywhere (desktop and Telegram), so
  a chat started at the desk continues on the phone. Starting a new one makes it
  current. Brains can list, read and search past conversations through tools.
- **Profile**: what the assistant knows about the user's life (habits, near- and
  long-term goals, preferences, observations from statistics). The user can read
  and edit it; the assistant maintains it.
- **Memory**: notes about how things work for this user ("Probation items are
  maybes"). Good ones can later be promoted into prompts or config.
- **Energy log**: occasional self-reported energy levels, for statistics.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from thirties_core.scheduler import StateDatabase, utc_now_iso

PROFILE_SECTIONS = [
    "Habits & routines",
    "Near-term goals",
    "Long-term goals",
    "Preferences",
    "Observations",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    messages TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS conversations_by_day ON conversations (plan_date, created_at);
CREATE TABLE IF NOT EXISTS profile_sections (
    name TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_notes (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'assistant',
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    due_utc TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS checkin_mutes (
    id TEXT PRIMARY KEY,
    span_id TEXT NOT NULL DEFAULT '',
    until_utc TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    level INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""


def ensure_schema(db: StateDatabase) -> None:
    with db._get_connection() as conn:
        conn.executescript(_SCHEMA)


# ----------------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------------

@dataclass
class Conversation:
    id: str
    plan_date: date
    created_at: str
    updated_at: str
    title: str = ""
    summary: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")


def _row_to_conversation(row) -> Conversation:
    return Conversation(
        id=row["id"],
        plan_date=date.fromisoformat(row["plan_date"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        title=row["title"],
        summary=row["summary"],
        messages=json.loads(row["messages"]),
    )


_CHANGE_RE = re.compile(r"^(?:\[[^\]]+\]\s*)?(?:Added|Moved|Changed|Removed|Swapped|Marked|Rolled into today|Broke|"
                        r"Reminder set|Cancelled the reminder|Planned .+ again|I won't ask|Confirmed|Declined|"
                        r"Updated the profile|Remembered|Okay, I'll remind you|Got it, I'll remember)\b")


def summarize_conversation(conv: "Conversation") -> str:
    """A short, factual summary: how it started and what changed (from the tool results and confirmations)."""
    users = [str(m.get("content", "")) for m in conv.messages if m.get("role") == "user"]
    if not users:
        return ""
    changes: List[str] = []
    for m in conv.messages:
        if m.get("role") not in ("assistant", "tool"):
            continue
        for line in str(m.get("content", "")).splitlines():
            line = line.strip().lstrip("-• ").strip()
            if _CHANGE_RE.match(line):
                short = re.sub(r"\s*\[id [0-9a-f]+\]|\s*\(id [0-9a-f]+\)|\s*id=\S+", "", line)[:110]
                if short not in changes:
                    changes.append(short)
    first = users[0] if len(users[0]) <= 90 else users[0][:87] + "…"
    text = f'{len(users)} message{"s" if len(users) != 1 else ""}; started with "{first}".'
    if changes:
        text += " Changes: " + "; ".join(changes[:8]) + ("; …" if len(changes) > 8 else "") + "."
    return text


class ConversationStore:
    """Persistent conversations, grouped by diurnal day."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)

    def create(self, plan_date: date) -> Conversation:
        now = utc_now_iso()
        conv = Conversation(uuid.uuid4().hex[:12], plan_date, now, now)
        self.save(conv)
        return conv

    def save(self, conv: Conversation) -> None:
        conv.updated_at = utc_now_iso()
        if not conv.title:
            first = next((m.get("content", "") for m in conv.messages if m.get("role") == "user"), "")
            conv.title = (first[:60] + "…") if len(first) > 60 else first
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO conversations (id, plan_date, created_at, updated_at, title, summary, messages)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at, title = excluded.title,
                summary = excluded.summary, messages = excluded.messages
            """, (conv.id, conv.plan_date.isoformat(), conv.created_at, conv.updated_at, conv.title,
                  conv.summary, json.dumps(conv.messages, default=str)))

    def get(self, conv_id: str) -> Optional[Conversation]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return _row_to_conversation(row) if row else None

    def for_day(self, plan_date: date) -> List[Conversation]:
        """A day's conversations, oldest first."""
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM conversations WHERE plan_date = ? ORDER BY created_at",
                                (plan_date.isoformat(),)).fetchall()
        return [_row_to_conversation(r) for r in rows]

    def latest_for_day(self, plan_date: date) -> Optional[Conversation]:
        convs = self.for_day(plan_date)
        return convs[-1] if convs else None

    def current_or_new(self, plan_date: date) -> Conversation:
        return self.latest_for_day(plan_date) or self.create(plan_date)

    def between(self, start: date, end: date) -> List[Conversation]:
        with self.db._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE plan_date >= ? AND plan_date <= ? ORDER BY plan_date, created_at",
                (start.isoformat(), end.isoformat())).fetchall()
        return [_row_to_conversation(r) for r in rows]

    def summarize_finished(self, today: date, current_id: Optional[str] = None) -> int:
        """Write summaries for conversations that are over: from earlier days, or replaced by a newer one."""
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM conversations WHERE summary = '' ORDER BY plan_date, created_at").fetchall()
        latest_today = self.latest_for_day(today)
        done = 0
        for row in rows:
            conv = _row_to_conversation(row)
            if conv.id == current_id or (latest_today and conv.id == latest_today.id):
                continue
            summary = summarize_conversation(conv)
            if summary:
                with self.db._get_connection() as conn:
                    conn.execute("UPDATE conversations SET summary = ? WHERE id = ?", (summary, conv.id))
                done += 1
        return done

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Summaries and messages matching the query's words, best matches first (simple keyword scoring).
        A match in a conversation's summary ranks above single messages."""
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2]
        if not words:
            return []
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM conversations ORDER BY plan_date DESC").fetchall()
        hits = []
        for row in rows:
            conv = _row_to_conversation(row)
            if conv.summary:
                lowered = conv.summary.lower()
                score = sum(lowered.count(w) for w in words) + 2 * sum(1 for w in words if w in lowered)
                if score:
                    hits.append({"score": score + 3, "date": conv.plan_date.isoformat(), "conversation_id": conv.id,
                                 "role": "summary", "text": conv.summary})
            for m in conv.messages:
                if m.get("role") not in ("user", "assistant"):
                    continue
                text = str(m.get("content", ""))
                lowered = text.lower()
                score = sum(lowered.count(w) for w in words) + 2 * sum(1 for w in words if w in lowered)
                if score:
                    hits.append({"score": score, "date": conv.plan_date.isoformat(), "conversation_id": conv.id,
                                 "role": m["role"], "text": text})
        hits.sort(key=lambda h: h["date"], reverse=True)  # newest first among equal scores
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM conversations").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM conversations WHERE id = ?", (record["id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("""
            INSERT INTO conversations (id, plan_date, created_at, updated_at, title, summary, messages)
            VALUES (:id, :plan_date, :created_at, :updated_at, :title, :summary, :messages)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at, title = excluded.title,
                summary = excluded.summary, messages = excluded.messages
            """, record)
            return True


# ----------------------------------------------------------------------------
# Profile
# ----------------------------------------------------------------------------

class ProfileStore:
    """The user profile: named markdown sections the assistant maintains and the user can edit."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)

    def sections(self) -> Dict[str, str]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT name, content FROM profile_sections").fetchall()
        stored = {r["name"]: r["content"] for r in rows}
        ordered = {name: stored.get(name, "") for name in PROFILE_SECTIONS}
        ordered.update({k: v for k, v in stored.items() if k not in ordered})
        return ordered

    def set_section(self, name: str, content: str, updated_at: Optional[str] = None) -> None:
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT INTO profile_sections (name, content, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
            """, (name.strip(), content.strip(), updated_at or utc_now_iso()))

    def append(self, name: str, line: str) -> None:
        current = self.sections().get(name, "")
        entry = line.strip()
        if not entry.startswith("- "):
            entry = f"- {entry}"
        self.set_section(name, f"{current}\n{entry}".strip())

    def as_markdown(self) -> str:
        return "\n\n".join(f"## {name}\n{content or '(empty)'}" for name, content in self.sections().items())

    def is_empty(self) -> bool:
        return not any(v.strip() for v in self.sections().values())

    def save_markdown(self, text: str) -> None:
        """Replace the profile from an edited markdown document (## headings are sections)."""
        current, buf = None, []
        parsed: Dict[str, str] = {}
        for line in text.splitlines():
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                if current is not None:
                    parsed[current] = "\n".join(buf).strip()
                current, buf = m.group(1), []
            elif current is not None:
                buf.append(line)
        if current is not None:
            parsed[current] = "\n".join(buf).strip()
        for name, content in parsed.items():
            self.set_section(name, "" if content == "(empty)" else content)

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM profile_sections").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM profile_sections WHERE name = ?", (record["name"],)).fetchone()
        if row and row["updated_at"] >= record["updated_at"]:
            return False
        self.set_section(record["name"], record["content"], updated_at=record["updated_at"])
        return True


# ----------------------------------------------------------------------------
# Memory notes
# ----------------------------------------------------------------------------

@dataclass
class MemoryNote:
    id: str
    text: str
    created_at: str
    source: str = "assistant"


class MemoryStore:
    """Short notes the assistant keeps about how things work for this user."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)

    def add(self, text: str, source: str = "assistant") -> MemoryNote:
        now = utc_now_iso()
        note = MemoryNote(uuid.uuid4().hex[:8], text.strip(), now, source)
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO memory_notes (id, text, created_at, updated_at, source) VALUES (?, ?, ?, ?, ?)",
                         (note.id, note.text, now, now, source))
        return note

    def notes(self) -> List[MemoryNote]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM memory_notes WHERE deleted = 0 ORDER BY created_at").fetchall()
        return [MemoryNote(r["id"], r["text"], r["created_at"], r["source"]) for r in rows]

    def update(self, note_id: str, text: str) -> bool:
        with self.db._get_connection() as conn:
            cur = conn.execute("UPDATE memory_notes SET text = ?, updated_at = ? WHERE id = ? AND deleted = 0",
                               (text.strip(), utc_now_iso(), note_id))
            return cur.rowcount > 0

    def forget(self, note_id: str) -> bool:
        with self.db._get_connection() as conn:
            cur = conn.execute("UPDATE memory_notes SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0",
                               (utc_now_iso(), note_id))
            return cur.rowcount > 0

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM memory_notes").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM memory_notes WHERE id = ?", (record["id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("""
            INSERT INTO memory_notes (id, text, created_at, updated_at, source, deleted)
            VALUES (:id, :text, :created_at, :updated_at, :source, :deleted)
            ON CONFLICT(id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at,
                deleted = excluded.deleted
            """, record)
            return True


# ----------------------------------------------------------------------------
# Energy log
# ----------------------------------------------------------------------------

class EnergyLog:
    """Self-reported energy levels (1 = drained, 5 = great) with a timestamp."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)

    def record(self, level: int, at: Optional[datetime] = None, note: str = "") -> None:
        level = max(1, min(5, int(level)))
        when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO energy_log (id, at, level, note) VALUES (?, ?, ?, ?)",
                         (uuid.uuid4().hex[:12], when, level, note))

    def entries(self, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            if since:
                rows = conn.execute("SELECT * FROM energy_log WHERE at >= ? ORDER BY at",
                                    (since.astimezone(timezone.utc).isoformat(),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM energy_log ORDER BY at").fetchall()
        return [{"at": datetime.fromisoformat(r["at"]), "level": r["level"], "note": r["note"]} for r in rows]

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM energy_log").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO energy_log (id, at, level, note) VALUES (:id, :at, :level, :note)",
                               record)
            return cur.rowcount > 0


# ----------------------------------------------------------------------------
# Reminders and check-in muting
# ----------------------------------------------------------------------------

@dataclass
class Reminder:
    id: str
    due: datetime
    text: str
    state: str = "pending"  # pending, sent, cancelled
    span_id: str = ""       # the planned item it pings about ("remind me to..." puts the thing on the plan)


class ReminderStore:
    """Messages the user asked for at a given time ("remind me Saturday at 3 to water the alligator")."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)
        with db._get_connection() as conn:
            if "span_id" not in {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}:
                conn.execute("ALTER TABLE reminders ADD COLUMN span_id TEXT NOT NULL DEFAULT ''")

    def add(self, due: datetime, text: str, span_id: str = "") -> Reminder:
        now = utc_now_iso()
        rem = Reminder(uuid.uuid4().hex[:12], due, text.strip(), span_id=span_id)
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO reminders (id, due_utc, text, created_at, updated_at, span_id) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (rem.id, due.astimezone(timezone.utc).isoformat(), rem.text, now, now, span_id))
        return rem

    def _rows(self, where: str = "", args: tuple = ()) -> List[Reminder]:
        with self.db._get_connection() as conn:
            rows = conn.execute(f"SELECT * FROM reminders {where} ORDER BY due_utc", args).fetchall()
        return [Reminder(r["id"], datetime.fromisoformat(r["due_utc"]), r["text"], r["state"], r["span_id"] or "")
                for r in rows]

    def pending(self) -> List[Reminder]:
        return self._rows("WHERE state = 'pending'")

    def due(self, now: datetime) -> List[Reminder]:
        return [r for r in self.pending() if r.due <= now]

    def set_state(self, rem_id: str, state: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("UPDATE reminders SET state = ?, updated_at = ? WHERE id = ?", (state, utc_now_iso(), rem_id))

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM reminders").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM reminders WHERE id = ?", (record["id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO reminders (id, due_utc, text, created_at, updated_at, state, span_id) "
                         "VALUES (:id, :due_utc, :text, :created_at, :updated_at, :state, :span_id)",
                         {"span_id": "", **record})
            return True


class CheckinMutes:
    """Check-ins the user asked to stop: for one item, or for everything until a time."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        ensure_schema(db)

    def mute(self, until: datetime, span_id: str = "") -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO checkin_mutes (id, span_id, until_utc, updated_at) VALUES (?, ?, ?, ?)",
                         (uuid.uuid4().hex[:12], span_id, until.astimezone(timezone.utc).isoformat(), utc_now_iso()))

    def is_muted(self, span_id: str, now: datetime) -> bool:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT span_id, until_utc FROM checkin_mutes").fetchall()
        return any(datetime.fromisoformat(r["until_utc"]) > now and r["span_id"] in ("", span_id) for r in rows)

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM checkin_mutes").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO checkin_mutes (id, span_id, until_utc, updated_at) "
                               "VALUES (:id, :span_id, :until_utc, :updated_at)", record)
            return cur.rowcount > 0

