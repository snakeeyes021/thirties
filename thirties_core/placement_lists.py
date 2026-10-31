"""Where a new item goes: which list, and which section of it (B1, J5).

The model brings the judgement, Python everything else (AGENTS.md §7):
- Python lists the candidate notes and their sections, with their roles and what's
  in them, ranked by how much they share with the item;
- the model picks one with a reason (it sees the whole backlog anyway);
- Python refuses what's obviously wrong: a routine (a template), a reference or
  ignored note, today's list (that's add_to_today), a Done heading, a section that
  isn't there, and a someday section for a dated or "now" item (10-24: "Set an item
  for tomorrow" landed under Probation, because a line with no section went after
  the note's last item, which was in Probation);
- with no section given, Python picks the obvious one: never a someday or Done
  section, the one whose items are most like the new one, else the first.

It never asks the user where something goes (10-27: "It should either put it there
or it shouldn't"). When no list is obvious, the **drop box** takes it (soak 5, the
user's "Assorted Quickies"): a quick place to drop things without deciding, for one
notebook or for all of them. Thirties offers now and then to empty it, proposing a
home for each item, one small confirmation each.

This module also spots lists sorted by time ("This week / Later / Someday") rather
than by area, for the one-time triage conversation (After 0.1, item 8).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from thirties_core.note_edits import Section, outline
from thirties_core.scheduler import utc_now_iso

PLACEABLE_ROLES = ("list", "project", "menu", "backlog", "dropbox")
REFUSED_ROLES = {
    "routine": "is a routine, a template of steps done every time; it only changes when the user asks to change the "
               "routine itself",
    "reference": "is a reference list (never pushed); things to do don't go there",
    "ignore": "isn't a to-do list (the user set Thirties to leave it alone)",
    "today": "is today's list: put things on it with add_to_today",
}
DROPBOX_REF = re.compile(r"^\s*(?:the\s+|my\s+)?(?:drop\s*-?box|inbox|grab\s*-?bag)\s*$", re.IGNORECASE)

DROPBOX_OFFER_MIN = 3          # items waiting before Thirties offers to empty a drop box
DROPBOX_OFFER_EVERY = timedelta(days=3)
DROPBOX_OFFER_HOURS = (12, 20)  # offered in the afternoon or early evening, never first thing

TRIAGE_MIN_ITEMS = 10
HORIZON_RE = re.compile(r"\b(today|tonight|tomorrow|this week|next week|this month|next month|this year|soon|"
                        r"later|someday|some day|now|next|urgent|asap|whenever|eventually|probation|active|maybe|"
                        r"backlog|waiting|on hold|short[- ]term|long[- ]term|weekend|done|completed?)\b", re.I)
_HORIZON_URGENCY = [
    (re.compile(r"\b(today|tonight|tomorrow|now|urgent|asap)\b", re.I), "now"),
    (re.compile(r"\b(this week|next week|this month|soon|next|active|short[- ]term|weekend)\b", re.I), "soon"),
    (re.compile(r"\b(someday|some day|maybe|probation|eventually|long[- ]term)\b", re.I), "someday"),
]


def _toolerror(msg: str) -> Exception:
    from thirties_core.assistant_tools import ToolError
    return ToolError(msg)


def _stems(text: str) -> set:
    from thirties_core.assistant_tools import _stem, _words
    return {_stem(w) for w in _words(text)}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def top_notebook(path: str) -> str:
    return (path or "").split(" > ")[0].strip()


def horizon_urgency(section: str) -> str:
    """The urgency a time-horizon heading stands for ("This week" → soon); "whenever" if it says none."""
    for pattern, urgency in _HORIZON_URGENCY:
        if pattern.search(section or ""):
            return urgency
    return "whenever"


_SCOPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS dropbox_scope (
    note_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class DropBoxScopes:
    """What each drop box covers: "notebook" (its own notebook's lists) or "all". Synced, last write wins."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCOPE_SCHEMA)

    def all(self) -> Dict[str, str]:
        with self.db._get_connection() as conn:
            return {r["note_id"]: r["scope"] for r in conn.execute("SELECT * FROM dropbox_scope").fetchall()}

    def set(self, note_id: str, scope: str) -> None:
        if scope not in ("notebook", "all"):
            raise ValueError(f"unknown scope {scope!r}")
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO dropbox_scope (note_id, scope, updated_at) VALUES (?, ?, ?)",
                         (note_id, scope, utc_now_iso()))

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM dropbox_scope").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM dropbox_scope WHERE note_id = ?", (record["note_id"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute("INSERT OR REPLACE INTO dropbox_scope (note_id, scope, updated_at) VALUES (?, ?, ?)",
                         (record["note_id"], record["scope"], record["updated_at"]))
        return True


class Placement:
    """The user's notes as places to put things: roles, sections, what's in them."""

    def __init__(self, notes: List[Dict[str, Any]], roles: Dict[str, Dict[str, str]], tasks: Iterable = (),
                 someday_sections: Iterable[str] = (), scopes: Optional[Dict[str, str]] = None) -> None:
        self.notes = [n for n in notes if n.get("id")]
        self.roles = {nid: (r or {}).get("role", "") for nid, r in roles.items()}
        self.tasks = list(tasks)
        self.someday = list(someday_sections)
        self.scopes = dict(scopes or {})

    @classmethod
    def for_assistant(cls, assistant) -> "Placement":
        from thirties_core.lists import ListCatalog
        db = assistant._db
        notes = {n["id"]: dict(n) for n in assistant._joplin_notes()}
        for t in assistant.s.tasks:  # notes known only from their items (no body read yet)
            nid = t.parent_note_id if t.is_checkbox_item else None
            if nid and nid not in notes and t.parent_note_title:
                notes[nid] = {"id": nid, "title": t.parent_note_title, "source_notebook": t.source_notebook}
        someday = getattr(assistant.pt.scheduler.config.joplin, "someday_sections", []) if assistant.pt.scheduler else []
        return cls(list(notes.values()), ListCatalog(db).roles() if db else {}, assistant.s.tasks, someday,
                   DropBoxScopes(db).all() if db else {})

    # --- notes ---

    def role(self, note: Dict[str, Any]) -> str:
        return self.roles.get(note["id"], "")

    def label(self, note: Dict[str, Any]) -> str:
        """How a note is named to the model: its title, with its notebook when another note has the same title."""
        same = [n for n in self.notes if _norm(n["title"]) == _norm(note["title"])]
        return f"{note.get('source_notebook', '')} > {note['title']}" if len(same) > 1 else note["title"]

    def dropboxes(self) -> List[Tuple[Dict[str, Any], str]]:
        """Each drop box with what it covers. Unless the user said, the only drop box covers everything, and with
        several, each covers its own notebook."""
        boxes = [n for n in self.notes if self.role(n) == "dropbox"]
        return [(n, self.scopes.get(n["id"]) or ("all" if len(boxes) == 1 else "notebook")) for n in boxes]

    def dropbox_for(self, notebook: str = "") -> Optional[Dict[str, Any]]:
        """The drop box for things from this notebook (its own, else one for everything); with no notebook, the one
        for everything, or the only one."""
        boxes = self.dropboxes()
        if notebook:
            own = [n for n, scope in boxes if scope == "notebook"
                   and top_notebook(n.get("source_notebook", "")) == top_notebook(notebook)]
            if own:
                return own[0]
        general = [n for n, scope in boxes if scope == "all"]
        if general:
            return general[0]
        return boxes[0][0] if len(boxes) == 1 else None

    def resolve(self, ref: str) -> Dict[str, Any]:
        """The note a reference names: its id, its title (or "Notebook > Title"), "drop box", or the one note whose
        title shares the most words with it. Otherwise refused, with the candidates."""
        ref = str(ref or "").strip()
        if DROPBOX_REF.match(ref):
            box = self.dropbox_for()
            if box is None:
                names = ", ".join(self.label(n) for n, _ in self.dropboxes())
                raise _toolerror("Which drop box? " + names if names else
                                 "There's no drop box yet. Pick a list, or ask the user which note to use as one "
                                 "(set_list_role role=dropbox).")
            return box
        exact = [n for n in self.notes if n["id"] == ref or _norm(n["title"]) == _norm(ref)
                 or _norm(f"{n.get('source_notebook', '')} > {n['title']}") == _norm(ref)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise _toolerror(f"More than one note is called '{ref}': "
                             + "; ".join(f"{n.get('source_notebook', '')} > {n['title']}" for n in exact)
                             + ". Give the list as 'Notebook > Title'.")
        want = _stems(ref)
        scored = sorted(((len(want & _stems(n["title"])), n["title"], n) for n in self.notes), key=lambda x: -x[0])
        if want and scored and scored[0][0] and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            return scored[0][2]
        raise _toolerror(f"No list is clearly '{ref}'.")

    def refusal(self, note: Dict[str, Any]) -> Optional[str]:
        """Why a new item can't go on this note, or None."""
        why = REFUSED_ROLES.get(self.role(note))
        return f"{note['title']} {why}." if why else None

    # --- sections ---

    def sections(self, note: Dict[str, Any]) -> List[Section]:
        return outline(note.get("body") or "", self.someday)

    def choose_section(self, note: Dict[str, Any], section: Optional[str], title: str,
                       dated: bool) -> Tuple[Optional[str], str]:
        """(where it goes: None for the end of a note without headings, "" for the top, or a heading; how to say
        it). Refuses a section that's wrong for the item."""
        sections = self.sections(note)
        heads = [s for s in sections if s.name]
        named = ", ".join(f"'{s.name}'" + (" (someday)" if s.someday else "") for s in heads
                          if not s.done) or "none"
        if section not in (None, ""):
            if str(section).strip().lower() in ("top", "(top)", "the top"):
                return "", " at the top"
            hit = next((s for s in heads if _norm(s.name) == _norm(section)), None)
            if hit is None:
                if not heads and note.get("body") is not None:
                    raise _toolerror(f"{note['title']} has no sections; leave section out.")
                if note.get("body") is None:
                    return str(section).strip(), f" under '{str(section).strip()}'"
                raise _toolerror(f"{note['title']} has no section '{section}'. Its sections: {named}. Pick one of "
                                 "them, or leave section out.")
            if hit.done:
                raise _toolerror(f"'{hit.name}' in {note['title']} is for finished things. Pick another section, or "
                                 "leave section out.")
            if hit.someday and dated:
                ok = [s.name for s in heads if not s.someday and not s.done]
                raise _toolerror(f"'{hit.name}' in {note['title']} is a someday section, and this item has a date or "
                                 "can't wait. " + (f"Use {', '.join(repr(x) for x in ok)}, or leave section out."
                                                    if ok else "Leave section out (it goes at the top)."))
            return hit.name, f" under '{hit.name}'"
        if not heads:
            return None, ""
        top = next((s for s in sections if not s.name), None)
        allowed = ([top] if top else []) + [s for s in heads if not s.someday and not s.done]
        if not allowed:
            return "", " at the top, above its someday sections"
        best = allowed[0]
        if len(allowed) > 1:
            want = _stems(title)
            scores = [len(want & _stems(s.name + " " + " ".join(s.open_items))) for s in allowed]
            if max(scores) > 0:
                best = allowed[scores.index(max(scores))]
        return (best.name, f" under '{best.name}'") if best.name else ("", " at the top")

    # --- candidates ---

    def _items_of(self, note: Dict[str, Any]) -> List[str]:
        return [t for s in self.sections(note) if not s.someday and not s.done for t in s.open_items]

    def candidates(self, title: str, limit: int = 5, notebook: str = "", exclude: Iterable[str] = ()) -> List[tuple]:
        """Notes a new item could go on, best first: (note, score, a section suggestion or '')."""
        want, skip = _stems(title), set(exclude)
        out = []
        for note in self.notes:
            if note["id"] in skip or self.role(note) not in PLACEABLE_ROLES or self.role(note) == "dropbox":
                continue
            if notebook and top_notebook(note.get("source_notebook", "")) != top_notebook(notebook):
                continue
            items = self._items_of(note)
            score = 3 * len(want & _stems(note["title"])) + min(4, sum(1 for t in items if want & _stems(t)))
            out.append((note, score))
        out.sort(key=lambda x: -x[1])
        picked = []
        for note, score in out[:limit]:
            try:
                where, _ = self.choose_section(note, None, title, dated=False)
            except Exception:
                where = None
            picked.append((note, score, where or ""))
        return picked

    def describe(self, note: Dict[str, Any], sample: int = 3) -> str:
        """A note as a place: its title, role, sections (someday ones marked) and a few of its items."""
        role = self.role(note) or "not sorted yet"
        parts = []
        for s in self.sections(note):
            if s.done:
                continue
            items = ", ".join(s.open_items[:sample]) + ("…" if len(s.open_items) > sample else "")
            name = f"'{s.name}'" if s.name else "top"
            parts.append(f"{name}{' (someday: not for dated or pressing items)' if s.someday else ''}: {items or '(empty)'}")
        inside = "; ".join(parts) if parts else "(no items yet)"
        return f"{self.label(note)} [{role}] — {inside}"

    def candidates_text(self, title: str) -> str:
        lines = [f"- {self.describe(n)}" for n, _, _ in self.candidates(title, limit=6)]
        boxes = [f"- {self.describe(n, 2)} (drop box for {'everything' if scope == 'all' else top_notebook(n.get('source_notebook', '')) or 'its notebook'})"
                 for n, scope in self.dropboxes()]
        return ("Lists that could take it, best first:\n" + ("\n".join(lines) or "- (none)")
                + ("\nDrop box, when none of them is obvious:\n" + "\n".join(boxes) if boxes else ""))

    # --- the drop box, emptied ---

    def dropbox_items(self) -> List[Tuple[Dict[str, Any], str, List[Any]]]:
        """Each drop box, its scope, and its open top-level items (as TaskItems, with ids)."""
        out = []
        for note, scope in self.dropboxes():
            items = [t for t in self.tasks if t.is_checkbox_item and t.parent_note_id == note["id"]
                     and not t.parent_task_id and not t.someday]
            out.append((note, scope, items))
        return out

    def dropbox_review(self) -> str:
        blocks = []
        for note, scope, items in self.dropbox_items():
            if not items:
                continue
            notebook = top_notebook(note.get("source_notebook", "")) if scope == "notebook" else ""
            lines = []
            for t in items:
                cands = self.candidates(t.title, limit=3, notebook=notebook, exclude=[note["id"]])
                where = "; ".join(f"{self.label(n)}" + (f" ('{w}')" if w else "") for n, score, w in cands if score) \
                    or "nothing obvious"
                lines.append(f"- {t.title} task_id={t.id}: likely {where}")
            blocks.append(f"{self.label(note)} (drop box for {'everything' if scope == 'all' else notebook}) holds "
                          f"{len(items)}:\n" + "\n".join(lines))
        if not blocks:
            return "The drop box is empty."
        return ("\n\n".join(blocks) + "\n\nPropose a home for each item, one per line with a short reason "
                "(\"- Clip cat nails → Errands/Admin, 'Active': an errand\"); a list from the user's backlog may fit "
                "better than these guesses. Move each item the user agrees to with edit_list_item (to_list, "
                "to_section), and leave the rest where they are.")

    # --- triage of a list sorted by time ---

    def sorted_by_time(self, note: Dict[str, Any]) -> bool:
        """A list whose headings are time horizons ("This week", "Later", "Someday") rather than areas of life,
        with enough in it that splitting it by area would help."""
        if self.role(note) not in ("list", "project"):
            return False
        heads = [s for s in self.sections(note) if s.name]
        horizon = [s for s in heads if HORIZON_RE.search(s.name)]
        items = sum(len(s.open_items) for s in self.sections(note))
        return len(horizon) >= 2 and len(horizon) >= 0.6 * len(heads) and items >= TRIAGE_MIN_ITEMS

    def triage_text(self, note: Dict[str, Any]) -> str:
        items = [t for t in self.tasks if t.is_checkbox_item and t.parent_note_id == note["id"] and not t.parent_task_id]
        homes = [n for n in self.notes if n["id"] != note["id"] and self.role(n) in PLACEABLE_ROLES
                 and top_notebook(n.get("source_notebook", "")) == top_notebook(note.get("source_notebook", ""))]
        lines = [f"- {t.title} task_id={t.id} [{t.section or 'top'} → {horizon_urgency(t.section)}]" for t in items]
        return (f"{self.label(note)} is sorted by time, not by area. Its {len(items)} items (with the urgency each "
                "keeps after the move):\n" + "\n".join(lines)
                + "\nLists already in its notebook: " + (", ".join(self.label(n) for n in homes) or "none")
                + "\nGroup the items by area of life (home, errands, health, a project...), each group going to one "
                "of those lists or a new note named for the area. Propose one group at a time, by name with its "
                "items; on the user's yes, write it with apply_triage_group. Items the user wants kept stay put.")


def dropbox_offer(placement: Placement, db, now: datetime) -> str:
    """The occasional offer to empty a drop box ("" when it isn't time): enough waiting, not offered lately, and
    in the afternoon or early evening."""
    if not (DROPBOX_OFFER_HOURS[0] <= now.hour < DROPBOX_OFFER_HOURS[1]):
        return ""
    for note, _, items in placement.dropbox_items():
        if len(items) < DROPBOX_OFFER_MIN:
            continue
        key = f"dropbox_offered:{note['id']}"
        last = db.get_sync_meta(key)
        try:
            if last and now - datetime.fromisoformat(last) < DROPBOX_OFFER_EVERY:
                continue
        except ValueError:
            pass
        db.set_sync_meta(key, now.isoformat())
        return (f"🗃️ {len(items)} things are waiting in {note['title']}. Want me to suggest a home for each? "
                "You'd just say yes or no to each one.")
    return ""


def triage_offer_badge(placement: Placement, db, note: Dict[str, Any]) -> str:
    """For the backlog in the prompt: a list sorted by time that hasn't had the triage offer yet."""
    if db is None or db.get_sync_meta(f"triage:{note['id']}") or not placement.sorted_by_time(note):
        return ""
    return " [Sorted by time, not by area: once, when it fits, offer to split it by area (triage_list)]"


__all__ = ["Placement", "DropBoxScopes", "PLACEABLE_ROLES", "REFUSED_ROLES", "dropbox_offer", "triage_offer_badge",
           "horizon_urgency", "top_notebook", "DROPBOX_OFFER_MIN", "DROPBOX_OFFER_EVERY", "TRIAGE_MIN_ITEMS"]
