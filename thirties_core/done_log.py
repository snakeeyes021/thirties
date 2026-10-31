"""Where done items go: one Done note per month, in a "Thirties" notebook.

Decided 2026-10-24 (ROADMAP "### Decided"): a done item moves out of its list into
the month's Done note as one dated line naming the list it came from
("- [x] 2026-10-30: Call the bank (Errands/Admin)"), so lists hold only what's
still to do (heavy principle 2: Thirties keeps the backend clean). Undo moves it
back, to the section it came from, open again. There are no per-list Done
sections: more places means more ways to fall out of step.

What stays in place:
- routine items (a routine note is a template; the day's ticks go on Today);
- the Today note's lines (it's cleared each morning);
- ongoing items: "worked on" doesn't tick them, so they're never filed; a
  "worked on" line can be added to the log with ``worked_on_op``;
- anything ticked before Thirties first read the note (years of old ticks are
  the user's history, not something to file in one go), sub-steps of an item
  that's still open, and items under the user's own Done headings.

Filing happens a while after the tick (FILE_AFTER), not at once: a mis-tap in
Joplin can be undone by unticking it, and ticks made by hand in Joplin are filed
too. Unticking a line in the Done note by hand moves the item back to its list.
Every move is one logged Joplin write through an exact link (the item's own
wording and which copy it is, in a known note).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX, item_id
from thirties_core.note_edits import HeadingTrail, item_end
from thirties_core.scheduler import utc_now_iso

logger = logging.getLogger(__name__)

DONE_NOTEBOOK = "Thirties"
DONE_NOTE_TITLE = "Done {month}"   # month as 2026-10: the notes sort in order
# Where done items are filed from. Not a menu: its options are picked again and again ("go outside"), so a tick
# there isn't "finished for good" (decision; revisit with item kinds, B2).
FILED_ROLES = ("list", "project", "backlog", "dropbox")
FILE_AFTER = timedelta(minutes=30)  # a tick stands this long before it's filed (time to take a mis-tap back)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS done_filed (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_note_id TEXT NOT NULL,
    source_title TEXT NOT NULL DEFAULT '',
    section TEXT,
    block TEXT NOT NULL DEFAULT '[]',
    line TEXT NOT NULL,
    month TEXT NOT NULL,
    day TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'filed',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS done_watch (
    note_id TEXT PRIMARY KEY,
    open_items TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS done_seen (
    note_id TEXT NOT NULL,
    text TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (note_id, text)
);
"""


def done_note_title(month: str) -> str:
    return DONE_NOTE_TITLE.format(month=month)


def done_line(day: date, title: str, list_title: str) -> str:
    """The Done note's line for an item, without its checkbox."""
    return f"{day.isoformat()}: {title.strip()} ({list_title.strip()})"


def worked_line(day: date, title: str, list_title: str) -> str:
    return f"{day.isoformat()}: worked on {title.strip()} ({list_title.strip()})"


def filed_key(note_id: str, title: str, day: str) -> str:
    return hashlib.sha256(f"{note_id}|{title.strip().lower()}|{day}".encode("utf-8")).hexdigest()[:16]


# Two devices can each make the month's Done note before Joplin syncs the other's in. The newer copy's lines go
# into the older note (date order); the newer one is emptied, renamed with this suffix and left with a pointer,
# rather than deleted: Joplin's DELETE moves a note to its trash only from 2.15 on (older versions delete it for
# good), and a device that hasn't synced the merge yet may still write to it. A renamed copy that gains lines
# that way is merged again on the next look.
MERGED_SUFFIX = " (merged)"
_DATED = re.compile(r"^- \[[ xX]\] (\d{4}-\d{2}-\d{2})")


def _blocks(body: str) -> tuple:
    """(lines before the first item, [item blocks: a top-level line and what's nested under it])."""
    head: List[str] = []
    blocks: List[List[str]] = []
    for line in (body or "").splitlines():
        if (CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)) and not line[:1].isspace():
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            head.append(line)
    return head, blocks


def merge_done_bodies(older: str, newer: str) -> str:
    """The older note's lines and the newer's together, in date order (an item in both is kept once; an undated
    line keeps its place after the dated line before it)."""
    head, mine = _blocks(older)
    _, theirs = _blocks(newer)
    seen = {tuple(b) for b in mine}
    combined = list(mine)
    for block in theirs:
        if tuple(block) not in seen:
            seen.add(tuple(block))
            combined.append(block)
    keyed, last = [], ""
    for n, block in enumerate(combined):
        m = _DATED.match(block[0])
        last = m.group(1) if m else last
        keyed.append((last, n, block))
    lines = head + [line for _, _, block in sorted(keyed, key=lambda k: (k[0], k[1])) for line in block]
    return "\n".join(lines) + ("\n" if lines else "")


