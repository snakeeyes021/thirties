"""Something to read, watch or listen to, and where it came from, with the Media Queue first.

The user asks for one or two things from their lists, or for something to put on alongside other work (an
easy film while they fold the laundry). Python gathers the
candidates from every list they keep: the Media Queue note first (the "up next" list, 10-30), then the
public wishlists (Goodreads, Letterboxd, Serializd) and the want-to lists in their notes. It says where each one
came from, rotates so the same few aren't offered every day, and, for "alongside", gives the time of the thing
it goes with. The model picks one or two that fit the ask (its judgement: "easy", "something light")
and says why.

Each pick keeps its source in the reply: when the reply names a title Python offered without saying where it's
from, Python adds it after the title (``name_sources``).
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from thirties_core.clock import format_clock, format_duration

QUEUE_RE = re.compile(r"\bmedia\s+queue\b|\bup\s+next\b", re.IGNORECASE)
_KIND_WORDS = {
    "watch": re.compile(r"\b(watch|movies?|films?|shows?|tv|series|anime|documentar(?:y|ies))\b", re.IGNORECASE),
    "read": re.compile(r"\b(read|books?|novels?|comics?|manga|articles?)\b", re.IGNORECASE),
    "listen": re.compile(r"\b(listen|albums?|music|podcasts?|audiobooks?|records?)\b", re.IGNORECASE),
}
_MEDIA_NOTE_RE = re.compile(r"\b(want[- ]to|to[- ](?:read|watch|listen)|watchlist|reading list|media)\b",
                            re.IGNORECASE)
_VERB_RE = re.compile(r"^(?:read|watch|listen(?: to)?)\s*:\s*", re.IGNORECASE)
_WISHLIST_KIND = {"book": "read", "film": "watch", "show": "watch"}
_KIND_ALIASES = {"read": "read", "book": "read", "books": "read", "reading": "read",
                 "watch": "watch", "movie": "watch", "film": "watch", "show": "watch", "tv": "watch",
                 "movies": "watch", "films": "watch", "shows": "watch", "watching": "watch",
                 "listen": "listen", "music": "listen", "album": "listen", "podcast": "listen", "audio": "listen"}
RECENT_KEY = "media_offered"  # sync meta: {item id: day offered}, so the same few aren't offered every day
RECENT_DAYS = 10
OFFER = 8


@dataclass
class Pick:
    id: str
    title: str        # as the user would say it: no "Watch:" prefix
    kind: str         # read / watch / listen / "" (unknown)
    source: str       # where it came from, as said to the user: "your Media Queue", "your Letterboxd watchlist"
    queue: bool = False


def _kind_of(text: str) -> str:
    for kind, rx in _KIND_WORDS.items():
        if rx.search(text or ""):
            return kind
    return ""


def _wishlist_source(task) -> str:
    from thirties_core.media_sources import SOURCE_LABELS
    tags = list(task.tags or [])
    label = SOURCE_LABELS.get(tags[1], tags[1].title()) if len(tags) > 1 else "media"
    rest = task.source_notebook.split(label, 1)[-1].strip() if label in task.source_notebook else ""
    if rest in ("", "to-read"):
        rest = {"Goodreads": "to-read shelf"}.get(label, rest or "list")
    return f"your {label} {rest}"


def is_queue(task) -> bool:
    return bool(QUEUE_RE.search(task.parent_note_title or "")) and not task.id.startswith("media:")


def candidates(tasks: Iterable[Any]) -> List[Pick]:
    """Everything on the user's media lists, the Media Queue first."""
    out: List[Pick] = []
    for t in tasks or []:
        if t.id.startswith("media:"):
            kind = next((_WISHLIST_KIND[k] for k in (t.tags or []) if k in _WISHLIST_KIND), "")
            out.append(Pick(t.id, _VERB_RE.sub("", t.title), kind, _wishlist_source(t)))
            continue
        note = t.parent_note_title or ""
        if getattr(t, "someday", False) and not is_queue(t):
            continue
        if is_queue(t):
            out.append(Pick(t.id, _VERB_RE.sub("", t.title), _kind_of(t.title) or _kind_of(t.section),
                            f"your {note}", queue=True))
        elif _MEDIA_NOTE_RE.search(note) or (_kind_of(note) and re.search(r"\blists?\b|\bto\b", note, re.I)):
            out.append(Pick(t.id, _VERB_RE.sub("", t.title), _kind_of(note) or _kind_of(t.title), f"your {note}"))
    return sorted(out, key=lambda p: not p.queue)


