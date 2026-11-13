"""Item identity that follows the user's edits.

An item's id used to be worked out from its wording (a hash of note, text and which open copy it was), so an edit
or a move made in Joplin orphaned everything Thirties kept about the item, ticking one copy of a repeated line gave
the next copy its id ("done comes back", hidden copies), and Thirties had to re-key its own records before Joplin
even had the change.

Now Thirties keeps a table of every checklist line it has seen, and an id is never derived from text again:

- **On each read**, a note's lines are matched to what was seen last time (a line diff). Unchanged lines keep their
  ids; a reworded line keeps its id (paired with the line it replaced when the wording is close enough); an open
  line that left a note and turns up elsewhere (reordered, or moved to another note) keeps its id; a new line gets
  a new id. Ticking keeps the id, so a finished copy and a later copy of the same wording are different items, and
  a finished line that's cleared away and written again is a new item.
- **Thirties' own changes** say what they expect: a rewording or a move keeps the item's id (``expect``), and a
  line Thirties adds gets the id it reserved (``reserve``). The id changes hands only when a read shows the new line,
  that is, once Joplin has the change; if it never lands, the expectation lapses and nothing points at a missing id.
- **The first read** of a note keeps the ids its items already had (the old hash), so plans, links and history
  made before this keep working. A finished copy recorded done under the open copy's old id gets that id back.

New ids still look like the old ones (the hash, with the next free copy number), so they're 16 hex characters and
a brand-new wording gets the very id it would have had before.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, item_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_ids (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    pos INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'live',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS item_ids_by_note ON item_ids (note_id, state);
CREATE TABLE IF NOT EXISTS item_id_notes (
    note_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS item_id_expected (
    note_id TEXT NOT NULL,
    text TEXT NOT NULL,
    id TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (note_id, text, id)
);
"""

MOVE_WINDOW = timedelta(days=1)  # an open line that left a note this recently can turn up in another and keep its id
EXPECT_FOR = timedelta(days=3)   # Thirties' own change, waiting for Joplin to have it
REWORD_RATIO = 0.5               # how alike a line and the one it replaced must be to be the same item reworded

_lock = threading.RLock()  # one identity pass at a time in this process (the window's, the background's, Telegram's)