def _merge_duplicates(api, db, title: str, notes: List[Dict[str, Any]]) -> Optional[str]:
    """Several notes for one month (and merged copies that gained lines since): everything goes into the oldest
    live one. Returns its id, or None if that couldn't be done (and nothing more was changed). Each write is
    logged like every other Joplin write."""
    from thirties_core.joplin_writeback import WriteLog
    full = []
    for n in notes:
        got = api.get_note(n["id"], fields="id,title,body,created_time")
        if got:
            full.append({**n, **got})
    live = sorted((n for n in full if n.get("title") == title), key=lambda n: (n.get("created_time") or 0, n["id"]))
    if not live:
        return None
    keep = live[0]
    extra = live[1:] + [n for n in full if n.get("title") == title + MERGED_SUFFIX and _blocks(n.get("body") or "")[1]]
    log = WriteLog(db)
    body = keep.get("body") or ""
    for other in extra:
        merged = merge_done_bodies(body, other.get("body") or "")
        op = {"op": "merge_done_notes", "into": keep["id"], "from": other["id"], "title": title}
        if merged != body:
            ok = api.update_note(keep["id"], {"body": merged})
            log.record(op, title, ok, "the other copy's lines, in date order", body, merged, note_id=keep["id"])
            if not ok:
                return None
            body = merged
        pointer = (f"Merged into the older \"{title}\" note (two devices each made one before syncing). Nothing is "
                   f"left here; this note can be deleted.\n")
        ok = api.update_note(other["id"], {"title": title + MERGED_SUFFIX, "body": pointer})
        log.record(op, other.get("title") or title, ok, "emptied, pointing at the older note",
                   other.get("body") or "", pointer, note_id=other["id"])
        logger.info("Merged the duplicate Done note %s into %s", other["id"], keep["id"])
    return keep["id"]


def done_note_id(api, db, month: str, create: bool = True) -> Optional[str]:
    """The id of the month's Done note, made (with the Thirties notebook) the first time it's needed. Joplin must
    be up. The note is marked "ignore", so it's never read as a list. It's looked for by title before one is made,
    and when Joplin has synced in a second one (two devices each made it), they're merged into the older one."""
    key = f"done_note:{month}"
    cached = db.get_sync_meta(key)
    title = done_note_title(month)
    cached_note = api.get_note(cached, fields="id,title") if cached else None
    if cached_note and cached_note.get("title") not in (None, title):
        cached_note = None  # merged away (renamed) on another device: find the one it went into
    folders = [f for f in api.get_folders() if f.get("title") == DONE_NOTEBOOK and not f.get("parent_id")]
    candidates = [n for f in folders for n in api.get_folder_notes(f["id"])
                  if n.get("title") in (title, title + MERGED_SUFFIX)]
    note_id = None
    if len(candidates) > 1:
        note_id = _merge_duplicates(api, db, title, candidates)
    elif candidates and candidates[0].get("title") == title:
        note_id = candidates[0]["id"]
    if note_id is None and cached_note:
        note_id = cached  # the folder listing came back short: never make a second note over it
    if note_id is None:
        if not create:
            return None
        folder = folders[0] if folders else api.create_folder(DONE_NOTEBOOK)
        if not folder or "id" not in folder:
            return None
        note = api.create_note(title, "", parent_id=folder["id"])
        if not note or "id" not in note:
            return None
        note_id = note["id"]
    if note_id != cached:
        db.set_sync_meta(key, note_id)
    try:
        from thirties_core.lists import ListCatalog
        catalog = ListCatalog(db)
        for n in candidates or [{"id": note_id, "title": title}]:
            if catalog.role(n["id"]) != "ignore":
                catalog.set_role(n["id"], "ignore", n.get("title") or title, source="user")
    except Exception as e:
        logger.warning("Couldn't mark the Done note as not a list: %s", e)
    return note_id


def file_done_op(note_id: str, title: str, list_title: str, day: date, occurrence: int = 0) -> Dict[str, Any]:
    return {"op": "file_done", "note_id": note_id, "title": title, "occurrence": occurrence,
            "list_title": list_title, "day": day.isoformat(), "month": day.strftime("%Y-%m"),
            "key": filed_key(note_id, title, day.isoformat())}


