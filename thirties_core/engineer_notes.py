"""Notes to the engineers: kept for review, never acted on.

When the user writes a note to the people who build Thirties ("note to the engineers: the check-in came twice"),
it's for them to catch, not for the assistant to do anything about (10-24). Python spots it, keeps it in the app's
own database with when and where it was said, and mirrors every open note to a plain file next to the database
(``notes-to-engineers.md``), so anyone reviewing the app can read them at a glance (``python3 -m
thirties_core.engineer_notes`` prints them too). The assistant can list them when asked ("what have I told the
engineers?").

A message that is only a note gets a short acknowledgement from Python and never reaches the model, so nothing in
it can be acted on. For a note inside a longer message, the model is told (before the message) which part is the
note, that it's kept, and to act only on the rest.
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE_NAME = "notes-to-engineers.md"
ACK = "Kept for the engineers."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engineer_notes (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    via TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open'
);
"""

_WHO = r"(?:engineers?|developers?|dev\s+team|devs?|programmers?|the\s+team\s+that\s+builds?\s+(?:this|thirties))\b"
# "note to the engineers: ...", "(note for devs) ...", "engineers: ...", "dev note: ...", "@engineers ..."
_OPENERS = re.compile(
    r"(?:\(|\[)?\s*(?:(?:a\s+)?(?:quick\s+|side\s+)?note\s+(?:to|for)\s+(?:the\s+)?" + _WHO
    + r"|(?:hey\s+|attention\s+|@)(?:the\s+)?" + _WHO
    + r"|(?:dev|developer|engineering|engineer)\s+note"
    + r"|(?:for|to)\s+the\s+" + _WHO + r"(?=\s*[:\-–—]))"
    + r"\s*(?:\)|\])?\s*[:\-–—,]?\s*",
    re.IGNORECASE)
_LEADING_WHO = re.compile(r"^\s*(?:the\s+)?" + _WHO + r"\s*[:\-–—]\s*", re.IGNORECASE)


@dataclass
class EngineerNote:
    id: str
    at: str
    via: str
    text: str
    message: str
    state: str = "open"


def split(message: str) -> Tuple[str, str]:
    """(the note to the engineers, the rest of the message). The note runs from its opener ("note to the
    engineers:") to the end of that paragraph (or of a bracketed aside); ("", message) when there's none."""
    text = message or ""
    m = _OPENERS.search(text)
    if m is None:
        m = _LEADING_WHO.match(text)
        if m is None:
            return "", text
    start, body_start = m.start(), m.end()
    bracket = text[start:start + 1] in "([" and text[m.end() - 1:m.end()] not in ")]"
    if bracket:  # "(note to devs: the bell rang twice) also, move lunch": the aside ends at its closing bracket
        close = text.find(")" if text[start] == "(" else "]", body_start)
        end = close if close != -1 else len(text)
        rest_from = end + 1 if close != -1 else end
    else:
        para = text.find("\n\n", body_start)
        end = rest_from = para if para != -1 else len(text)
    note = text[body_start:end].strip(" \n:-–—")
    if not note:
        return "", text
    rest = (text[:start] + " " + text[rest_from:]).strip()
    return note, re.sub(r"[ \t]{2,}", " ", rest)


def only_a_note(rest: str) -> bool:
    """Nothing left to act on besides the note (a greeting or "thanks" doesn't count)."""
    words = re.findall(r"[a-z']+", (rest or "").lower())
    return not [w for w in words if w not in {"also", "and", "ok", "okay", "thanks", "thank", "you", "hey", "hi",
                                              "fyi", "btw", "oh", "so", "um", "uh", "lol"}]


