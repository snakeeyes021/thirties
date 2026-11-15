"""Settings for how Thirties reads and keeps the user's lists. The data layer: the app's Preferences
show and change these; the assistant can too, when the user says so.

What the user decides: which notebooks and notes to read or not, what each list's time scale is, how to treat
one-off notes, and a note of their choosing for done items.

What's already a setting elsewhere, gathered here for one view (``structure``):
- the notebooks read and the ones left out, and single notes read from anywhere (joplin.allowed_notebooks,
  joplin.excluded_notebooks, joplin.whitelist_notes);
- each list's role (lists.ListCatalog) and its time scale (horizons.HorizonCatalog: day, week, month, year,
  someday);
- whether done items are filed at all (joplin.file_done).

New here, both shared (synced, about the person):
- **one-off notes** (``lists.one_off_notes``): a Joplin to-do note with no checklist in it is one thing to do. It's
  read as one item ("item", the default), as a someday item that's never pushed ("someday"), or not at all
  ("ignore"). A note the user gave a role keeps it.
- **the done note** (``lists.done_note``): the note done items are filed in, as {"id", "title"}. Unset, they go to
  one "Done <month>" note per month in a Thirties notebook (done_log.py). A chosen note that's gone from Joplin
  falls back to the monthly notes, so a done item is never lost.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ONE_OFF_KEY = "lists.one_off_notes"
DONE_NOTE_KEY = "lists.done_note"
ONE_OFF_CHOICES = {
    "item": "each is one thing to do, planned like any list item",
    "someday": "each is a someday item: kept, never pushed",
    "ignore": "not read at all",
}


def _settings(db):
    from thirties_core.settings import Settings
    return Settings(db)


def one_off_mode(db) -> str:
    if db is None:
        return "item"
    value = _settings(db).get(ONE_OFF_KEY)
    return value if value in ONE_OFF_CHOICES else "item"


def set_one_off_mode(db, mode: str) -> None:
    mode = str(mode or "").strip().lower()
    if mode not in ONE_OFF_CHOICES:
        raise ValueError(f"one-off notes: one of {', '.join(ONE_OFF_CHOICES)}")
    _settings(db).set(ONE_OFF_KEY, mode)


def is_one_off(task) -> bool:
    """A note that is itself one thing to do (a to-do note with no checklist), not a line in a list."""
    return not getattr(task, "is_checkbox_item", True) and not str(getattr(task, "id", "")).startswith("media:")


def apply_one_off(db, tasks: List[Any]) -> List[Any]:
    """The one-off notes treated as the setting says; a note with a role the user gave keeps it."""
    mode = one_off_mode(db)
    if mode == "item" or db is None:
        return tasks
    from thirties_core.lists import ListCatalog
    roles = ListCatalog(db).roles()
    out = []
    for t in tasks:
        if is_one_off(t) and (roles.get(t.id) or {}).get("source") != "user":
            if mode == "ignore":
                continue
            t.someday = True
        out.append(t)
    return out


def done_note(db) -> Optional[Dict[str, str]]:
    """The note the user chose for done items, or None (the monthly Done notes)."""
    if db is None:
        return None
    value = _settings(db).get(DONE_NOTE_KEY)
    return value if isinstance(value, dict) and value.get("id") else None


def set_done_note(db, note_id: str = "", title: str = "") -> None:
    """Choose the done note (an existing note, by id), or clear it with no id."""
    _settings(db).set(DONE_NOTE_KEY, {"id": note_id, "title": title} if note_id else None)


def structure(db, config=None) -> Dict[str, Any]:
    """Everything about how the lists are read and kept, for one Preferences view."""
    from thirties_core.lists import ListCatalog
    joplin = getattr(config, "joplin", None)
    roles = ListCatalog(db).roles() if db is not None else {}
    try:
        from thirties_core.horizons import HorizonCatalog
        horizons = HorizonCatalog(db).notes() if db is not None else {}
    except Exception:
        horizons = {}
    return {
        "notebooks": list(getattr(joplin, "allowed_notebooks", []) or []),
        "excluded_notebooks": list(getattr(joplin, "excluded_notebooks", []) or []),
        "notes": list(getattr(joplin, "whitelist_notes", []) or []),
        "lists": [{"id": nid, "title": r.get("title", ""), "role": r.get("role", ""),
                   "role_source": r.get("source", ""), "horizon": (horizons.get(nid) or {}).get("horizon", "")}
                  for nid, r in sorted(roles.items(), key=lambda kv: (kv[1].get("title") or "").lower())],
        "one_off_notes": one_off_mode(db),
        "file_done": bool(getattr(joplin, "file_done", True)) if joplin is not None else True,
        "done_note": done_note(db),
    }


__all__ = ["DONE_NOTE_KEY", "ONE_OFF_CHOICES", "ONE_OFF_KEY", "apply_one_off", "done_note", "is_one_off",
           "one_off_mode", "set_done_note", "set_one_off_mode", "structure"]
