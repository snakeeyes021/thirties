"""Applying structure changes to Joplin: the operations JoplinWriter hands over (STRUCTURE_OPS).

Each operation names its item exactly (its note, its wording, which copy) and is
worked out in full before anything is written (JoplinWriter._edit_bodies), so an
item that was changed by hand in the meantime changes nothing. The writer logs
every note each one touches.

- rename, split, untick: one line of one note.
- move: an item (with what's nested under it) to another section, or another note.
- move_many: several items of one note into another note, made beside it if it's new
  (triage of a list sorted by time: "Thirties writes it").
- file_done / unfile_done / worked_on: the monthly Done note (done_log.py).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from thirties_core import note_edits as ne


def _headings(body: str) -> List[str]:
    return [s.name for s in ne.outline(body) if s.name]


def apply(writer, op: Dict[str, Any]) -> Tuple[bool, str]:
    kind = op["op"]
    occ = int(op.get("occurrence") or 0)
    if kind == "rename":
        return writer._edit_body(op["note_id"], lambda b: ne.rename_item(b, op["title"], op["new_title"], occ,
                                                                         open_only=bool(op.get("open_only"))))
    if kind == "split":
        return writer._edit_body(op["note_id"], lambda b: ne.split_item(b, op["title"], op["parts"], occ,
                                                                        open_only=bool(op.get("open_only"))))
    if kind == "untick":
        def untick(body: str) -> Optional[str]:
            lines = body.splitlines()
            i = ne.find_item(lines, op["title"], occ, ticked_only=True)
            if i is None:
                return None
            lines[i:i + 1] = ne.untick([lines[i]])
            return "\n".join(lines) + ("\n" if body.endswith("\n") else "")
        return writer._edit_body(op["note_id"], untick)
    if kind == "move":
        return _move(writer, op, occ)
    if kind == "move_many":
        return _move_many(writer, op)
    if kind in ("file_done", "unfile_done", "worked_on"):
        return _done(writer, op, occ)
    if kind in ("file_dropped", "unfile_dropped"):
        return _dropped(writer, op, occ)
    if kind == "create_list":
        return _create_list(writer, op)
    return False, f"gone: unknown operation {kind!r}"


def create_list_op(title: str, notebook: str, items: List[str]) -> Dict[str, Any]:
    """A new list: a note called ``title`` in the notebook named ``notebook`` ("Tasks" or "Tasks > Home"),
    holding ``items`` as open lines."""
    return {"op": "create_list", "title": title.strip(), "notebook": notebook.strip(),
            "items": [str(x).strip() for x in items if str(x).strip()]}


def _folder_paths(folders: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_id = {f["id"]: f for f in folders if f.get("id")}

    def path(f: Dict[str, Any], seen=()) -> str:
        parent = by_id.get(f.get("parent_id") or "")
        if parent is None or parent["id"] in seen:
            return f.get("title") or ""
        return path(parent, seen + (f["id"],)) + " > " + (f.get("title") or "")
    return {path(f).strip().lower(): f for f in by_id.values()}


def _create_list(writer, op: Dict[str, Any]) -> Tuple[bool, str]:
    """Make the note, once: only in a notebook Thirties reads (else it would never be seen), never over a note
    of the same name there, and never twice (a retry after Joplin went away finds the one it made)."""
    if op.get("_made"):
        got = writer.api.get_note(op["_made"], fields="id,title")
        if got and got.get("id"):
            return True, f"note:{op['_made']}"
    paths = _folder_paths(writer.api.get_folders() or [])
    want = re.sub(r"\s*>\s*", " > ", op.get("notebook") or "").strip().lower()
    folder = paths.get(want)
    if folder is None:
        tops = [f for p, f in paths.items() if p.split(" > ")[-1] == want]
        folder = tops[0] if len(tops) == 1 else None
    if folder is None:
        return False, f"gone: there's no notebook called '{op.get('notebook')}' in Joplin."
    path = next(p for p, f in paths.items() if f["id"] == folder["id"])
    cfg = writer.cfg
    top = path.split(" > ")[0]
    excluded = any(path == nb.lower() or path.startswith(nb.lower() + " >") for nb in (cfg.excluded_notebooks or []))
    allowed = not cfg.allowed_notebooks or any(path == nb.lower() or path.startswith(nb.lower() + " >")
                                               for nb in cfg.allowed_notebooks)
    if excluded or not allowed:
        return False, (f"gone: Thirties doesn't read the notebook '{op.get('notebook')}', so a list there would never "
                       f"be seen. Pick one it reads, or add {top!r} in Preferences → Tasks.")
    same = [n for n in writer.api.get_folder_notes(folder["id"]) or []
            if re.sub(r"\s+", " ", n.get("title") or "").strip().lower() == op["title"].lower()]
    if same:
        return False, f"gone: there's already a note called '{op['title']}' there."
    body = "".join(f"- [ ] {x}\n" for x in op.get("items") or [])
    made = writer.api.create_note(op["title"], body, parent_id=folder["id"])
    if not made or "id" not in made:
        return False, "Joplin didn't make the new note."
    op["_made"] = made["id"]
    writer._edits.append((made["id"], op["title"], "", body))
    return True, f"note:{made['id']}"


def _move(writer, op: Dict[str, Any], occ: int) -> Tuple[bool, str]:
    target = op.get("to_note_id") or op["note_id"]
    section = op.get("section")
    open_only = bool(op.get("open_only"))  # an open list item: never an old ticked line with the same wording
    if target == op["note_id"]:
        return writer._edit_body(target, lambda b: ne.move_within(b, op["title"], section, occ, open_only=open_only))
    held: Dict[str, Any] = op.setdefault("_taken", {})  # kept on the op: a retry may find the change already made

    def take(body: str) -> Optional[str]:
        taken = ne.take_item(body, op["title"], occ, ticked_too=bool(op.get("ticked_too")), open_only=open_only)
        if taken is None:
            return None
        held["block"] = taken[1]
        return taken[0]
    return writer._edit_bodies([(op["note_id"], take),
                                (target, lambda b: ne.insert_lines(b, held["block"], section))])


def _move_many(writer, op: Dict[str, Any]) -> Tuple[bool, str]:
    """Several items out of one note into another (one that exists, or a new one made in the same notebook)."""
    blocks: List[str] = []

    def take_all(body: str) -> Optional[str]:
        # The later copies of a wording first, so taking one doesn't renumber the others; written in the given order.
        items = op["items"]
        order = sorted(range(len(items)), key=lambda k: -int(items[k].get("occurrence") or 0))
        taken_blocks: Dict[int, List[str]] = {}
        for k in order:
            taken = ne.take_item(body, items[k]["title"], int(items[k].get("occurrence") or 0), ticked_too=False,
                                 open_only=True)
            if taken is None:
                return None
            body, taken_blocks[k] = taken[0], taken[1]
        blocks.clear()
        for k in range(len(items)):
            blocks.extend(taken_blocks[k])
        return body
    if op.get("to_note_id"):
        return writer._edit_bodies([(op["note_id"], take_all),
                                    (op["to_note_id"], lambda b: ne.insert_lines(b, blocks, op.get("section")))])
    # A new note, beside the old one: read the old note, make the new one with the items, then take them out.
    source = writer.api.get_note(op["note_id"], fields="id,title,body,parent_id,updated_time")
    if not source or "body" not in source:
        return (False, "gone: the note isn't in Joplin any more.") if writer.api.ping() else \
            (False, "Couldn't read the note from Joplin.")
    rest = take_all(source["body"])
    if rest is None:
        return False, "gone: an item isn't in the note any more (it was changed in Joplin)."
    still = writer._unchanged(source)  # never write over an edit made meanwhile
    if not still:
        return False, ("The note changed while Thirties wrote to it; it'll try again." if still is False
                       else "Couldn't read the note from Joplin.")
    body = "\n".join(blocks) + "\n"
    made = writer.api.create_note(op["new_title"], body, parent_id=source.get("parent_id") or None)
    if not made or "id" not in made:
        return False, "Joplin didn't make the new note."
    writer._edits.append((made["id"], op["new_title"], "", body))
    if writer._put(op["note_id"], source["body"], rest) != "landed":
        # Parked, never retried: a retry would make a second new note. The log shows the half-done change.
        return False, "gone: half done (the new note was made, but the items couldn't be taken out of the old one)."

    writer._edits.append((op["note_id"], source.get("title", ""), source["body"], rest))
    return True, f"note:{made['id']}"


def _done(writer, op: Dict[str, Any], occ: int) -> Tuple[bool, str]:
    from thirties_core.done_log import DoneLog, done_line, done_note_id, done_note_ids
    from datetime import date
    log = DoneLog(writer.db)
    if op["op"] == "unfile_done":
        rec = log.get(op["key"])
        if rec is None:
            return False, "gone: Thirties has no record of filing it."
        places = done_note_ids(writer.api, writer.db, rec["month"])  # the chosen note, then the month's
        if not places:
            return False, "gone: the Done note isn't in Joplin any more."
        done_id = places[0]
        for nid in places:  # the one that holds its line (the chosen note may have changed since it was filed)
            got = writer.api.get_note(nid, fields="id,body")
            if got and ne.take_item(got.get("body") or "", rec["line"], 0):
                done_id = nid
                break

        def take_line(body: str) -> Optional[str]:
            taken = ne.take_item(body, rec["line"], 0)
            return taken[0] if taken else None

        def put_back(body: str) -> str:
            heads = _headings(body)  # its old section, else the top: never after a later someday section
            section = rec["section"] if rec["section"] in ([""] + heads) else ("" if heads else None)
            return ne.insert_lines(body, ne.untick(rec["block"] or [f"- [x] {rec['title']}"]), section)
        ok, msg = writer._edit_bodies([(done_id, take_line), (rec["source_note_id"], put_back)])
        if ok:
            log.set_state(rec["key"], "undone")
        return ok, msg
    done_id = done_note_id(writer.api, writer.db, op["month"])
    if done_id is None:
        return False, "Couldn't make the Done note in Joplin."
    if op["op"] == "worked_on":
        return writer._edit_body(done_id, lambda b: ne.insert_lines(b, [f"- [x] {op['line']}"], None))
    line = done_line(date.fromisoformat(op["day"]), op["title"], op.get("list_title") or "")
    held: Dict[str, Any] = op.setdefault("_taken", {})  # kept on the op: a retry may find the change already made

    def take(body: str) -> Optional[str]:
        taken = ne.take_item(body, op["title"], occ, ticked_only=True)
        if taken is None:
            return None
        held["block"], held["section"] = taken[1], taken[2]
        return taken[0]

    def file(body: str) -> str:  # the dated line, with what was nested under the item kept under it
        return ne.insert_lines(body, [f"- [x] {line}"] + held["block"][1:], None)  # (already indented under it)
    ok, msg = writer._edit_bodies([(op["note_id"], take), (done_id, file)])
    if ok:
        log.save_filed(op["key"], op["title"], op["note_id"], op.get("list_title") or "", held.get("section"),
                       held.get("block") or [], line, op["month"], op["day"])
    return ok, msg


def _anchor(body: str, text: str, occ: int) -> str:
    """The wording of the item just before this one, at the same depth (where to put it back)."""
    lines = body.splitlines()
    i = ne.find_item(lines, text, occ, ticked_too=False, open_only=True)
    if i is None:
        return ""
    m = ne._match(lines[i])
    width = ne._width(m.group(1)) if m else 0
    for j in range(i - 1, -1, -1):
        if ne.HEADING_REGEX.match(lines[j]):
            return FIRST  # the first item of its section
        p = ne._match(lines[j])
        if p and ne._width(p.group(1)) == width:
            return p.group(2).strip()
    return FIRST


FIRST = "^"  # an anchor meaning "first in its section"


def _put_after(body: str, block: List[str], anchor: str, section: Optional[str]) -> str:
    """The block right after the item worded ``anchor`` if it's still there, else at its section's end."""
    lines = body.splitlines()
    if anchor == FIRST:
        heads = [k for k, line in enumerate(lines) if ne.HEADING_REGEX.match(line)
                 and ne.HEADING_REGEX.match(line).group(1).strip() == (section or "")]
        at = heads[0] + 1 if heads else (0 if not section else None)
        if at is not None:
            lines[at:at] = block
            return "\n".join(lines) + ("\n" if body.endswith("\n") or not body else "")
    i = ne.find_item(lines, anchor, 0) if anchor and anchor != FIRST else None
    if i is None:
        heads = _headings(body)
        sec = section if section in ([""] + heads) else ("" if heads else None)
        return ne.insert_lines(body, block, sec)
    j = ne.item_end(lines, i)
    lines[j:j] = block
    return "\n".join(lines) + ("\n" if body.endswith("\n") or not body else "")


def _dropped(writer, op: Dict[str, Any], occ: int) -> Tuple[bool, str]:
    """A dropped item, filed like a done one (done_log.DROPPED_NOTE_TITLE), or put back where it was."""
    from datetime import date
    from thirties_core.done_log import DROPPED_NOTE_TITLE, DoneLog, done_note_id, dropped_line
    log = DoneLog(writer.db)
    if op["op"] == "unfile_dropped":
        rec = log.get(op["key"])
        if rec is None:
            return False, "gone: Thirties has no record of dropping it."
        note_id = done_note_id(writer.api, writer.db, rec["month"], create=False, title_tpl=DROPPED_NOTE_TITLE,
                               key_prefix="dropped_note")
        if note_id is None:
            return False, "gone: the Dropped note isn't in Joplin any more."
        anchor = writer.db.get_sync_meta(f"dropped_anchor:{rec['key']}") or ""

        def take_line(body: str) -> Optional[str]:
            taken = ne.take_item(body, rec["line"], 0)
            return taken[0] if taken else None

        def put_back(body: str) -> str:
            return _put_after(body, ne.untick(rec["block"] or [f"- [x] {rec['title']}"]), anchor, rec["section"])
        ok, msg = writer._edit_bodies([(note_id, take_line), (rec["source_note_id"], put_back)])
        if ok:
            log.set_state(rec["key"], "undropped")
        return ok, msg
    note_id = done_note_id(writer.api, writer.db, op["month"], title_tpl=DROPPED_NOTE_TITLE, key_prefix="dropped_note")
    if note_id is None:
        return False, "Couldn't make the Dropped note in Joplin."
    line = dropped_line(date.fromisoformat(op["day"]), op["title"], op.get("list_title") or "", op.get("why") or "")
    held: Dict[str, Any] = op.setdefault("_taken", {})  # kept on the op: a retry may find the change already made

    def take(body: str) -> Optional[str]:
        held["anchor"] = _anchor(body, op["title"], occ)
        if op.get("ticked"):  # ticked by the user with a drop word on it (list_reading.py): the ticked line moves
            taken = ne.take_item(body, op["title"], occ, ticked_only=True)
        else:
            taken = ne.take_item(body, op["title"], occ, ticked_too=False, open_only=True)
        if taken is None:
            return None
        held["block"], held["section"] = taken[1], taken[2]
        return taken[0]

    def file(body: str) -> str:  # ticked, dated, naming its list and why; what was nested under it stays under it
        return ne.insert_lines(body, [f"- [x] {line}"] + held["block"][1:], None)
    ok, msg = writer._edit_bodies([(op["note_id"], take), (note_id, file)])
    if ok:
        log.save_filed(op["key"], op["title"], op["note_id"], op.get("list_title") or "", held.get("section"),
                       held.get("block") or [], line, op["month"], op["day"], state="dropped")
        writer.db.set_sync_meta(f"dropped_anchor:{op['key']}", held.get("anchor") or "")
    return ok, msg


__all__ = ["apply"]
