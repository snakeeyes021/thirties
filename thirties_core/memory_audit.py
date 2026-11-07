"""Memory notes that paper over fixed bugs, listed for the user to confirm deletion (AGENTS.md §7.5, ROADMAP D).

"A memory note that makes the assistant behave around a bug hides the bug from
tests. Fix the bug and delete the note." The 10-24 review found six of eleven
notes were workarounds; Block 0 of 0.2 fixed the bugs behind them. Thirties
never deletes a note on its own: the notes are the user's. This module only
recognises the likely workarounds (each pattern names the fix that made it
moot) and lists them; the user confirms each deletion, or keeps the note, in
Profile → Memory or with the command line:

    python -m thirties_core.memory_audit                 # list notes that look outdated
    python -m thirties_core.memory_audit --delete ID ... # delete the ones you confirm
    python -m thirties_core.memory_audit --keep ID ...   # keep them; they won't be listed again

A false positive costs one "keep"; the patterns lean towards listing.
"""

from __future__ import annotations

import argparse
import re
from typing import Dict, List, Optional, Tuple

from thirties_core.memory import MemoryNote, MemoryStore
from thirties_core.scheduler import StateDatabase, utc_now_iso

# (pattern, what fixed it). Each is a known kind of workaround from the 10-24 review and the Oct 14-30 frictions.
WORKAROUNDS: List[Tuple[str, str]] = [
    (r"\bblock (?:boundar|edge)|\bcross(?:es|ing)? (?:a |the )?(?:block|thirt)|\bstraddl",
     "contradicts the time model: items keep their real times, and only times Thirties picks are rounded (A13)"),
    (r"\broutine\w*\b.*\btoday\b|\btoday\b.*\broutine|\bcop(?:y|ied|ies)\b.*\broutine|\broutine note",
     "routines now go on Today as one group with their steps, and routine notes are never written (0.2, item 1)"),
    (r"\bshower\w*\b.*\bafter work|\bafter work\b.*\bshower",
     "was never right: shower things go after work only when the morning was missed (routines, 0.2 item 8)"),
    (r"\b(?:done|finished|completed|ticked)\b.*\b(?:offer|suggest|again|roll)|\b(?:offer|suggest)\w*\b.*\b(?:done|"
     r"finished|completed)\b",
     "what's done stays done: done items are never offered again (A2, 0.2 item 5)"),
    (r"\b(?:record|log|store)\w*\b.*\b(?:time|when)\b.*\b(?:said|say|exact|actual|really)|\bplanned (?:start|time)",
     "times are recorded as the user says them, and a done with no time is stored when it was said (A9, 0.2 item 2)"),
    (r"\b(?:never|don'?t|do not)\b.*\b(?:move|change|touch|shift)\b.*\b(?:work|sleep)\b",
     "Work and Sleep change only when the user talks about them (0.2, Block 0)"),
    (r"\bCALL\b|\btool (?:call|syntax|text)|\binternal text",
     "tool syntax is stripped from every reply in Python (0.2, item 4)"),
    (r"\b(?:duplicate|ghost|twice|copies)\b.*\b(?:plan|schedul|item|event)|\b(?:plan|schedul)\w*\b.*\b(?:duplicate|"
     r"twice)\b",
     "moving or redrawing an item moves it, never leaves a copy (0.2, item 3)"),
    (r"\b(?:tick|check(?:ed)? off|mark)\w*\b.*\b(?:wrong|exact|similar|fuzzy|match)",
     "every Joplin write goes through an exact link, never a fuzzy match (A1)"),
    (r"\b(?:never|don'?t|do not|stop)\b.*\b(?:check(?:ing)? in|ask(?:ing)?|schedul\w*)\b.*\b(?:relax|downtime|down "
     r"time|rest|break)\b",
     "check-ins follow the message level now (0.1 §4); downtime belongs in the core, not a note (C1)"),
    (r"\b(?:never|don'?t|do not)\b.*\boffer\b.*\b(?:meal|routine)",
     "routines and daily anchors now set up the day on their own (0.2 item 8)"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in WORKAROUNDS]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_review (
    note_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def why_outdated(text: str) -> Optional[str]:
    """What fixed the bug this note works around, if it looks like a workaround; else None."""
    return next((why for pattern, why in _COMPILED if pattern.search(text or "")), None)


class MemoryReview:
    """The user's answers ("keep") about listed notes, so a kept note isn't listed again. Synced."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def kept(self) -> set:
        with self.db._get_connection() as conn:
            return {r["note_id"] for r in conn.execute("SELECT note_id FROM memory_review WHERE decision = 'keep'")}

    def keep(self, note_id: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO memory_review (note_id, decision, updated_at) VALUES (?, 'keep', ?) "
                         "ON CONFLICT(note_id) DO UPDATE SET decision = 'keep', updated_at = excluded.updated_at",
                         (note_id, utc_now_iso()))

    def all_records(self) -> List[Dict[str, str]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM memory_review").fetchall()]

    def merge_record(self, record: Dict[str, str]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM memory_review WHERE note_id = ?", (record["note_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO memory_review (note_id, decision, updated_at) "
                         "VALUES (:note_id, :decision, :updated_at)", record)
            return True


def to_review(db: StateDatabase) -> List[Tuple[MemoryNote, str]]:
    """The live notes that look like workarounds for fixed bugs and that the user hasn't chosen to keep."""
    kept = MemoryReview(db).kept()
    out = []
    for note in MemoryStore(db).notes():
        why = why_outdated(note.text)
        if why and note.id not in kept:
            out.append((note, why))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="List memory notes that paper over fixed bugs; delete or keep them.")
    parser.add_argument("--db", help="The state database (default: Thirties' own).")
    parser.add_argument("--delete", nargs="+", metavar="ID", default=[], help="Delete these notes (you confirm).")
    parser.add_argument("--keep", nargs="+", metavar="ID", default=[], help="Keep these; don't list them again.")
    args = parser.parse_args(argv)
    from pathlib import Path
    db = StateDatabase(Path(args.db) if args.db else None)
    memory, review = MemoryStore(db), MemoryReview(db)
    for note_id in args.delete:
        print(f"Deleted {note_id}." if memory.forget(note_id) else f"No note {note_id}.")
    for note_id in args.keep:
        review.keep(note_id)
        print(f"Keeping {note_id}.")
    listed = to_review(db)
    if not listed:
        print("No memory notes look like workarounds for fixed bugs.")
        return 0
    print("These notes look like workarounds for bugs that are now fixed. Nothing is deleted unless you say so:")
    for note, why in listed:
        print(f"  {note.id}  {note.text}\n            ({why})")
    print("Delete with --delete ID ..., or keep with --keep ID ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