def checklist_lines(body: Optional[str]) -> List[Tuple[str, bool]]:
    """A note's checklist lines in order, open and ticked: (text, ticked). The same lines, in the same order, as
    joplin_engine.checklist_tasks walks."""
    out: List[Tuple[str, bool]] = []
    for line in (body or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        if m:
            out.append((m.group(2).strip(), False))
            continue
        m = DONE_CHECKBOX_REGEX.match(line)
        if m:
            out.append((m.group(2).strip(), True))
    return out


def _digest(lines: List[Tuple[str, bool]]) -> str:
    return hashlib.sha256(json.dumps(lines).encode("utf-8")).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


class ItemIds:
    """The id table, over a StateDatabase."""

    _ready: set = set()  # databases whose tables exist already (lookups are frequent)

    def __init__(self, db) -> None:
        self.db = db
        key = str(getattr(db, "db_path", id(db)))
        if key not in ItemIds._ready:
            with db._get_connection() as conn:
                conn.executescript(SCHEMA)
            ItemIds._ready.add(key)

    # --- lookups ---

    def find(self, note_id: str, text: str, done: Optional[bool] = None, occurrence: int = 0) -> Optional[str]:
        """The id of the line in this note with this wording (the n-th such copy; open or ticked if ``done`` is
        None), as of the last read."""
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT id, text, done FROM item_ids WHERE note_id = ? AND state = 'live' ORDER BY pos",
                                (note_id,)).fetchall()
        hits = [r["id"] for r in rows if r["text"] == text.strip() and (done is None or bool(r["done"]) == done)]
        if not hits:
            hits = [r["id"] for r in rows if _same(r["text"], text) and (done is None or bool(r["done"]) == done)]
        return hits[occurrence] if occurrence < len(hits) else None

    def find_all(self, note_id: str, text: str) -> List[str]:
        """Every id this wording has had in this note, live or gone (for "was this one of ours?")."""
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT id, text FROM item_ids WHERE note_id = ?", (note_id,)).fetchall()
        return [r["id"] for r in rows if _same(r["text"], text)]

    def revision(self, note_id: str) -> int:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT revision FROM item_id_notes WHERE note_id = ?", (note_id,)).fetchone()
        return int(row["revision"]) if row else 0

    def ids_for(self, note: Dict[str, Any]) -> List[str]:
        """The ids of one note's checklist lines, in order (following any change since the last read)."""
        return self.follow([note]).get(note.get("id", ""), [])

    # --- Thirties' own changes ---

    def reserve(self, note_id: str, text: str) -> str:
        """The id a line Thirties is adding to this note will have: a new item's, never one already used."""
        with _lock, self.db._get_connection() as conn:
            row = conn.execute("SELECT id FROM item_id_expected WHERE note_id = ? AND text = ?",
                               (note_id, text.strip())).fetchone()
            if row and not self._live(conn, row["id"]):
                return row["id"]
            new = self._new_id(conn, note_id, text, set())
            conn.execute("INSERT OR REPLACE INTO item_id_expected (note_id, text, id, at) VALUES (?, ?, ?, ?)",
                         (note_id, text.strip(), new, _now().isoformat()))
            return new

    def expect(self, note_id: str, text: str, item: str) -> None:
        """Thirties is rewording or moving the item ``item``: when a read shows a new line worded ``text`` in this
        note, it's that item (nothing is re-keyed before Joplin has the change)."""
        with _lock, self.db._get_connection() as conn:
            conn.execute("DELETE FROM item_id_expected WHERE id = ?", (item,))
            conn.execute("INSERT OR REPLACE INTO item_id_expected (note_id, text, id, at) VALUES (?, ?, ?, ?)",
                         (note_id, text.strip(), item, _now().isoformat()))

    # --- following the notes ---

    def follow(self, notes: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Match each note's checklist lines to the last read and return {note id: ids of its lines, in order}."""
        notes = [n for n in notes if n.get("id")]
        out: Dict[str, List[str]] = {}
        with _lock, self.db._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")  # one pass at a time across processes too
            now = _now()
            changed = []
            for n in notes:
                lines = checklist_lines(n.get("body"))
                digest = _digest(lines)
                seen = conn.execute("SELECT digest FROM item_id_notes WHERE note_id = ?", (n["id"],)).fetchone()
                prev = conn.execute("SELECT id, text, done FROM item_ids WHERE note_id = ? AND state = 'live' "
                                    "ORDER BY pos", (n["id"],)).fetchall()
                if seen is not None and seen["digest"] == digest and len(prev) == len(lines):
                    out[n["id"]] = [r["id"] for r in prev]
                    continue
                changed.append((n["id"], lines, [dict(r) for r in prev], digest, seen is None and not prev))
            if not changed:
                return out
            self._expire(conn, now)
            expected = {(r["note_id"], r["text"].lower()): r["id"]
                        for r in conn.execute("SELECT note_id, text, id FROM item_id_expected").fetchall()}
            assigned: Dict[str, List[Optional[str]]] = {}
            inserted: List[Tuple[str, int]] = []   # (note id, line index)
            removed: List[Dict[str, Any]] = []     # rows whose line left this pass
            for note_id, lines, prev, _, first in changed:
                if first:
                    assigned[note_id] = self._seed(conn, note_id, lines)
                    continue
                ids, ins, rem = self._match(prev, lines)
                assigned[note_id] = ids
                inserted += [(note_id, j) for j in ins]
                removed += [dict(prev[i], note_id=note_id) for i in rem]
            in_pass = {i for ids in assigned.values() for i in ids if i}
            changed_ids = {c[0] for c in changed}
            lines_of = {c[0]: c[1] for c in changed}
            reused: set = set()
            recent = [dict(r) for r in conn.execute(
                "SELECT id, note_id, text, done FROM item_ids WHERE state = 'gone' AND done = 0 AND updated_at >= ?",
                ((now - MOVE_WINDOW).isoformat(),)).fetchall()]
            # Expectations first (Thirties' own rewordings, moves and additions), then lines that moved, then new.
            for note_id, j in sorted(inserted, key=lambda x: (x[0], x[1])):
                text = lines_of[note_id][j][0]
                want = expected.get((note_id, text.lower()))
                if want and want not in in_pass and want not in reused and not self._live_elsewhere(
                        conn, want, changed_ids):
                    assigned[note_id][j] = want
                    reused.add(want)
                    conn.execute("DELETE FROM item_id_expected WHERE id = ?", (want,))
            for note_id, j in inserted:
                if assigned[note_id][j]:
                    continue
                text = lines_of[note_id][j][0]
                pool = sorted([r for r in removed if not r["done"] and _same(r["text"], text)],
                              key=lambda r: r["note_id"] != note_id) + \
                    [r for r in recent if _same(r["text"], text)]
                hit = next((r for r in pool if r["id"] not in reused and r["id"] not in in_pass
                            and not self._live_elsewhere(conn, r["id"], changed_ids)), None)
                if hit is not None:
                    assigned[note_id][j] = hit["id"]
                    reused.add(hit["id"])
            taken = in_pass | reused
            for note_id, j in inserted:
                if not assigned[note_id][j]:
                    new = self._new_id(conn, note_id, lines_of[note_id][j][0], taken)
                    assigned[note_id][j] = new
                    taken.add(new)
            # Write the new snapshot of each changed note; lines that left and weren't seen again are gone.
            stamp = now.isoformat()
            for r in removed:
                if r["id"] not in reused:
                    conn.execute("UPDATE item_ids SET state = 'gone', updated_at = ? WHERE id = ?", (stamp, r["id"]))
            for note_id, lines, _, digest, _ in changed:
                for pos, ((text, done), iid) in enumerate(zip(lines, assigned[note_id])):
                    conn.execute("INSERT OR REPLACE INTO item_ids (id, note_id, text, done, pos, state, updated_at) "
                                 "VALUES (?, ?, ?, ?, ?, 'live', ?)", (iid, note_id, text, int(done), pos, stamp))
                conn.execute("INSERT INTO item_id_notes (note_id, digest, revision) VALUES (?, ?, 1) "
                             "ON CONFLICT(note_id) DO UPDATE SET digest = excluded.digest, revision = revision + 1",
                             (note_id, digest))
                out[note_id] = [i for i in assigned[note_id]]
        return out

    # --- the parts ---

    @staticmethod
    def _match(prev: List[Dict[str, Any]], lines: List[Tuple[str, bool]]):
        """Line diff of the last read against this one: (ids per line, None where new; new line indexes; indexes of
        old rows whose line left)."""
        a = [r["text"] for r in prev]
        b = [t for t, _ in lines]
        ids: List[Optional[str]] = [None] * len(lines)
        inserted: List[int] = []
        removed: List[int] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    ids[j1 + k] = prev[i1 + k]["id"]
            elif tag == "delete":
                removed += list(range(i1, i2))
            elif tag == "insert":
                inserted += list(range(j1, j2))
            else:  # replace: a reworded line keeps its id when the wording is close enough
                pairs = sorted(((difflib.SequenceMatcher(a=a[i].lower(), b=b[j].lower()).ratio(), i, j)
                                for i in range(i1, i2) for j in range(j1, j2)), reverse=True)
                used_i, used_j = set(), set()
                for ratio, i, j in pairs:
                    if ratio < REWORD_RATIO:
                        break
                    if i in used_i or j in used_j:
                        continue
                    ids[j] = prev[i]["id"]
                    used_i.add(i)
                    used_j.add(j)
                removed += [i for i in range(i1, i2) if i not in used_i]
                inserted += [j for j in range(j1, j2) if j not in used_j]
        return ids, inserted, removed

    def _seed(self, conn, note_id: str, lines: List[Tuple[str, bool]]) -> List[str]:
        """A note read for the first time: its items keep the ids they already had (the old hash of note, wording
        and open copy). A finished copy recorded done under the first open copy's id gets that id back, and
        the open copy a new one."""
        ids: List[Optional[str]] = [None] * len(lines)
        open_seen: Dict[str, int] = {}
        for j, (text, done) in enumerate(lines):
            if not done:
                n = open_seen.get(text, 0)
                open_seen[text] = n + 1
                ids[j] = item_id(note_id, text, n)
        ticked = {}
        for j, (text, done) in enumerate(lines):
            if done:
                ticked.setdefault(text, []).append(j)
        completed = None
        for text, where in ticked.items():
            first_open = next((j for j, (t, d) in enumerate(lines) if t == text and not d), None)
            if first_open is None:
                continue
            if completed is None:
                completed = self.db.completed_since(date(1970, 1, 1))
            if ids[first_open] in completed:
                ids[where[0]], ids[first_open] = ids[first_open], None
        for row in conn.execute("SELECT text, id FROM item_id_expected WHERE note_id = ?", (note_id,)).fetchall():
            # Thirties reworded, moved or added a line here before this note was ever read: that line is its item
            j = next((j for j, (t, _) in enumerate(lines) if _same(t, row["text"])), None)
            if j is not None and row["id"] not in ids and not self._live_elsewhere(conn, row["id"], {note_id}):
                ids[j] = row["id"]
                conn.execute("DELETE FROM item_id_expected WHERE id = ?", (row["id"],))
        taken = {i for i in ids if i}
        for j, (text, _) in enumerate(lines):
            if ids[j] is None or self._live_elsewhere(conn, ids[j], {note_id}):
                ids[j] = self._new_id(conn, note_id, text, taken)
                taken.add(ids[j])
        return ids  # type: ignore[return-value]

    @staticmethod
    def _taken(conn, iid: str) -> bool:
        return conn.execute("SELECT 1 FROM item_ids WHERE id = ? UNION ALL SELECT 1 FROM item_id_expected WHERE id = ?",
                            (iid, iid)).fetchone() is not None

    def _new_id(self, conn, note_id: str, text: str, taken: set) -> str:
        """The next free id for this wording in this note: the old hash with the first copy number never used."""
        k = 0
        while True:
            iid = item_id(note_id, text.strip(), k)
            if iid not in taken and not self._taken(conn, iid):
                return iid
            k += 1

    @staticmethod
    def _live(conn, iid: str) -> bool:
        return conn.execute("SELECT 1 FROM item_ids WHERE id = ? AND state = 'live'", (iid,)).fetchone() is not None

    @staticmethod
    def _live_elsewhere(conn, iid: str, notes_in_pass: set) -> bool:
        """The id belongs to a line in a note this pass isn't looking at (it can't be given to another line)."""
        row = conn.execute("SELECT note_id FROM item_ids WHERE id = ? AND state = 'live'", (iid,)).fetchone()
        return row is not None and row["note_id"] not in notes_in_pass

    @staticmethod
    def _expire(conn, now: datetime) -> None:
        conn.execute("DELETE FROM item_id_expected WHERE at < ?", ((now - EXPECT_FOR).isoformat(),))


def _table(db) -> Optional[ItemIds]:
    if db is None:
        return None
    try:
        return ItemIds(db)
    except Exception:  # a database that can't hold the table (a test double): the old hash
        return None


def line_id(db, note_id: str, text: str, done: Optional[bool] = None) -> str:
    """The id of the line worded so in this note, as of the last read; the old hash if Thirties hasn't seen it."""
    ids = _table(db)
    found = ids.find(note_id, text, done) if ids is not None else None
    return found or item_id(note_id, text.strip(), 0)


def line_ids(db, note_id: str, text: str) -> set:
    """Every id this wording has had in this note (and its old hash): for "is this record about that line?"."""
    ids = _table(db)
    return set(ids.find_all(note_id, text) if ids is not None else []) | {item_id(note_id, text.strip(), 0)}


def new_line_id(db, note_id: str, text: str) -> str:
    """The id of a line Thirties is adding (reserved, so the next read gives the line that id)."""
    ids = _table(db)
    return ids.reserve(note_id, text) if ids is not None else item_id(note_id, text.strip(), 0)


def keep_id(db, note_id: str, text: str, item: str) -> None:
    """Thirties is rewording or moving ``item`` so it reads ``text`` in this note: it keeps its id."""
    ids = _table(db)
    if ids is not None:
        ids.expect(note_id, text, item)


__all__ = ["ItemIds", "checklist_lines", "MOVE_WINDOW", "EXPECT_FOR", "line_id", "line_ids", "new_line_id",
           "keep_id"]