def worked_on_op(title: str, list_title: str, day: date) -> Dict[str, Any]:
    """A "worked on" line for an ongoing item, which stays on its list (B2)."""
    return {"op": "worked_on", "title": title, "line": worked_line(day, title, list_title),
            "month": day.strftime("%Y-%m")}


def ticked_to_file(body: str) -> List[tuple]:
    """Ticked items that can be filed: (text, which ticked copy of that text it is). Only top-level ones, with
    nothing still open under them (a sub-step left to do keeps the item where it is); never what's under the
    user's own Done heading (sub-headings too); and only a wording ticked once in the note, since which of two
    ticked copies is the new one can't be told (an old tick would be filed in its place)."""
    lines = (body or "").splitlines()
    trail = HeadingTrail()
    under_done = False
    counts: Dict[str, int] = {}
    found = []
    for i, line in enumerate(lines):
        if HEADING_REGEX.match(line):
            under_done = trail.enter(line)[1]
            continue
        d = DONE_CHECKBOX_REGEX.match(line)
        if not d:
            continue
        text = d.group(2).strip()
        n = counts.get(text.lower(), 0)
        counts[text.lower()] = n + 1
        if d.group(1) or under_done:
            continue
        if any(CHECKBOX_REGEX.match(x) for x in lines[i + 1:item_end(lines, i)]):
            continue
        found.append((text, n))
    return [(text, n) for text, n in found if counts[text.lower()] == 1]


