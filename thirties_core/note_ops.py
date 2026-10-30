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

from typing import Any, Dict, List, Optional, Tuple

from thirties_core import note_edits as ne


def _headings(body: str) -> List[str]:
    return [s.name for s in ne.outline(body) if s.name]


def apply(writer, op: Dict[str, Any]) -> Tuple[bool, str]:
    kind = op["op"]
    occ = int(op.get("occurrence") or 0)
    if kind == "rename":
        return writer._edit_body(op["note_id"], lambda b: ne.rename_item(b, op["title"], op["new_title"], occ))
    if kind == "split":
        return writer._edit_body(op["note_id"], lambda b: ne.split_item(b, op["title"], op["parts"], occ))
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
    return False, f"gone: unknown operation {kind!r}"


def _move(writer, op: Dict[str, Any], occ: int) -> Tuple[bool, str]:
    target = op.get("to_note_id") or op["note_id"]
    section = op.get("section")
    if target == op["note_id"]:
        return writer._edit_body(target, lambda b: ne.move_within(b, op["title"], section, occ))
    held: Dict[str, Any] = {}

    def take(body: str) -> Optional[str]:
        taken = ne.take_item(body, op["title"], occ, ticked_too=bool(op.get("ticked_too")))
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
        for item in op["items"]:
            taken = ne.take_item(body, item["title"], int(item.get("occurrence") or 0), ticked_too=False)
            if taken is None:
                return None
            body = taken[0]
            blocks.extend(taken[1])
        return body
    if op.get("to_note_id"):
        return writer._edit_bodies([(op["note_id"], take_all),
                                    (op["to_note_id"], lambda b: ne.insert_lines(b, blocks, op.get("section")))])
    # A new note, beside the old one: read the old note, make the new one with the items, then take them out.
    source = writer.api.get_note(op["note_id"], fields="id,title,body,parent_id")
    if not source or "body" not in source:
        return (False, "gone: the note isn't in Joplin any more.") if writer.api.ping() else \
            (False, "Couldn't read the note from Joplin.")
    rest = take_all(source["body"])
    if rest is None:
        return False, "gone: an item isn't in the note any more (it was changed in Joplin)."
    body = "\n".join(blocks) + "\n"
    made = writer.api.create_note(op["new_title"], body, parent_id=source.get("parent_id") or None)
    if not made or "id" not in made:
        return False, "Joplin didn't make the new note."
    writer._edits.append((made["id"], op["new_title"], "", body))
    if not writer.api.update_note(op["note_id"], {"body": rest}):
        return False, "Joplin made the new note but didn't take the items out of the old one."
    writer._edits.append((op["note_id"], source.get("title", ""), source["body"], rest))
    return True, f"note:{made['id']}"


def _done(writer, op: Dict[str, Any], occ: int) -> Tuple[bool, str]:
    from thirties_core.done_log import DoneLog, done_line, done_note_id
    from datetime import date
    log = DoneLog(writer.db)
    if op["op"] == "unfile_done":
        rec = log.get(op["key"])
        if rec is None:
            return False, "gone: Thirties has no record of filing it."
        done_id = done_note_id(writer.api, writer.db, rec["month"], create=False)
        if done_id is None:
            return False, "gone: the Done note isn't in Joplin any more."

        def take_line(body: str) -> Optional[str]:
            taken = ne.take_item(body, rec["line"], 0)
            return taken[0] if taken else None

        def put_back(body: str) -> str:
            section = rec["section"] if rec["section"] in ([""] + _headings(body)) else None
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
    held: Dict[str, Any] = {}

    def take(body: str) -> Optional[str]:
        taken = ne.take_item(body, op["title"], occ, ticked_only=True)
        if taken is None:
            return None
        held["block"], held["section"] = taken[1], taken[2]
        return taken[0]
    ok, msg = writer._edit_bodies([(op["note_id"], take),
                                   (done_id, lambda b: ne.insert_lines(b, [f"- [x] {line}"], None))])
    if ok:
        log.save_filed(op["key"], op["title"], op["note_id"], op.get("list_title") or "", held.get("section"),
                       held.get("block") or [], line, op["month"], op["day"])
    return ok, msg


__all__ = ["apply"]
