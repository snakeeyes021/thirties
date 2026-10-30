"""Overlap or push: which one the user means when something lands on what's already there.

The user, 10-30: "Anything can overlap anything, but the assistant will need to know when the user is asking for throw
xyz thing at 5 to OVERLAP with the thing already there or to push it and everything else behind it back." So:

- Overlap is allowed everywhere and never destroys anything (PlanTools._place).
- Explicit push words ("push everything", "make room", "bump", "and what follows") mean push: Python slides what
  follows later, in order, around fixed things (``wants_push``).
- Explicit together words ("alongside", "during", "while", "at the same time", "with") mean overlap, silently.
- Unclear, and the new item lands on planned items that both need the user's attention (not work or sleep, not a
  background item like a long dev session, an audiobook or the laundry running): the tool result flags it, as a note,
  not a failure, so the model asks one short question ("Alongside the shower, or push the shower and what follows
  back?") or acts if the context makes it obvious (``needs_a_word``).
- The answer is kept when it generalises ("dev sessions can share time with anything"): ``SharingRules``, keyed by a
  kind of item (``kind_of``), synced across devices.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from thirties_core.models import EventSpan, SpanStatus
from thirties_core.scheduler import StateDatabase, utc_now_iso

# "...and push everything to make room": the user wants what's there (and after it) to move.
EXPLICIT_PUSH_RE = re.compile(
    r"\b(make room|push(?:ing)? (?:everything|the rest|it all|all|things|stuff|what'?s (?:there|after|next)|them|"
    r"(?:that|those|it) (?:back|later|along))|bump\w*|shift (?:everything|the rest|it all|things)|"
    r"slide (?:everything|the rest|things)|everything (?:else )?(?:back|later|after|behind)|"
    r"(?:and|with) (?:everything|what(?:'s| is)?) (?:after|behind|follows|following)|what follows|"
    r"move (?:everything|the rest) (?:back|later))\b", re.IGNORECASE)
# "...during dinner", "at the same time": the user wants them together.
TOGETHER_RE = re.compile(r"\b(alongside|during|while|whilst|at the same time|same time|together|with|on top of|"
                         r"overlap\w*|meanwhile|in the background|as well)\b", re.IGNORECASE)
_STRONG_TOGETHER_RE = re.compile(TOGETHER_RE.pattern.replace("|with|", "|"), re.IGNORECASE)
# Things that run in the background or that the user stacks freely: sharing time with them needs no question.
BACKGROUND_RE = re.compile(r"\b(dev|audiobook|podcast|music|listen\w*|laundry|dishwasher|dryer|washer|charg\w*|"
                           r"download\w*|render\w*|backups?|soak\w*|marinat\w*|slow ?cook\w*|baking|oven|"
                           r"focus|deep work|tv|show|episode|stream\w*)\b", re.IGNORECASE)
LONG_SESSION_MINUTES = 180  # a session this long is a backdrop the user often stacks things on

_GENERIC_TAILS = {"dev", "session", "practice", "time", "calls", "call", "work", "errands", "chores", "reading"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sharing_rules (
    pair TEXT PRIMARY KEY,
    first TEXT NOT NULL,
    second TEXT NOT NULL,
    together INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def kind_of(title: str) -> str:
    """The kind of item a rule is about: "Redfox dev" and "Thirties dev" are both "dev"; "anything" is any item;
    otherwise the title itself, lowercased ("shower")."""
    words = re.findall(r"[a-z0-9']+", (title or "").lower())
    if not words:
        return ""
    if words in (["anything"], ["everything"], ["any"], ["*"]):
        return "*"
    if len(words) > 1 and words[-1] in _GENERIC_TAILS:
        return words[-1]
    return " ".join(words)


def _pair(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


class SharingRules:
    """What the user said about two kinds of items sharing time ("dev can share with anything", "not the shower
    and a call")."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def set(self, first: str, second: str, together: bool) -> str:
        a, b = kind_of(first), kind_of(second)
        if not a or not b:
            raise ValueError("Name both kinds of item.")
        self._write({"pair": _pair(a, b), "first": a, "second": b, "together": int(together), "updated_at": utc_now_iso()})
        return f"{a if a != '*' else 'anything'} and {b if b != '*' else 'anything'}"

    def verdict(self, a: EventSpan, b: EventSpan) -> Optional[bool]:
        """True: they can share time; False: they can't; None: nothing said. An exact pair wins over "anything"."""
        ka, kb = kind_of(a.title), kind_of(b.title)
        with self.db._get_connection() as conn:
            rows = {r["pair"]: bool(r["together"]) for r in conn.execute(
                "SELECT pair, together FROM sharing_rules WHERE pair IN (?, ?, ?, ?)",
                (_pair(ka, kb), _pair(ka, "*"), _pair(kb, "*"), _pair("*", "*"))).fetchall()}
        for key in (_pair(ka, kb), _pair(ka, "*"), _pair(kb, "*"), _pair("*", "*")):
            if key in rows:
                return rows[key]
        return None

    def _write(self, record: Dict[str, Any]) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO sharing_rules (pair, first, second, together, updated_at) "
                         "VALUES (?, ?, ?, ?, ?)", (record["pair"], record["first"], record["second"],
                                                    int(record["together"]), record["updated_at"]))

    # sync: last write wins
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sharing_rules").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM sharing_rules WHERE pair = ?", (record["pair"],)).fetchone()
        if row and row["updated_at"] >= record["updated_at"]:
            return False
        self._write(record)
        return True


def is_background(span: EventSpan) -> bool:
    return bool(BACKGROUND_RE.search(span.title)) or span.duration_minutes >= LONG_SESSION_MINUTES


def needs_a_word(new: EventSpan, other: EventSpan, rules: Optional[SharingRules]) -> bool:
    """Both need the user's attention, and nothing says they can share time: worth one short question."""
    if other.status != SpanStatus.PLANNED or other.is_envelope:
        return False
    verdict = rules.verdict(new, other) if rules is not None else None
    if verdict is not None:
        return not verdict
    return not (is_background(new) or is_background(other))


def said_together(message: Optional[str]) -> bool:
    return bool(message and TOGETHER_RE.search(message))


def wants_push(message: Optional[str]) -> bool:
    """The user asked for what's there to move ("...and push everything to make room"), and didn't say together
    ("with" alone doesn't count against it: "lunch with Sam, push the rest back")."""
    return bool(message and EXPLICIT_PUSH_RE.search(message) and not _STRONG_TOGETHER_RE.search(message))
