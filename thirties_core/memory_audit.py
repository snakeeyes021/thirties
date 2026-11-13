"""Memory notes that cover something Thirties now does by itself, offered for removal (AGENTS.md §7.5).

After an update, any note that only asks for a behaviour Thirties has since built in (or works around a bug it
has since fixed) is offered in Profile, Memory as "Now built in, remove?". It shows the user what the
assistant had been working around. Each built-in behaviour is in the list here, with the version that built it in. A note
is matched by the patterns here, and, once after each update, by the assistant's model, which is given the list
of built-in behaviours and the notes the patterns didn't catch, and names any note that only asks for one of
them (Python checks the ids). Nothing is ever removed without the user's tap; a note the user keeps is offered
again only for a behaviour built in after they kept it.

A memory note that makes the assistant behave around a bug hides the bug from tests: fix the bug, then the note
can go. The notes are the user's, so this module only lists; the user removes each one, or keeps it, in
Profile → Memory or with the command line:

    python -m thirties_core.memory_audit                 # list notes that look outdated
    python -m thirties_core.memory_audit --delete ID ... # delete the ones you confirm
    python -m thirties_core.memory_audit --keep ID ...   # keep them; they won't be listed again

A false positive costs one "keep"; the patterns lean towards listing.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import Dict, List, Optional, Tuple

from thirties_core.memory import MemoryNote, MemoryStore
from thirties_core.scheduler import StateDatabase, utc_now_iso

logger = logging.getLogger(__name__)

# (pattern, what Thirties now does by itself, the version that built it in)
WORKAROUNDS: List[Tuple[str, str, str]] = [
    (r"\bblock (?:boundar|edge)|\bcross(?:es|ing)? (?:a |the )?(?:block|thirt)|\bstraddl",
     "items keep their real times; only the times Thirties picks itself are rounded to a block", "0.2.0"),
    (r"\broutine\w*\b.*\btoday\b|\btoday\b.*\broutine|\bcop(?:y|ied|ies)\b.*\broutine|\broutine note",
     "routines go on Today as one group with their steps, and routine notes are never written", "0.2.0"),
    (r"\bshower\w*\b.*\bafter work|\bafter work\b.*\bshower",
     "a routine's parts go where they belong in the day; one goes after work only when its morning was missed", "0.2.0"),
    (r"\b(?:done|finished|completed|ticked)\b.*\b(?:offer|suggest|again|roll)|\b(?:offer|suggest)\w*\b.*\b(?:done|"
     r"finished|completed)\b",
     "what's done stays done: a done item is never offered again", "0.2.0"),
    (r"\b(?:record|log|store)\w*\b.*\b(?:time|when)\b.*\b(?:said|say|exact|actual|really)|\bplanned (?:start|time)",
     "times are recorded as the user says them, and a done with no time is stored as when it was said", "0.2.0"),
    (r"\b(?:never|don'?t|do not)\b.*\b(?:move|change|touch|shift)\b.*\b(?:work|sleep)\b",
     "Work and Sleep change only when the user talks about them", "0.2.0"),
    (r"\bCALL\b|\btool (?:call|syntax|text)|\binternal text",
     "no tool syntax or internal text ever reaches a reply", "0.2.0"),
    (r"\b(?:duplicate|ghost|twice|copies)\b.*\b(?:plan|schedul|item|event)|\b(?:plan|schedul)\w*\b.*\b(?:duplicate|"
     r"twice)\b",
     "moving or redrawing an item moves it and never leaves a copy", "0.2.0"),
    (r"\b(?:tick|check(?:ed)? off|mark)\w*\b.*\b(?:wrong|exact|similar|fuzzy|match)",
     "every write to the lists goes through an exact link to the item, never a guess by its wording", "0.2.0"),
    (r"\b(?:never|don'?t|do not|stop)\b.*\b(?:check(?:ing)? in|ask(?:ing)?|schedul\w*)\b.*\b(?:relax|downtime|down "
     r"time|rest|break)\b",
     "check-ins follow the chosen message level, and downtime is never filled with plans or check-ins", "0.2.0"),
    (r"\b(?:never|don'?t|do not)\b.*\boffer\b.*\b(?:meal|routine)",
     "routines and daily anchors such as meals go on the day by themselves", "0.2.0"),
    # rules a note had to carry that are now built in (0.3)
    (r"\bhalf[- ]?hour\b.*\bblocks?\b|\bblocks?\b.*\bhalf[- ]?hour\b|\bschedul\w*\b.*\b(?:by|in) (?:the )?blocks?\b",
     "Thirties places what it picks on half-hour blocks by itself; the times the user gives stay as they are", "0.3.0"),
    (r"\bappl(?:y|ies|ied)\b.*\b(?:proposals?|plans?)\b.*\b(?:without|instead of|rather than)\b.*\bask|"
     r"\bproposals?\b.*\b(?:directly|right away|straight away)\b",
     "a proposed schedule is the schedule: it goes on at once and is said as a notice", "0.3.0"),
    (r"\btickets?\b.*\b(?:schedul\w*|plan\w*)\b|\b(?:schedul\w*|plan\w*)\b.*\btickets?\b",
     "a project's backlog (an agent's work log too) is planned as time on the project, never ticket by ticket", "0.3.0"),
    (r"\broutines?\b.*\b(?:overviews?|summar(?:y|ies)|morning (?:message|schedule))\b|"
     r"\b(?:overviews?|summar(?:y|ies))\b.*\broutines?\b",
     "the morning's schedule lists the routines, part by part", "0.3.0"),
    (r"\broutines?\b.*\b(?:same|usual|right) order\b|\border of (?:the )?routine",
     "routines go on as their parts, in the routine's order, where the user last put them", "0.3.0"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), why, since) for p, why, since in WORKAROUNDS]
# What the model is asked about after an update: each behaviour once, with its version
BUILT_IN: List[Tuple[str, str]] = list(dict.fromkeys((why, since) for _, why, since in WORKAROUNDS))
VERSION_KEY = "memory_review_version"  # the app version the notes were last looked over for

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_review (
    note_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# decision 'keep' (since: the version it was kept at) or 'offer' (why and since: the behaviour the model matched)
_ADDED = {"why": "TEXT NOT NULL DEFAULT ''", "since": "TEXT NOT NULL DEFAULT ''"}


def _version(text: str) -> Tuple[int, ...]:
    """'0.3.0' as (0, 3, 0); a keep from before versions were kept counts as 0.2.0."""
    parts = [int(x) for x in re.findall(r"\d+", text or "")[:3]]
    return tuple(parts) if parts else (0, 2, 0)


def match(text: str) -> Optional[Tuple[str, str]]:
    """(what Thirties now does by itself, since which version) for a note the patterns recognise; else None."""
    return next(((why, since) for pattern, why, since in _COMPILED if pattern.search(text or "")), None)


def why_outdated(text: str) -> Optional[str]:
    """What Thirties now does by itself that this note asks for, if the patterns recognise it; else None."""
    m = match(text)
    return m[0] if m else None


class MemoryReview:
    """The user's answers ("keep") about listed notes, so a kept note isn't listed again. Synced."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)
            have = {r[1] for r in conn.execute("PRAGMA table_info(memory_review)").fetchall()}
            for col, kind in _ADDED.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE memory_review ADD COLUMN {col} {kind}")

    def _rows(self, decision: str) -> Dict[str, Dict[str, str]]:
        with self.db._get_connection() as conn:
            return {r["note_id"]: dict(r) for r in conn.execute("SELECT * FROM memory_review WHERE decision = ?",
                                                                 (decision,))}

    def kept(self) -> set:
        return set(self._rows("keep"))

    def kept_at(self) -> Dict[str, Tuple[int, ...]]:
        """Each kept note, with the version it was kept at."""
        return {nid: _version(r.get("since") or "") for nid, r in self._rows("keep").items()}

    def offered(self) -> Dict[str, Tuple[str, str]]:
        """Notes the model matched to a built-in behaviour: {note id: (why, since)}."""
        return {nid: (r.get("why") or "", r.get("since") or "") for nid, r in self._rows("offer").items()}

    def keep(self, note_id: str, version: str = "") -> None:
        """Kept by the user: not offered again, except for a behaviour built in after ``version``."""
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO memory_review (note_id, decision, updated_at, why, since) VALUES (?, 'keep', ?, "
                         "'', ?) ON CONFLICT(note_id) DO UPDATE SET decision = 'keep', updated_at = excluded.updated_at,"
                         " why = '', since = excluded.since", (note_id, utc_now_iso(), version))

    def offer(self, note_id: str, why: str, since: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO memory_review (note_id, decision, updated_at, why, since) VALUES (?, 'offer', ?, "
                         "?, ?) ON CONFLICT(note_id) DO UPDATE SET decision = 'offer', updated_at = "
                         "excluded.updated_at, why = excluded.why, since = excluded.since",
                         (note_id, utc_now_iso(), why, since))

    def all_records(self) -> List[Dict[str, str]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM memory_review").fetchall()]

    def merge_record(self, record: Dict[str, str]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM memory_review WHERE note_id = ?", (record["note_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO memory_review (note_id, decision, updated_at, why, since) "
                         "VALUES (:note_id, :decision, :updated_at, :why, :since)",
                         {"why": "", "since": "", **record})
            return True


def to_review(db: StateDatabase) -> List[Tuple[MemoryNote, str]]:
    """The live notes that cover something Thirties now does by itself (the patterns' match, or the model's from
    the last update), with what that is; a kept note only for a behaviour built in after it was kept."""
    review = MemoryReview(db)
    kept, offered = review.kept_at(), review.offered()
    out = []
    for note in MemoryStore(db).notes():
        found = match(note.text) or offered.get(note.id)
        if not found:
            continue
        why, since = found
        if note.id in kept and kept[note.id] >= _version(since):
            continue
        out.append((note, why))
    return out


JUDGE = ("These are things a personal planning assistant now does by itself:\n{behaviours}\n\n"
         "These are notes its user's assistant keeps in memory:\n{notes}\n\n"
         "Which notes ask only for one of the behaviours above, so the note adds nothing now? A note asking for "
         "more, or for a different behaviour, is not one. Reply with JSON only: "
         '{{"notes": [{{"id": "<note id>", "behaviour": <number>}}]}}, an empty list if none.')


def review_after_update(db: StateDatabase, version: str, engine=None) -> int:
    """Once per app version: the notes the patterns don't catch go to the model with the built-in behaviours, and
    any it names are offered (Python checks each id and number). Returns how many notes are offered now. Without
    a model (or with the mock), only the patterns count."""
    if not version or db.get_sync_meta(VERSION_KEY) == version:
        return len(to_review(db))
    review = MemoryReview(db)
    kept = review.kept_at()
    notes = [n for n in MemoryStore(db).notes() if not match(n.text)
             and not (n.id in kept and kept[n.id] >= _version(version))]
    if notes and engine is not None and type(engine).__name__ != "MockInferenceEngine":
        from thirties_core.brains.slm.parse import extract_json
        behaviours = "\n".join(f"{i + 1}. {why}" for i, (why, _) in enumerate(BUILT_IN))
        listed = "\n".join(f"- {n.id}: {n.text}" for n in notes)
        try:
            reply = engine.chat([{"role": "user", "content": JUDGE.format(behaviours=behaviours, notes=listed)}])
            data = extract_json(str(reply.get("content") or "")) or {}
            ids = {n.id for n in notes}
            for entry in data.get("notes") or []:
                nid, k = str(entry.get("id") or ""), entry.get("behaviour")
                if nid in ids and isinstance(k, int) and 1 <= k <= len(BUILT_IN):
                    review.offer(nid, *BUILT_IN[k - 1])
        except Exception as e:  # the patterns still count; the model is asked again after the next update
            logger.warning("Couldn't ask the model about memory notes: %s", e)
            return len(to_review(db))
    db.set_sync_meta(VERSION_KEY, version)
    return len(to_review(db))


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
