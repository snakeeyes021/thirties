"""Lasting facts about items, by name: what holds every day, not just for one planned span.

``facts.SpanFacts`` belongs to one planned item and moves with it. These belong to
an item's *name* ("Dinner", "Cats' dinner", "Shower"), so they hold for every copy
of it, on every day, on every device (they sync):

- its usual length ("dinner takes an hour": 10-19, 10-26, 10-28);
- a fixed time ("cats' dinner is at 5, it's not allowed to move": 10-27, 10-30);
- what it comes after ("nothing outside until after the shower": 10-17, 10-18);
- what it goes with, in the same stretch ("brush teeth and PT go with the shower": 10-18 to 10-30);
- a time it can't start before, and whether it needs daylight;
- its kind (once / ongoing / daily, see ``kinds.py``), when the user says so.

The model records them when the user says them (``set_item_facts`` with every_day);
Python applies them: new items get the usual length, a change that cuts an item
short or moves a fixed one is refused unless the user's message names the item,
and the redraw keeps them. The model never has to remember them (AGENTS.md §7).

"Outside" is a group, not an item: a fact on it holds for everything outdoors
(``weather.OUTDOOR_RE``, or an item that needs daylight).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from thirties_core.scheduler import StateDatabase, utc_now_iso

OUTSIDE = "@outside"
_OUTSIDE_NAMES = {"outside", "outdoors", "outdoor", "outside things", "outdoor things", "anything outside",
                  "anything outdoors", "going outside", "things outside", "everything outside", OUTSIDE}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_facts (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    minutes INTEGER NOT NULL DEFAULT 0,
    fixed_at TEXT NOT NULL DEFAULT '',
    not_before TEXT NOT NULL DEFAULT '',
    after TEXT NOT NULL DEFAULT '[]',
    goes_with TEXT NOT NULL DEFAULT '[]',
    daylight INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""
_COLUMNS = ["name", "title", "kind", "minutes", "fixed_at", "not_before", "after", "goes_with", "daylight",
            "updated_at"]


def item_name(title: Any) -> str:
    """The key an item's lasting facts are kept under: its words, lower case, without quotes, apostrophes, a
    trailing aside or end punctuation ("Cats' dinner" and "cats dinner." are one item; so are "Dinner" and
    "Dinner (with an audiobook)")."""
    t = str(title or "").strip().lower()
    if t in _OUTSIDE_NAMES:
        return OUTSIDE
    t = re.sub(r"[‘’'`\"“”]", "", t)
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".!?,;:").strip()
    return t


_STOP = {"the", "a", "an", "and", "of", "to", "for", "on", "in", "with", "my", "some", "at", "it", "do", "go",
         "get", "feed"}


def _stem(word: str) -> str:
    for suffix in ("ies", "ing", "es", "s", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def name_words(text: Any) -> set:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", re.sub(r"[‘’'`]", "", str(text or "").lower()))
            if len(w) > 1 and w not in _STOP}


def mentions(message: Optional[str], title: str, others: Iterable[str] = ()) -> bool:
    """Whether the user's message names this item: all of its words, or a word of it that no other item has
    ("feed the cats at 6" names Cats' dinner; "dinner at 7" doesn't, when there's also a Dinner)."""
    if message is None:
        return True  # nobody is talking (a drag in the app, the background service): the user did it
    said, mine = name_words(message), name_words(title)
    if not mine:
        return False
    if mine <= said:
        return True
    shared = set().union(*(name_words(o) for o in others if item_name(o) != item_name(title))) if others else set()
    return bool((mine - shared) & said)


def is_outside(title: str) -> bool:
    from thirties_core.weather import OUTDOOR_RE
    return bool(OUTDOOR_RE.search(title or ""))


def _clock(value: str) -> Optional[time]:
    try:
        h, m = (int(x) for x in value.split(":"))
        return time(h, m)
    except (ValueError, AttributeError):
        return None


@dataclass
class Lasting:
    """What holds every day for one item (by name)."""
    name: str
    title: str = ""                                     # as the user worded it
    kind: str = ""                                      # 'once', 'ongoing', 'daily', or '' (read from its list)
    minutes: int = 0                                    # its usual length; 0 = not said
    fixed_at: str = ""                                  # 'HH:MM' it happens at every day; it never moves
    not_before: str = ""                                # 'HH:MM' it can't start before
    after: List[str] = field(default_factory=list)      # names it comes after
    goes_with: List[str] = field(default_factory=list)  # names it goes with: right after them, in the same stretch
    daylight: bool = False
    updated_at: str = ""

    def is_empty(self) -> bool:
        return not (self.kind or self.minutes or self.fixed_at or self.not_before or self.after or self.goes_with
                    or self.daylight)

    def fixed_time(self, day_start: datetime) -> Optional[datetime]:
        """The fixed time on the day that starts at ``day_start`` (an early-morning time is the next morning's)."""
        return _on_day(self.fixed_at, day_start)

    def not_before_time(self, day_start: datetime) -> Optional[datetime]:
        return _on_day(self.not_before, day_start)

    def describe(self) -> List[str]:
        from thirties_core.clock import format_clock, format_duration
        out = []
        if self.kind:
            out.append({"once": "done once", "ongoing": "ongoing (worked on over time)",
                        "daily": "daily"}.get(self.kind, self.kind))
        if self.minutes:
            out.append(f"takes {format_duration(self.minutes)}")
        for label, value in (("always at", self.fixed_at), ("not before", self.not_before)):
            t = _clock(value) if value else None
            if t is not None:
                out.append(f"{label} {format_clock(datetime(2000, 1, 1, t.hour, t.minute))}")
        if self.after:
            out.append("after " + " and ".join(self.after))
        if self.goes_with:
            out.append("goes with " + " and ".join(self.goes_with))
        if self.daylight:
            out.append("needs daylight")
        return [x for x in out if x]


def _on_day(hhmm: str, day_start: datetime) -> Optional[datetime]:
    t = _clock(hhmm)
    if t is None:
        return None
    at = datetime.combine(day_start.date(), t, tzinfo=day_start.tzinfo)
    return at + timedelta(days=1) if at < day_start else at


class LastingFacts:
    """Lasting facts by item name; synced between devices (last write wins)."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    @staticmethod
    def _row(row) -> Lasting:
        return Lasting(name=row["name"], title=row["title"], kind=row["kind"], minutes=int(row["minutes"] or 0),
                       fixed_at=row["fixed_at"], not_before=row["not_before"], after=json.loads(row["after"] or "[]"),
                       goes_with=json.loads(row["goes_with"] or "[]"), daylight=bool(row["daylight"]),
                       updated_at=row["updated_at"])

    def all(self) -> List[Lasting]:
        with self.db._get_connection() as conn:
            rows = [self._row(r) for r in conn.execute("SELECT * FROM item_facts ORDER BY name").fetchall()]
        return [f for f in rows if not f.is_empty()]

    def get(self, title: Any) -> Optional[Lasting]:
        """The facts kept for this item's own name (not its groups), or None."""
        name = item_name(title)
        if not name:
            return None
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM item_facts WHERE name = ?", (name,)).fetchone()
        facts = self._row(row) if row else None
        return None if facts is None or facts.is_empty() else facts

    def for_item(self, title: Any, *aliases: Any) -> Lasting:
        """Everything that holds for an item: its own facts (under its title, or the first alias that has any,
        e.g. the list item a plan item is linked to), plus those of the groups it's in (outside). Never None."""
        own = next((f for f in (self.get(t) for t in (title,) + aliases if t) if f is not None), None)
        out = Lasting(**{**(own.__dict__ if own else {}), "name": item_name(title)})
        if (is_outside(str(title)) or out.daylight) and item_name(title) != OUTSIDE:
            group = self.get(OUTSIDE)
            if group is not None:
                out.after = sorted(set(out.after) | set(group.after))
                out.daylight = out.daylight or group.daylight
                out.not_before = out.not_before or group.not_before
        return out

    def set(self, facts: Lasting) -> Lasting:
        facts.name = item_name(facts.name or facts.title)
        facts.updated_at = utc_now_iso()
        with self.db._get_connection() as conn:  # an emptied row stays, so clearing a fact syncs too
            conn.execute(f"INSERT OR REPLACE INTO item_facts ({', '.join(_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(_COLUMNS))})", self._values(facts))
        return facts

    @staticmethod
    def _values(f: Lasting) -> list:
        return [f.name, f.title, f.kind, int(f.minutes), f.fixed_at, f.not_before, json.dumps(sorted(set(f.after))),
                json.dumps(sorted(set(f.goes_with))), int(f.daylight), f.updated_at]

    def kind_override(self, title: Any, *aliases: Any) -> str:
        own = next((f for f in (self.get(t) for t in (title,) + aliases if t) if f is not None), None)
        return own.kind if own else ""

    # sync: last write wins (a cleared fact syncs as a row with nothing in it)
    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM item_facts").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM item_facts WHERE name = ?", (record["name"],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            defaults = {"title": "", "kind": "", "minutes": 0, "fixed_at": "", "not_before": "", "after": "[]",
                        "goes_with": "[]", "daylight": 0}
            conn.execute(f"INSERT OR REPLACE INTO item_facts ({', '.join(_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                         [record.get(c, defaults.get(c)) for c in _COLUMNS])
        return True



# --- applying them (called from PlanTools; the arithmetic is here, never in a prompt) ---

def for_new_item(store: Optional[LastingFacts], title: str, aliases: Iterable[Any], start: datetime,
                 end: Optional[datetime], alongside: bool, user_message: Optional[str], others: Iterable[str],
                 day_start: datetime) -> Tuple[datetime, Optional[datetime], List[str]]:
    """A new item gets its lasting facts: its usual length when none was given (or a shorter one the user didn't
    ask for: dinner was planned for 30 minutes on 10-26 and 10-28), and its fixed time when it was put elsewhere
    without the user saying so (the cats' dinner went to 6 on 10-30). Returns (start, end, notes for the result);
    end is None only if no length was given and none is known."""
    if store is None:
        return start, end, []
    from thirties_core.clock import format_clock, format_duration
    facts = store.for_item(title, *aliases)
    said = mentions(user_message, title, list(others))
    notes = []
    if facts.fixed_at and not said:
        at = facts.fixed_time(day_start)
        if at is not None and at != start:
            length = (end - start) if end is not None else None
            start, end = at, (at + length if length is not None else None)
            notes.append(f"at {format_clock(at)}, its fixed time every day")
    usual = timedelta(minutes=facts.minutes) if facts.minutes else None
    if usual is not None and (end is None or (end - start < usual and not alongside and not said)):
        if end is not None:
            notes.append(f"for {format_duration(facts.minutes)}, its usual length")
        end = start + usual
    return start, end, notes


def change_refused(store: Optional[LastingFacts], span: Any, aliases: Iterable[Any], new_start: datetime,
                   new_end: datetime, user_message: Optional[str], others: Iterable[str],
                   day_start: datetime) -> Optional[str]:
    """Why a change to a planned item breaks what the user said holds every day, or None: it would cut the item
    below its usual length, or move an item that has a fixed time. Allowed when the user's message names the item
    (then the user asked for it), like Work and Sleep (PlanTools._envelope_ok)."""
    if store is None or mentions(user_message, span.title, list(others)):
        return None
    from thirties_core.clock import format_clock, format_duration
    facts = store.for_item(span.title, *aliases)
    length = new_end - new_start
    if facts.minutes and length < timedelta(minutes=facts.minutes) and length < span.end_dt - span.start_dt:
        return (f"{span.title} takes {format_duration(facts.minutes)} (the user said so; it holds every day), so it "
                "isn't cut shorter unless the user asks. Keep its length, or ask the user first.")
    if facts.fixed_at and new_start != span.start_dt:
        at = facts.fixed_time(day_start)
        return (f"{span.title} is at {format_clock(at) if at else facts.fixed_at} every day and doesn't move unless "
                "the user asks. Fit the rest around it.")
    return None


def redraw_facts(spans: List[Any], span_facts: Any, store: Optional[LastingFacts], day_start: datetime) -> Dict[str, Any]:
    """The facts the redraw keeps for each planned item: its own (``facts.SpanFacts``) plus what holds for its name
    every day: after and goes-with (as the ids of the day's items with those names), not before, daylight, and a
    fixed time (it stays put)."""
    out = {s.id: span_facts.get(s.id) for s in spans}
    if store is None:
        return out
    by_name: Dict[str, List[str]] = {}
    for s in spans:
        by_name.setdefault(item_name(s.title), []).append(s.id)
    for s in spans:
        if getattr(s, "is_envelope", False):
            continue
        lasting = store.for_item(s.title)
        f = out[s.id]
        before = {i for n in lasting.after + lasting.goes_with for i in by_name.get(item_name(n), []) if i != s.id}
        f.after = sorted(set(f.after) | before)
        f.goes_with = sorted({i for n in lasting.goes_with for i in by_name.get(item_name(n), []) if i != s.id})
        f.daylight = f.daylight or lasting.daylight
        nb = lasting.not_before_time(day_start)
        if nb is not None and (f.not_before is None or nb > f.not_before):
            f.not_before = nb
        f.anchored = f.anchored or bool(lasting.fixed_at)
    return out


def fact_order(spans: List[Any], facts: Dict[str, Any]) -> List[Any]:
    """Planned order, except that an item comes after whatever it has to come after, and an item that goes with
    another comes right after it (10-29: "Shower related items all go in the same block")."""
    ids = {s.id for s in spans}
    out, done = [], set()

    def visit(span, seen=()):
        if span.id in done or span.id in seen:
            return
        for before in facts[span.id].after:
            if before in ids:
                visit(next(s for s in spans if s.id == before), seen + (span.id,))
        done.add(span.id)
        out.append(span)
    for span in spans:
        visit(span)
    for span in [s for s in out if getattr(facts[s.id], "goes_with", None)]:
        partner = next((p for p in out if p.id in facts[span.id].goes_with), None)
        if partner is None:
            continue
        out.remove(span)
        at = out.index(partner) + 1
        while at < len(out) and partner.id in getattr(facts[out[at].id], "goes_with", []):
            at += 1  # several go with the same one: in their planned order
        out.insert(at, span)
    return out


def lasting_lines(store: Optional[LastingFacts]) -> List[str]:
    """What holds every day, one line per item, for a brain's prompt."""
    if store is None:
        return []
    return [f"{'Everything outside' if f.name == OUTSIDE else (f.title or f.name)}: " + ", ".join(f.describe())
            for f in store.all()]



def _names(value: Any) -> List[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else re.split(r"\s*(?:,|;|\band\b|&|\+)\s*", str(value))
    return [str(x).strip() for x in items if str(x).strip()]


def record_lasting(pt: Any, event: Any, after: Any = None, not_before: Any = None, daylight: Any = None,
                   stays_put: Any = None, length: Any = None, at: Any = None, goes_with: Any = None,
                   kind: Any = None) -> str:
    """set_item_facts with every_day: what the user says holds for an item every day, kept by its name. The item
    needn't be planned; if it is, its title is the name. Returns the tool result."""
    from thirties_core.clock import format_clock, format_duration, parse_duration_minutes
    from thirties_core.kinds import KINDS

    def truthy(v: Any) -> bool:
        return str(v).strip().lower() in ("true", "yes", "1") if isinstance(v, str) else bool(v)
    if pt._state_db is None:
        from thirties_core.assistant_tools import ToolError
        raise ToolError("Facts about items can't be saved here.")
    title = str(event or "").strip()
    span = None
    if item_name(title) != OUTSIDE:
        try:
            span = pt._find_span(title)
            title = span.title
        except Exception:
            span = None  # not planned: the name is as the user said it
    if not item_name(title):
        from thirties_core.assistant_tools import ToolError
        raise ToolError("Say which item (its name).")
    store = LastingFacts(pt._state_db)
    facts = store.get(title) or Lasting(name=item_name(title))
    facts.title = facts.title or (title if item_name(title) != OUTSIDE else "outside")
    if kind not in (None, ""):
        k = str(kind).strip().lower()
        if k not in KINDS:
            from thirties_core.assistant_tools import ToolError
            raise ToolError("kind must be 'once', 'ongoing' or 'daily'.")
        facts.kind = k
    if length not in (None, ""):
        facts.minutes = parse_duration_minutes(length)
    if at not in (None, ""):
        facts.fixed_at = pt._start(at).strftime("%H:%M")
    elif stays_put not in (None, ""):
        if not truthy(stays_put):
            facts.fixed_at = ""
        elif span is not None:
            facts.fixed_at = span.start_dt.strftime("%H:%M")
        else:
            from thirties_core.assistant_tools import ToolError
            raise ToolError(f"What time is {title} at every day? Give it as at.")
    if not_before not in (None, ""):
        facts.not_before = pt._start(not_before).strftime("%H:%M")
    if daylight not in (None, ""):
        facts.daylight = truthy(daylight)
    mine = item_name(title)
    facts.after = sorted(set(facts.after) | {n for n in (item_name(x) for x in _names(after)) if n and n != mine})
    facts.goes_with = sorted(set(facts.goes_with) | {n for n in (item_name(x) for x in _names(goes_with))
                                                      if n and n != mine})
    store.set(facts)
    out = f"Noted for {facts.title}, every day: " + (", ".join(facts.describe()) or "nothing special") + "."
    if span is not None and facts.minutes and span.duration_minutes < facts.minutes:
        out += (f" It's planned for {format_duration(span.duration_minutes)} today; lengthen it with update_event if "
                "the user wants today's to match.")
    if span is not None and facts.fixed_at and span.start_dt.strftime("%H:%M") != facts.fixed_at:
        out += (f" Today it's planned at {format_clock(span.start_dt)}; move it with update_event if the user wants "
                "today's at that time too.")
    return out


__all__ = ["Lasting", "LastingFacts", "item_name", "mentions", "is_outside", "OUTSIDE", "for_new_item",
           "change_refused", "redraw_facts", "fact_order", "lasting_lines", "record_lasting"]
