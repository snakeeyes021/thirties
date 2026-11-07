"""An item's short title and its long description (11-06, the user's design, part of the original vision).

Every item has a short title: what the chat and the app show. A long source line ("**Water heater: book the
yearly flush** (the tank is due on 11-04…) · then tell the landlord (details: https://…)")
is split: the headline (the bold part, else the first clause) is the title, and the rest is the description,
hidden by default (expandable in the app, never dumped into the chat). No raw markdown on screen. The user can
also set a description explicitly (set_item_note), kept in Thirties' own records so Joplin stays as written.

The item's source line stays its identity in Joplin (exact-link writes); only what's shown changes.

And no raw id is ever shown as a title (11-06: "Birthday cards" showed as 31a1d3808b2c9b79): ``is_raw_id`` catches
one, and ``repair_raw_titles`` puts the real title back on records already affected.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional, Tuple

RAW_ID = re.compile(r"^(?:cal:)?[0-9a-f]{12}(?:[0-9a-f]{4})?$")
SHORT_LINE = 60  # a plain line this long or shorter is a title as it stands
NOTES_KEY = "item_notes"  # {task_id or lowercased title: the description the user set}

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"\s*\(?(?:full report: )?https?://\S+\)?")


def is_raw_id(text: Any) -> bool:
    return bool(RAW_ID.match(str(text or "").strip()))


def _plain(text: str) -> str:
    """Markdown off: links to their words, bold and code marks dropped, warning signs and spacing tidied."""
    text = _LINK.sub(r"\1", str(text or ""))
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"\s*⚠\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_title(text: Any, limit: int = 80) -> Tuple[str, str]:
    """(short title, description) for a source line. A short line is all title."""
    raw = str(text or "").strip()
    if len(raw) <= SHORT_LINE and not re.search(r"\*\*|`|\]\(|https?://", raw):
        return raw, ""  # a short plain line is all title, asides and all ("Breakfast (w/ juice)")
    raw = re.sub(r"^(?:[a-z]|\d{1,2})[.)]\s+(?=\S)", "", raw)  # "b. READY: ..." (a lettered list line)
    bold = re.match(r"^\*\*(.+?)\*\*\s*[.:]?\s*(.*)$", raw)
    if bold:
        title, rest = _plain(bold.group(1)), bold.group(2)
    else:
        plain = _plain(raw)
        parts = re.split(r"\s+\(|\s+·\s+|(?<=.{12}[.!?])\s", plain, maxsplit=1)
        title = parts[0].strip()
        rest = plain[len(parts[0]):] if len(parts) > 1 else ""
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    description = _plain(_URL.sub("", rest)).strip(" ·;,") if rest else ""
    description = re.sub(r"^\((.*)\)$", r"\1", description).strip()
    return title, description


def title_of(text: Any, limit: int = 80) -> str:
    return split_title(text, limit)[0]


class ItemNotes:
    """Descriptions the user set for items (by task id when there is one, else by wording)."""

    def __init__(self, db) -> None:
        self.db = db

    def _load(self) -> Dict[str, str]:
        raw = self.db.get_sync_meta(NOTES_KEY) if self.db is not None else None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def get(self, task_id: str = "", title: str = "") -> str:
        data = self._load()
        return data.get(task_id or "") or data.get(str(title or "").strip().lower()) or ""

    def set(self, text: str, task_id: str = "", title: str = "") -> None:
        data = self._load()
        data[task_id or str(title).strip().lower()] = str(text or "").strip()
        self.db.set_sync_meta(NOTES_KEY, json.dumps(data, sort_keys=True))


def description_for(db, title: str, task_id: str = "") -> str:
    """What the app shows when an item is expanded: the user's own note, else what's left of a long line."""
    note = ItemNotes(db).get(task_id, title) if db is not None else ""
    return note or split_title(title)[1]


def known_titles(db, tasks: Iterable[Any] = ()) -> Dict[str, str]:
    """Every item id Thirties knows, with its wording: the lists as read, the leftovers, today's links, and what
    the day proposals recorded."""
    out: Dict[str, str] = {}
    for t in tasks:
        if getattr(t, "id", None) and getattr(t, "title", ""):
            out[t.id] = t.title
    if db is None:
        return out
    try:
        pool = json.loads(db.get_sync_meta("open_pool") or "{}")
        out.update({v["task_id"]: v["title"] for v in pool.values() if v.get("task_id") and v.get("title")})
    except (ValueError, AttributeError, KeyError):
        pass
    try:
        with db._get_connection() as conn:
            for row in conn.execute("SELECT home_id, home_title FROM today_links"):
                out.setdefault(row["home_id"], row["home_title"])
    except Exception:  # no links table yet
        pass
    try:
        with db._get_connection() as conn:
            for (value,) in conn.execute("SELECT value FROM sync_meta WHERE key LIKE 'day_proposal:%'"):
                try:
                    for item in json.loads(value).get("items", []):
                        if item.get("task_id") and item.get("title") and not is_raw_id(item["title"]):
                            out.setdefault(item["task_id"], item["title"])
                except (ValueError, AttributeError):
                    continue
    except Exception:
        pass
    return {k: v for k, v in out.items() if not is_raw_id(v)}


def resolve_raw(db, text: str, tasks: Iterable[Any] = ()) -> Optional[str]:
    """The wording behind a raw id, if Thirties knows it."""
    return known_titles(db, tasks).get(str(text or "").strip())


def repair_raw_titles(db, tasks: Iterable[Any] = ()) -> int:
    """Records whose title is a raw id get their real title back (and the link to the item, if the item is
    still on a list). Returns how many were repaired."""
    tasks = list(tasks)
    names = known_titles(db, tasks)
    by_title = {getattr(t, "title", "").strip().lower(): t for t in tasks}
    fixed = 0
    with db._get_connection() as conn:
        rows = conn.execute("SELECT id, data FROM spans WHERE deleted = 0").fetchall()
    for row in rows:
        data = json.loads(row["data"])
        raw = str(data.get("title") or "").strip()
        if not is_raw_id(raw) or raw not in names:
            continue
        data["title"] = names[raw]
        task = by_title.get(names[raw].strip().lower())
        if not data.get("task_id"):
            data["task_id"] = task.id if task is not None else raw
        with db._get_connection() as conn:
            conn.execute("UPDATE spans SET data = ? WHERE id = ?", (json.dumps(data), row["id"]))
        fixed += 1
    with db._get_connection() as conn:  # the day proposals that recorded one, so the morning says it right
        for key, value in conn.execute("SELECT key, value FROM sync_meta WHERE key LIKE 'day_proposal:%'").fetchall():
            try:
                data = json.loads(value)
            except ValueError:
                continue
            changed = False
            for item in data.get("items", []):
                if is_raw_id(item.get("title")) and item["title"] in names:
                    item["task_id"], item["title"], changed = item.get("task_id") or item["title"], names[item["title"]], True
            if changed:
                conn.execute("UPDATE sync_meta SET value = ? WHERE key = ?", (json.dumps(data), key))
    return fixed


def long_titles(texts: Iterable[Any]) -> list:
    """The item lines among ``texts`` that have a shorter title (longest first, so a line inside another isn't
    replaced first)."""
    out = {str(t).strip() for t in texts if t and split_title(t)[0] != str(t).strip()}
    return sorted(out, key=len, reverse=True)


def shorten_in(text: str, raws: Iterable[str]) -> str:
    """Every whole item line in ``text`` said by its short title (11-07: the schedule sent on request carried
    the full line, markdown and all). Applied where text leaves Thirties: tool results the model reads, its
    replies, and the messages the background sends."""
    if not text:
        return text
    for raw in raws:
        if raw in text:
            text = text.replace(raw, split_title(raw)[0])
    return text