class EngineerNotes:
    """The kept notes, in the app's database, mirrored to ``notes-to-engineers.md`` beside it."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    @property
    def file(self) -> Optional[Path]:
        path = getattr(self.db, "db_path", None)
        return Path(path).parent / FILE_NAME if path else None

    def add(self, text: str, message: str = "", via: str = "", at: Optional[datetime] = None) -> EngineerNote:
        note = EngineerNote(uuid.uuid4().hex[:12], (at or datetime.now().astimezone()).isoformat(timespec="seconds"),
                            via, text.strip(), message.strip())
        with self.db._get_connection() as conn:
            same = conn.execute("SELECT * FROM engineer_notes WHERE text = ? AND message = ? AND state = 'open'",
                                (note.text, note.message)).fetchone()
            if same is not None:  # the same message again (a turn retried after the model was unreachable)
                return EngineerNote(same["id"], same["at"], same["via"], same["text"], same["message"], same["state"])
            conn.execute("INSERT INTO engineer_notes (id, at, via, text, message, state) VALUES (?, ?, ?, ?, ?, ?)",
                         (note.id, note.at, note.via, note.text, note.message, note.state))
        self.write_file()
        return note

    def notes(self, state: Optional[str] = "open") -> List[EngineerNote]:
        with self.db._get_connection() as conn:
            if state:
                rows = conn.execute("SELECT * FROM engineer_notes WHERE state = ? ORDER BY at", (state,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM engineer_notes ORDER BY at").fetchall()
        return [EngineerNote(r["id"], r["at"], r["via"], r["text"], r["message"], r["state"]) for r in rows]

    def mark(self, note_id: str, state: str = "reviewed") -> bool:
        with self.db._get_connection() as conn:
            changed = conn.execute("UPDATE engineer_notes SET state = ? WHERE id = ?", (state, note_id)).rowcount
        self.write_file()
        return bool(changed)

    def as_text(self) -> str:
        notes = self.notes()
        if not notes:
            return "# Notes to the engineers\n\nNone open.\n"
        out = ["# Notes to the engineers", ""]
        for n in notes:
            when = n.at.replace("T", " ")[:16]
            out.append(f"## {when}" + (f" ({n.via})" if n.via else "") + f" [{n.id}]")
            out.append(n.text)
            if n.message and n.message != n.text:
                out += ["", "Said in: " + " ".join(n.message.split())]
            out.append("")
        return "\n".join(out)

    def write_file(self) -> None:
        path = self.file
        if path is None:
            return
        try:
            path.write_text(self.as_text(), encoding="utf-8")
        except OSError:
            pass  # the database keeps them; the file is only a copy to read


# --- in a turn ---

def take(db, message: str, via: str = "", at: Optional[datetime] = None) -> Tuple[Optional[EngineerNote], str]:
    """Keep the message's note to the engineers, if it has one. Returns (the kept note or None, what's left of
    the message for the assistant to act on)."""
    note, rest = split(message)
    if not note or db is None:
        return None, message
    return EngineerNotes(db).add(note, message, via, at), rest


def hidden_instruction(note: EngineerNote) -> str:
    """For the model, when a note came inside a longer message."""
    return (f"(The user's message has a note to the engineers who build Thirties: \"{note.text}\". It's been kept "
            f"for them. Take no action on it and change nothing because of it; at most say in a few words that it's "
            f"kept. Answer the rest of the message as usual.)")


# --- tools ---

ENGINEER_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "engineer_notes",
    "description": "List the notes the user has left for the engineers who build Thirties (kept for them to review; "
                   "never acted on). Use when the user asks what they've told the engineers.",
    "parameters": {},
    "required": [],
}]
ENGINEER_READ_ONLY = {"engineer_notes"}


def list_tool(db) -> str:
    if db is None:
        return "No notes for the engineers."
    notes = EngineerNotes(db).notes()
    if not notes:
        return "No notes for the engineers are waiting."
    return "Kept for the engineers:\n" + "\n".join(f"{n.at[:10]}: {n.text}" for n in notes)


def main(argv: List[str]) -> int:
    """Print the open notes (``python3 -m thirties_core.engineer_notes [state.sqlite]``)."""
    from thirties_core.scheduler import StateDatabase
    db = StateDatabase(Path(argv[1])) if len(argv) > 1 else StateDatabase()
    print(EngineerNotes(db).as_text())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


__all__ = ["EngineerNotes", "EngineerNote", "split", "only_a_note", "take", "hidden_instruction", "ACK",
           "ENGINEER_TOOL_SPECS", "ENGINEER_READ_ONLY", "list_tool", "FILE_NAME"]