def _recent(db, today: date) -> Dict[str, str]:
    if db is None:
        return {}
    try:
        seen = json.loads(db.get_sync_meta(RECENT_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in seen.items() if (today - date.fromisoformat(v)).days < RECENT_DAYS}


def offer(tasks: Iterable[Any], kind: str = "", db=None, today: Optional[date] = None, limit: int = OFFER) -> List[Pick]:
    """A handful to choose from: the queue first, in its order; then the rest, shuffled by the day, leaving out
    what was offered in the last few days while there's anything else."""
    today = today or date.today()
    want = _KIND_ALIASES.get((kind or "").strip().lower(), "")
    pool = [p for p in candidates(tasks) if not want or p.kind in (want, "")]
    if want:  # a known kind first; an unknown kind (a mixed queue) only when nothing else fits
        pool = [p for p in pool if p.kind == want] or pool
    recent = _recent(db, today)
    queue = [p for p in pool if p.queue]
    rest = [p for p in pool if not p.queue]
    random.Random(today.toordinal()).shuffle(rest)
    fresh = [p for p in rest if p.id not in recent]
    rest = fresh + [p for p in rest if p.id in recent] if fresh else rest
    picks = (queue + rest)[:limit]
    if db is not None and picks:
        recent.update({p.id: today.isoformat() for p in picks})
        db.set_sync_meta(RECENT_KEY, json.dumps(recent))
    return picks


# --- the tool ---

MEDIA_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "suggest_media",
    "description": "Something to read, watch or listen to from the user's own lists (their Media Queue first: "
                   "what's up next), each with where it came from. Use when they ask for something from their lists, "
                   "or for something to put on alongside other work. Pick one or two that fit what they asked "
                   "(a mood, a length) and say where each is from. To put one alongside something on the plan, "
                   "give alongside, then add it at that time with add_event.",
    "parameters": {"kind": "read, watch or listen (leave out for any).",
                   "alongside": "An item on the plan to do it alongside (its id or name), if they asked."},
    "required": [],
}]
MEDIA_READ_ONLY = {"suggest_media"}


def suggest(assistant, kind: Any = "", alongside: Any = "") -> str:
    from thirties_core.assistant_tools import ToolError
    picks = offer(assistant.s.tasks, str(kind or ""), assistant._db, assistant._today())
    if not picks:
        what = {"read": "to read", "watch": "to watch", "listen": "to listen to"}.get(
            _KIND_ALIASES.get(str(kind or "").lower(), ""), "")
        raise ToolError(f"Nothing {what} is on the user's lists.".replace("  ", " "))
    offered = getattr(assistant, "media_offered", None)
    if offered is None:
        offered = assistant.media_offered = {}
    offered.update({p.title: p.source for p in picks})
    lines = [f"{p.title} (from {p.source}{': up next' if p.queue else ''}) id={p.id}" for p in picks]
    out = "To choose from (say where each pick is from):\n" + "\n".join(lines)
    if alongside not in (None, ""):
        span = assistant.pt._find_span(alongside)
        out += (f"\nAlongside {span.title}: {format_clock(span.start_dt)} – {format_clock(span.end_dt)} "
                f"({format_duration(int((span.end_dt - span.start_dt).total_seconds() // 60))}). Add the pick at "
                f"that start with add_event; the two share the time.")
    return out


def _core(title: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*$", "", title).strip()


def name_sources(reply: str, offered: Optional[Dict[str, str]]) -> str:
    """The reply, with "(from <source>)" after each offered title it names without saying where it's from."""
    if not reply or not offered:
        return reply
    for title, source in offered.items():
        core = _core(title)
        if len(core) < 3:
            continue
        m = re.search(re.escape(core), reply, re.IGNORECASE)
        if m is None:
            continue
        place = source.replace("your ", "", 1)
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]+", place) if w.lower() not in ("list", "the")]
        if any(re.search(re.escape(w), reply, re.IGNORECASE) for w in words[:1]):
            continue  # it says where it's from already
        end = m.end()
        tail = re.match(r"\s*\([^)]*\)", reply[end:])  # after "(1996)", not inside it
        if tail:
            end += tail.end()
        tail = re.match(r"\*+|[\"”']", reply[end:])
        if tail:
            end += tail.end()
        reply = reply[:end] + f" (from {source})" + reply[end:]
    return reply


def after_reply(assistant, reply: str) -> str:
    """For the conversation, once a reply is final: sources named, and this turn's offers forgotten."""
    offered = getattr(assistant, "media_offered", None) if assistant is not None else None
    if not offered:
        return reply
    assistant.media_offered = {}
    return name_sources(reply, offered)


def prompt_section(tasks: Iterable[Any]) -> str:
    """The Media Queue for the prompt: what's up next, in its order."""
    queue = [p for p in candidates(tasks) if p.queue]
    if not queue:
        return ""
    return ("MEDIA QUEUE (what the user means to read, watch or listen to next, in order: offer these first; "
            "suggest_media finds more)\n" + "\n".join(f"- {p.title} id={p.id}" for p in queue[:15]))


__all__ = ["Pick", "candidates", "offer", "suggest", "name_sources", "after_reply", "prompt_section", "is_queue",
           "MEDIA_TOOL_SPECS", "MEDIA_READ_ONLY"]