class DoneLog:
    """What Thirties filed (for undo, synced), and what it has seen of each list (local)."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    # --- what was filed ---

    def save_filed(self, key: str, title: str, source_note_id: str, source_title: str, section: Optional[str],
                   block: List[str], line: str, month: str, day: str, state: str = "filed") -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO done_filed (key, title, source_note_id, source_title, section, block, "
                         "line, month, day, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (key, title, source_note_id, source_title, section, json.dumps(block), line, month, day,
                          state, utc_now_iso()))

    def set_state(self, key: str, state: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("UPDATE done_filed SET state = ?, updated_at = ? WHERE key = ?", (state, utc_now_iso(), key))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM done_filed WHERE key = ?", (key,)).fetchone()
        return self._row(row) if row else None

    def filed(self, since_month: str = "") -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM done_filed WHERE state = 'filed' AND month >= ? ORDER BY day DESC, "
                                "updated_at DESC", (since_month,)).fetchall()
        return [self._row(r) for r in rows]

    def latest(self, title: str) -> Optional[Dict[str, Any]]:
        """The most recently filed item with this wording."""
        want = title.strip().lower()
        return next((r for r in self.filed() if r["title"].strip().lower() == want), None)

    @staticmethod
    def _row(row) -> Dict[str, Any]:
        d = dict(row)
        d["block"] = json.loads(d.get("block") or "[]")
        return d

    # sync: last write wins, so an undo on one device holds on the others
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM done_filed").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        cols = ["key", "title", "source_note_id", "source_title", "section", "block", "line", "month", "day", "state",
                "updated_at"]
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM done_filed WHERE key = ?", (record["key"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO done_filed ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [record.get(c) for c in cols])
        return True

    # --- what's been seen of each list ---

    def snapshot(self, note_id: str) -> Optional[set]:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT open_items FROM done_watch WHERE note_id = ?", (note_id,)).fetchone()
        return set(json.loads(row["open_items"])) if row else None

    def save_snapshot(self, note_id: str, open_items: set) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO done_watch (note_id, open_items, updated_at) VALUES (?, ?, ?)",
                         (note_id, json.dumps(sorted(open_items)), utc_now_iso()))

    def first_seen(self, note_id: str, text: str, now: datetime, day: date) -> tuple:
        """When this tick was first seen (and the day it counts for), noting it now if it's new."""
        key = text.strip().lower()
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT first_seen, day FROM done_seen WHERE note_id = ? AND text = ?",
                               (note_id, key)).fetchone()
            if row:
                return datetime.fromisoformat(row["first_seen"]), date.fromisoformat(row["day"])
            conn.execute("INSERT INTO done_seen (note_id, text, first_seen, day) VALUES (?, ?, ?, ?)",
                         (note_id, key, now.isoformat(), day.isoformat()))
        return now, day

    def seen_ticked(self, note_id: str) -> set:
        with self.db._get_connection() as conn:
            return {r["text"] for r in conn.execute("SELECT text FROM done_seen WHERE note_id = ?", (note_id,))}

    def forget_seen(self, note_id: str, text: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("DELETE FROM done_seen WHERE note_id = ? AND text = ?", (note_id, text.strip().lower()))

    # --- filing ---

    def sweep(self, assistant) -> List[str]:
        """File what's been done since the last look: ticked items in lists, a while after the tick. Lines unticked
        by hand in the Done note go back to their lists. Returns what moved, for the log. Runs while Joplin is up
        (every couple of minutes, from the background service)."""
        from thirties_core.lists import ListCatalog
        writer = assistant._writer()
        if writer is None or not writer.allowed or not getattr(writer.cfg, "file_done", True):
            return []
        if not assistant._joplin_up():
            return []
        roles = ListCatalog(self.db).roles()
        now, today = assistant.pt._now(), assistant._today()
        completed = self.db.completed_since(today - timedelta(days=2))
        moved: List[str] = []
        for note in assistant._joplin_notes():
            role = (roles.get(note["id"]) or {}).get("role")
            if role not in FILED_ROLES:
                continue
            body = note.get("body") or ""
            open_now = {m.group(2).strip().lower() for m in map(CHECKBOX_REGEX.match, body.splitlines()) if m}
            before = self.snapshot(note["id"])
            pending = self.seen_ticked(note["id"])
            for text in pending & open_now:  # unticked again before it was filed
                self.forget_seen(note["id"], text)
            for text, n in ticked_to_file(body):
                low = text.lower()
                ours = any(item_id(note["id"], text, k) in completed for k in range(3))
                if not (low in pending or ours or (before is not None and low in before)):
                    continue  # ticked before Thirties ever saw it open: history, not news
                if self._daily(assistant, text):
                    continue  # done means done today: it stays on its list for tomorrow (B2)
                seen, day = self.first_seen(note["id"], text, now, today)
                if now - seen < FILE_AFTER:
                    continue
                status, msg = writer.submit(file_done_op(note["id"], text, note.get("title", ""), day, n))
                if status in ("done", "queued"):
                    self.forget_seen(note["id"], text)
                    moved.append(f"filed {text} from {note.get('title', '')}")
                elif status == "failed":
                    self.forget_seen(note["id"], text)  # it's gone from the list: nothing to file
            self.save_snapshot(note["id"], open_now | self.seen_ticked(note["id"]))
        moved += self._unticked_by_hand(writer)
        return moved

    @staticmethod
    def _daily(assistant, text: str) -> bool:
        """A daily item (B2: its kind, from what's known about it) is never filed."""
        kind_of = getattr(assistant, "kind_of", None)
        try:
            return kind_of is not None and kind_of(text) == "daily"
        except Exception:
            return False

    def _unticked_by_hand(self, writer) -> List[str]:
        """A line unticked in the Done note means "not done after all": the item goes back to its list."""
        out = []
        months = sorted({r["month"] for r in self.filed()}, reverse=True)[:2]
        for month in months:
            note_id = self.db.get_sync_meta(f"done_note:{month}")
            note = writer.api.get_note(note_id, fields="id,title,body") if note_id else None
            if not note:
                continue
            open_lines: Dict[str, int] = {}  # each open line puts back one item (two lists can file the same line)
            for m in map(CHECKBOX_REGEX.match, (note.get("body") or "").splitlines()):
                if m:
                    open_lines[m.group(2).strip()] = open_lines.get(m.group(2).strip(), 0) + 1
            for rec in self.filed(month):
                if rec["month"] == month and open_lines.get(rec["line"], 0) > 0:
                    open_lines[rec["line"]] -= 1
                    status, _ = writer.submit({"op": "unfile_done", "key": rec["key"], "title": rec["title"],
                                               "note_id": rec["source_note_id"]})
                    if status in ("done", "queued"):
                        out.append(f"put {rec['title']} back on {rec['source_title']}")
        return out


def clear_completed(db, task_id: str) -> None:
    """Thirties' own done record for an item is taken back (an undo), so the item is offered again."""
    with db._get_connection() as conn:
        conn.execute("UPDATE task_history SET completed_date = NULL WHERE task_id = ?", (task_id,))


__all__ = ["DoneLog", "MERGED_SUFFIX", "merge_done_bodies", "DONE_NOTEBOOK", "DONE_NOTE_TITLE", "FILED_ROLES",
           "FILE_AFTER", "done_note_id", "done_line",
           "file_done_op", "worked_on_op", "ticked_to_file", "clear_completed", "filed_key"]
