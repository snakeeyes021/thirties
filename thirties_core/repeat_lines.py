"""A line about an item's time is said once a day, not under every reply.

"Clay delivery is at 3:00 PM." is worth saying when it's news: the first time, after the item moved, or when the
user asks about it. Said again under the next reply, and the next, it's noise, and the user has to ask for it to
stop. So, on every reply, in Python:

- a sentence that only gives an item's time ("<item> is at <time>", "<item> is still at <time>") and was already
  said word for word in an earlier reply today goes, unless the user's message names that item;
- the user asking for it to stop ("stop repeating the clay line", "you've said that already") stops it: lines about
  the items they name, or failing that the items such lines named in the last reply, are left out for the rest of
  the day, until the item moves or the user asks about it.

Anything else in the reply stays as it is; a reply that would be left empty is kept whole.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

KEY = "repeat_lines_stopped"  # {"day": "<iso>", "items": {<title, lowercased>: <its start when stopped>}}

_FACT_RE = re.compile(r"^\s*(?P<title>[^.!?\n]{2,160}?) is (?:still )?(?:at|on the plan at|set for|planned for) "
                      r"(?P<time>\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)\s*[.!]?\s*$")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# the user asking for a repeated line to stop: about repeating, a line, a mention
_STOP_RE = re.compile(
    r"\b(?:stop|quit|enough|no more|don'?t keep|do not keep|cut|drop|lose|skip|leave out)\b[^.?!\n]{0,40}?"
    r"\b(?:repeat\w*|saying (?:that|it|this)|telling me|mention\w*|bringing (?:it|that|this) up|lines?|bit|"
    r"sentences?|"
    r"reminding me)\b|\byou(?:'ve| have)? (?:already )?(?:said|told me) (?:that|it|this)\b|"
    r"\bstop (?:it|that)\b|\bnot (?:that|this) (?:line )?again\b", re.IGNORECASE)
# a shorter "I know" / "I get it" counts only right after a reply that repeated a line
_HEARD_RE = re.compile(r"\bi (?:know|get it|got it|heard you)\b", re.IGNORECASE)


def _facts(text: str) -> List[tuple]:
    """(sentence, title) for each sentence that only gives an item's time."""
    out = []
    for line in (text or "").split("\n"):
        for sentence in _SENTENCE_RE.split(line):
            m = _FACT_RE.match(sentence)
            if m:
                out.append((sentence.strip(), m.group("title").strip()))
    return out


def _said_before(messages: Iterable[Dict[str, Any]]) -> set:
    said = set()
    for m in messages:
        if m.get("role") == "assistant" and not m.get("hidden") and not m.get("spent"):
            said |= {s for s, _ in _facts(str(m.get("content") or ""))}
    return said


def _last_reply(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and not m.get("hidden") and not m.get("spent") and m.get("content"):
            return str(m["content"])
    return ""


def _load(db, today: date) -> Dict[str, str]:
    try:
        data = json.loads(db.get_sync_meta(KEY) or "{}") if db is not None else {}
    except ValueError:
        return {}
    return data.get("items", {}) if isinstance(data, dict) and data.get("day") == today.isoformat() else {}


def _span_for(title: str, plan) -> Optional[Any]:
    key = title.strip().lower()
    for s in getattr(plan, "spans", []) or []:
        if (s.title or "").strip().lower() == key:
            return s
    return None


def stop_asked(said: str, messages: List[Dict[str, Any]], plan, db, today: date) -> List[str]:
    """Record the user's ask to stop a repeated line; the titles it stops (lowercased)."""
    if not said or db is None:
        return []
    last = _facts(_last_reply(messages))
    if not (_STOP_RE.search(said) or (last and _HEARD_RE.search(said))):
        return []
    from thirties_core.item_facts import mentions
    titles = [s.title for s in getattr(plan, "spans", []) or [] if s.title]
    named = [t for t in titles if mentions(said, t, titles)]
    if not named:
        named = [t for _, t in last]
    if not named:
        return []
    items = _load(db, today)
    for t in named:
        span = _span_for(t, plan)
        items[t.strip().lower()] = span.start_dt.isoformat() if span is not None else ""
    db.set_sync_meta(KEY, json.dumps({"day": today.isoformat(), "items": items}))
    return [t.strip().lower() for t in named]


def without_repeats(reply: str, said: str, messages: List[Dict[str, Any]], plan, db, today: date,
                    asking: Iterable[str] = ()) -> str:
    """The reply without the time lines already said today or asked to stop (see the module notes); ``asking``
    are the titles this message asked to stop (naming them is no question about them)."""
    facts = _facts(reply)
    if not facts:
        return reply
    from thirties_core.item_facts import mentions
    before = _said_before(messages)
    stopped = _load(db, today)
    titles = [s.title for s in getattr(plan, "spans", []) or [] if s.title]
    asking = set(asking)
    drop = set()
    for sentence, title in facts:
        key = title.lower()
        if mentions(said or "", title, titles) and key not in asking:
            continue  # the user asked about it: the answer stays
        if key in stopped:
            span = _span_for(title, plan)
            if span is None or not stopped[key] or span.start_dt.isoformat() == stopped[key]:
                drop.add(sentence)
                continue
        if sentence in before:
            drop.add(sentence)
    if not drop:
        return reply
    lines = []
    for line in reply.split("\n"):
        parts = _SENTENCE_RE.split(line)
        kept = [p for p in parts if p.strip() not in drop]
        if parts and not kept and line.strip():
            continue
        lines.append(" ".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or reply


def after_reply(manager, reply: str, said: str, before: int) -> str:
    """Run on each reply (conversation.py): record a stop ask, then leave out the lines it stops and the ones
    already said today."""
    db = manager.scheduler.state_db if getattr(manager, "scheduler", None) is not None else None
    plan = manager.day_plan
    today = plan.target_date
    earlier = manager.brain.messages[:before]
    asking = stop_asked(said, earlier, plan, db, today)
    return without_repeats(reply, said, earlier, plan, db, today, asking)


__all__ = ["KEY", "after_reply", "stop_asked", "without_repeats"]
