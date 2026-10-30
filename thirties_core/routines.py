"""Routines and daily anchors: the parts of the day that set themselves up (0.2 item 8, B3).

The user's most repeated friction (R1, R2, 12+ days): "Just put them on the stinking schedule! This is your job!"
Each morning Python puts the day's due routines and daily anchors on the plan by itself, where they usually go,
before the morning message; the model never has to remember to, and never asks.

- **A routine** is a note with the "routine" role (a template: it's never written to). Thirties keeps, per routine:
  - its **repeat schedule**: every day, every N days, some weekdays (work days, days off), or only when asked.
    Python counts from the last time it was actually done, not from a fixed date;
  - its **placement**: its parts (which steps go together in one thirty), when each part usually happens, and a
    fallback for a missed part ("after work"). It's learned from how the user places it: the last put_routine
    becomes the placement, unless the user confirmed one ("that's how it always goes"), which then stays until
    they change it. Before any of that, it's read from the last two weeks of plans. Steps that aren't in the note
    (Brush teeth) may be part of it; they're planned with it, never written into the note;
  - **skips**, like a subscription: skip for today (back tomorrow), skip this time (a full cycle later), or skip
    until a day.
- **A daily anchor** is something on no list that happens every day at about the same time (lunch, dinner, the
  cats' dinner at 5). Lunch and dinner are offered once as defaults (DEFAULT_ANCHORS).

These are Thirties' own facts about the user's items (B3: "they live in Thirties' item facts, not in the notes"),
synced between devices like the list roles. Work and Sleep are never moved for them: they sit alongside.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from thirties_core.clock import (ClockError, format_clock, format_duration, parse_clock, parse_day,
                                 parse_duration_minutes, resolve_clock, thirty_ceil, thirty_of)
from thirties_core.models import SpanCategory, SpanStatus
from thirties_core.scheduler import StateDatabase, utc_now_iso
from thirties_core.week import DAYS, describe_days, parse_days, weekday_key, work_days

if TYPE_CHECKING:
    from thirties_core.assistant_tools import AssistantTools
    from thirties_core.models import DayPlan

logger = logging.getLogger(__name__)

# Offered once, as defaults (B3: "daily anchors that aren't on any list are offered once as defaults"). Dinner takes
# an hour (R6, 10-19: "half an hour for dinner will hardly be sufficient").
DEFAULT_ANCHORS = (("Lunch", "12:00", 30), ("Dinner", "18:00", 60))
# How far back the plans are read for a routine that has no placement yet.
HISTORY_DAYS = 14
# Routines whose title says they're occasional are on request by default (B3: "a packing list happens only when
# asked"); every other routine is daily until the user says otherwise.
ON_REQUEST_RE = re.compile(r"\b(pack(ing)?|travel|trip|vacation|moving|guests?|party)\b", re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routine_plans (
    note_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    repeat TEXT NOT NULL DEFAULT '',
    parts TEXT NOT NULL DEFAULT '[]',
    placement_source TEXT NOT NULL DEFAULT '',
    last_done TEXT NOT NULL DEFAULT '',
    skipped_on TEXT NOT NULL DEFAULT '',
    snoozed_until TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_anchors (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    at TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 30,
    fixed INTEGER NOT NULL DEFAULT 0,
    days TEXT NOT NULL DEFAULT '[]',
    removed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


# --- repeat schedules ---

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "twenty": 20, "thirty": 30}


def parse_repeat(text: Any, gen=None) -> Dict[str, Any]:
    """'every day', 'every other day', 'every 12 days', 'weekly', 'weekdays', 'work days', 'days off', 'Mon and
    Thu', 'on request' -> a schedule."""
    raw = re.sub(r"\s+", " ", str(text or "").strip().lower().rstrip("."))
    if raw in ("", "daily", "every day", "everyday", "each day", "every morning", "every evening", "every night"):
        return {"every": "day"}
    if re.search(r"\b(on request|request|when (i )?asks?|when asked|only when asked|as needed|when needed|on demand|"
                 r"never|manual(ly)?|ad hoc)\b", raw):
        return {"every": "asked"}
    if re.fullmatch(r"every (other|second) day|every 2(nd)? days?|alternate days", raw):
        return {"every": "days", "n": 2}
    if re.fullmatch(r"(weekly|every week|once a week)", raw):
        return {"every": "days", "n": 7}
    if re.fullmatch(r"(every other week|biweekly|fortnightly|every 2 weeks|every two weeks)", raw):
        return {"every": "days", "n": 14}
    m = re.fullmatch(r"(?:every|each|once every) (\d+|[a-z]+) (days?|weeks?)", raw)
    if m and (m.group(1).isdigit() or m.group(1) in _NUMBERS):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMBERS[m.group(1)]
        n *= 7 if m.group(2).startswith("week") else 1
        return {"every": "day"} if n <= 1 else {"every": "days", "n": n}
    if re.fullmatch(r"(on )?(my )?(work ?days?|working days|days i work)", raw):
        return {"every": "work_days"}
    if re.fullmatch(r"(on )?(my )?(days? off|off days?|non-?work(ing)? days|free days)", raw):
        return {"every": "days_off"}
    try:
        days = parse_days(re.sub(r"^(on|every) ", "", raw), gen)
    except ValueError:
        raise ValueError(f"Could not read how often '{text}' repeats. Say e.g. 'every day', 'every other day', "
                         "'every 12 days', 'weekdays', 'work days', 'days off', 'Mon and Thu' or 'on request'.")
    return {"every": "day"} if len(days) == 7 else {"every": "weekdays", "days": days}


def describe_repeat(rep: Dict[str, Any]) -> str:
    every = rep.get("every", "day")
    if every == "days":
        n = int(rep.get("n", 1))
        return "every other day" if n == 2 else ("every week" if n == 7 else f"every {n} days")
    return {"day": "every day", "asked": "only when asked", "work_days": "on work days",
            "days_off": "on days off"}.get(every) or f"on {describe_days(rep.get('days') or [])}"


def repeat_days(rep: Dict[str, Any], gen) -> List[str]:
    every = rep.get("every", "day")
    if every == "work_days":
        return work_days(gen)
    if every == "days_off":
        return [d for d in DAYS if d not in work_days(gen)]
    if every == "weekdays":
        return list(rep.get("days") or [])
    return list(DAYS)


def _day_of(text: str) -> Optional[date]:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


@dataclass
class Routine:
    note_id: str
    title: str = ""
    repeat: Dict[str, Any] = field(default_factory=dict)  # empty: the default (daily, or on request by its title)
    parts: List[Dict[str, Any]] = field(default_factory=list)  # [{"items": [...], "at": "HH:MM", "fallback": ""}]
    placement_source: str = ""  # "" (none yet), "learned" (the last put_routine) or "user" (confirmed)
    last_done: str = ""         # said by the user; task history adds what was ticked since
    skipped_on: str = ""        # "skip this time": the next one counts from here
    snoozed_until: str = ""     # "skip today" / "skip until a day": not due before this day
    updated_at: str = ""

    @property
    def schedule(self) -> Dict[str, Any]:
        if self.repeat:
            return self.repeat
        return {"every": "asked"} if ON_REQUEST_RE.search(self.title or "") else {"every": "day"}


def is_due(r: Routine, day: date, gen, last_done: Optional[date]) -> bool:
    """Whether a routine happens on a day, counting from the last time it was really done (or skipped this time)."""
    snoozed = _day_of(r.snoozed_until)
    if snoozed and day < snoozed:
        return False
    skipped = _day_of(r.skipped_on)
    if skipped == day:
        return False
    rep = r.schedule
    every = rep.get("every", "day")
    if every == "asked":
        return False
    if every in ("weekdays", "work_days", "days_off"):
        return weekday_key(day) in repeat_days(rep, gen)
    if every == "days":
        if last_done == day:
            return True
        since = max([d for d in (last_done, skipped) if d and d < day], default=None)
        return since is None or (day - since).days >= max(1, int(rep.get("n", 1)))
    return True


def next_due(r: Routine, after: date, gen, last_done: Optional[date], horizon: int = 400) -> Optional[date]:
    for k in range(1, horizon):
        d = after + timedelta(days=k)
        if is_due(r, d, gen, last_done):
            return d
    return None


@dataclass
class Anchor:
    title: str
    at: str                 # "HH:MM"
    minutes: int = 30
    fixed: bool = False     # set by the clock (the cats' dinner at 5: "that one's not allowed to move", 10-27)
    days: List[str] = field(default_factory=list)  # empty: every day
    removed: bool = False
    updated_at: str = ""

    @property
    def key(self) -> str:
        return self.title.strip().lower()

    def on(self, day: date) -> bool:
        return not self.removed and (not self.days or weekday_key(day) in self.days)


class RoutineBook:
    """Routine schedules, placements and skips, and the daily anchors. Synced, last write wins."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    # --- routines ---

    def get(self, note_id: str, title: str = "") -> Routine:
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT * FROM routine_plans WHERE note_id = ?", (note_id,)).fetchone()
        if not row:
            return Routine(note_id, title)
        return Routine(row["note_id"], title or row["title"], json.loads(row["repeat"] or "{}"),
                       json.loads(row["parts"] or "[]"), row["placement_source"], row["last_done"], row["skipped_on"],
                       row["snoozed_until"], row["updated_at"])

    def save(self, r: Routine) -> None:
        r.updated_at = utc_now_iso()
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO routine_plans (note_id, title, repeat, parts, placement_source, last_done,
                skipped_on, snoozed_until, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (r.note_id, r.title, json.dumps(r.repeat), json.dumps(r.parts), r.placement_source, r.last_done,
                  r.skipped_on, r.snoozed_until, r.updated_at))

    # --- anchors ---

    def anchors(self, include_removed: bool = False) -> List[Anchor]:
        with self.db._get_connection() as conn:
            rows = conn.execute("SELECT * FROM daily_anchors ORDER BY at").fetchall()
        out = [Anchor(r["title"], r["at"], r["minutes"], bool(r["fixed"]), json.loads(r["days"] or "[]"),
                      bool(r["removed"]), r["updated_at"]) for r in rows]
        return out if include_removed else [a for a in out if not a.removed]

    def save_anchor(self, a: Anchor) -> None:
        a.updated_at = utc_now_iso()
        with self.db._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO daily_anchors (key, title, at, minutes, fixed, days, removed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (a.key, a.title.strip(), a.at, int(a.minutes), int(a.fixed), json.dumps(a.days), int(a.removed),
                  a.updated_at))

    # --- sync ---

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return ([dict(r, kind="routine") for r in conn.execute("SELECT * FROM routine_plans").fetchall()]
                    + [dict(r, kind="anchor") for r in conn.execute("SELECT * FROM daily_anchors").fetchall()])

    def merge_record(self, record: Dict[str, Any]) -> bool:
        if record.get("kind") == "anchor":
            table, key = "daily_anchors", "key"
            cols = ["key", "title", "at", "minutes", "fixed", "days", "removed", "updated_at"]
        else:
            table, key = "routine_plans", "note_id"
            cols = ["note_id", "title", "repeat", "parts", "placement_source", "last_done", "skipped_on",
                    "snoozed_until", "updated_at"]
        with self.db._get_connection() as conn:
            row = conn.execute(f"SELECT updated_at FROM {table} WHERE {key} = ?", (record[key],)).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [record.get(c, "") for c in cols])
        return True


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _clock_of(hhmm: str) -> str:
    """A stored 'HH:MM' (24-hour) as the user reads it: '3:00 PM'."""
    return format_clock(datetime.strptime(hhmm, "%H:%M"))


def _at(hhmm: str, day_start: datetime) -> datetime:
    """A stored 'HH:MM' on the diurnal day that starts at day_start."""
    return resolve_clock(_clock_of(hhmm), day_start)


def _as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return value if isinstance(value, list) else [value]


def _true(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


ROUTINE_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "set_routine",
        "description": "Record how one of the user's routines goes, when they say so: how often it repeats, when it "
                       "was last done, its usual placement, where a part goes if it's missed, or (confirm) that "
                       "today's placement is how it always goes. Routines go on the plan by themselves each morning.",
        "parameters": {"routine": "Which routine (its note's name).",
                       "repeat": "'every day', 'every other day', 'every 12 days', 'weekly', 'weekdays', 'work days', "
                                 "'days off', 'Mon and Thu' or 'on request'.",
                       "last_done": "When it was last done, if the user says (e.g. 'Oct 20', 'yesterday').",
                       "parts": "Its usual placement, as a list of {start, items} like put_routine's.",
                       "confirm": "true when the user says the way it's on the plan today is how it always goes.",
                       "fallback": "Where a missed part goes, in the user's words (e.g. 'after work on work days'); "
                                   "one for every part, or a list with one per part."},
        "required": ["routine"],
    },
    {
        "name": "skip_routine",
        "description": "Skip a routine, the way the user picks: 'today' (it comes back tomorrow), 'this_time' (the "
                       "next one is a full cycle later) or 'until' a day. If they didn't say which, call it without "
                       "how: it returns the three choices with their dates, to ask in one short question.",
        "parameters": {"routine": "Which routine.", "how": "'today', 'this_time' or 'until'.",
                       "until": "With 'until': the day it comes back, e.g. 'Monday' or 'Nov 3'.",
                       "day": "The day being skipped, if not today."},
        "required": ["routine"],
    },
    {
        "name": "set_anchor",
        "description": "A daily anchor: something on no list that happens every day (or some days) at about the same "
                       "time, like lunch, dinner or feeding a pet at 5. It goes on the plan by itself every morning. "
                       "Record one when the user says so or agrees to the offer; remove=true stops it; decline=true "
                       "when they don't want the offered ones.",
        "parameters": {"title": "What it is, in the user's words (e.g. 'Cats' dinner').",
                       "time": "Its usual clock time, e.g. '5:00 PM'.",
                       "duration": "How long it takes (default 30 minutes).",
                       "fixed": "true if it keeps its exact time and never slides (the cats' dinner at 5).",
                       "days": "Which days, if not every day ('weekdays', 'work days', 'Sat and Sun').",
                       "remove": "true to stop it.", "decline": "true: the user doesn't want the offered anchors."},
        "required": [],
    },
    {
        "name": "set_week",
        "description": "Record the user's usual work hours or sleep, for some weekdays or all of them (\"I work 7–3 "
                       "on weekdays, off weekends\", \"I sleep in until 9 on Saturdays\"). Work and Sleep on the plan "
                       "follow it from today on, except where they were changed by hand.",
        "parameters": {"days": "Which days: 'weekdays', 'weekends', 'every day', 'Mon-Thu', 'Friday'.",
                       "work_start": "Clock time work starts.", "work_end": "Clock time work ends.",
                       "no_work": "true if those are days off.",
                       "bedtime": "Clock time they go to bed on those days.",
                       "wake": "Clock time they get up on those days."},
        "required": ["days"],
    },
]


class RoutineTools:
    """The routine and anchor tools, and the morning set-up, on top of an AssistantTools."""

    def __init__(self, assistant: "AssistantTools") -> None:
        self.a = assistant
        self.placing = False  # the morning set-up is placing: put_routine doesn't learn from it

    # --- helpers ---

    @property
    def pt(self):
        return self.a.pt

    @property
    def db(self) -> Optional[StateDatabase]:
        return self.a._db

    @property
    def gen(self):
        return self.pt.scheduler.config.general if self.pt.scheduler else None

    def book(self) -> Optional[RoutineBook]:
        return RoutineBook(self.db) if self.db is not None else None

    def _note(self, name: str) -> Dict[str, Any]:
        from thirties_core.assistant_tools import ToolError, _same_words, _stem, _words
        routines = self.a._routines()
        found = [n for n in routines if _same_words(n["title"], name)]
        if not found:
            want = {_stem(w) for w in _words(name)} - {"routine"}
            found = [n for n in routines if want and want <= {_stem(w) for w in _words(n["title"])}] or \
                [n for n in routines if want & {_stem(w) for w in _words(n["title"])}]
        if len(found) != 1:
            names = ", ".join(n["title"] for n in routines) or "none yet (mark a note as a routine in Profile → Lists)"
            raise ToolError(f"Which routine? Routines: {names}.")
        return found[0]

    @staticmethod
    def steps(note: Dict[str, Any]) -> List[str]:
        from thirties_core.today import note_items
        return [text for text, _ in note_items(note)]

    def last_done(self, note: Dict[str, Any], r: Optional[Routine] = None) -> Optional[date]:
        """The last day it was really done: what the user said, or the latest tick of any of its steps."""
        from thirties_core.assistant_tools import _checkbox_id
        r = r or self.book().get(note["id"], note["title"])
        days = [_day_of(r.last_done)]
        for step in self.steps(note):
            rec = self.db.get_task_history(_checkbox_id(note["id"], step))
            days.append(_day_of(rec.completed_date) if rec and rec.completed_date else None)
        return max((d for d in days if d), default=None)

    # --- placement ---

    def parts_for(self, note: Dict[str, Any], r: Optional[Routine] = None, before: Optional[date] = None
                  ) -> Tuple[List[Dict[str, Any]], str]:
        """The routine's usual placement and where it came from: stored, or read from recent plans."""
        r = r or self.book().get(note["id"], note["title"])
        if r.parts:
            return r.parts, r.placement_source or "learned"
        parts = self.placement_from_history(note, before or self.pt.day_plan.target_date)
        return parts, "history" if parts else ""

    def placement_from_history(self, note: Dict[str, Any], before: date) -> List[Dict[str, Any]]:
        """For a routine with no placement yet: each step at the thirty it was most often planned in over the last
        two weeks (ties: the latest), grouped into parts in the note's order. On 0.2's first morning this is the
        pattern the user built by hand since 10-18."""
        from thirties_core.assistant_tools import _checkbox_id
        if self.pt.scheduler is None:
            return []
        steps = self.steps(note)
        by_id = {_checkbox_id(note["id"], s): s for s in steps}
        by_title = {s.lower(): s for s in steps}
        tz = self.pt.day_plan.day_start.tzinfo
        end = datetime.combine(before, datetime.min.time(), tzinfo=tz)
        seen: Dict[str, List[Tuple[datetime, str]]] = {}
        for span in self.db.get_spans_between(end - timedelta(days=HISTORY_DAYS), end):
            step = by_id.get(span.task_id or "") or by_title.get(span.title.strip().lower())
            if step and span.status != SpanStatus.SKIPPED and not span.is_envelope:
                start = span.planned_start or span.start_dt
                seen.setdefault(step, []).append((start, _hhmm(thirty_of(start.astimezone(tz)))))
        at: Dict[str, str] = {}
        for step, times in seen.items():
            counts = Counter(t for _, t in times)
            best = max(counts.values())
            at[step] = max((when, t) for when, t in times if counts[t] == best)[1]
        groups: Dict[str, List[str]] = {}
        for step in steps:
            if step in at:
                groups.setdefault(at[step], []).append(step)
        return [{"items": items, "at": t, "fallback": ""} for t, items in sorted(groups.items())]

    def learn(self, note: Dict[str, Any], parts: List[Dict[str, Any]]) -> None:
        """How the user placed it (a put_routine) becomes its usual placement, unless they confirmed one. Steps the
        call didn't mention keep their old part, so placing one part doesn't lose the other."""
        if self.placing or self.db is None:
            return
        book = self.book()
        r = book.get(note["id"], note["title"])
        if r.placement_source == "user":
            return
        new = self._normalize_parts(note, parts)
        if not new:
            return
        r.parts, r.placement_source = self._merge_parts(r.parts, new), "learned"
        book.save(r)

    def _normalize_parts(self, note: Dict[str, Any], parts: Any) -> List[Dict[str, Any]]:
        from thirties_core.assistant_tools import ToolError
        by_text = {s.lower(): s for s in self.steps(note)}
        out = []
        for part in _as_list(parts):
            if not isinstance(part, dict):
                raise ToolError("parts must be a list of {start, items}.")
            items = [by_text.get(str(n).strip().lower(), str(n).strip()) for n in (part.get("items") or list(by_text.values()))
                     if str(n).strip()]
            when = part.get("start") or part.get("at")
            if when in (None, "") or not items:
                continue
            if re.fullmatch(r"\d{2}:\d{2}", str(when).strip()):
                when = _clock_of(str(when).strip())  # a stored part's "15:00"
            try:
                start = thirty_of(self.pt._start(when))
            except (ClockError, ValueError) as e:
                raise ToolError(f"Couldn't read the time '{when}': {e}")
            out.append({"items": items, "at": _hhmm(start), "fallback": str(part.get("fallback") or "")})
        return out

    @staticmethod
    def _merge_parts(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        named = {i.lower() for p in new for i in p["items"]}
        kept = []
        for p in old:
            items = [i for i in p["items"] if i.lower() not in named]
            if items:
                kept.append({**p, "items": items})
        for p in new:  # a part at an old part's time keeps that part's fallback unless it brings one
            if not p.get("fallback"):
                p["fallback"] = next((o.get("fallback", "") for o in old if o.get("at") == p["at"]), "")
        merged: Dict[str, Dict[str, Any]] = {}
        for p in kept + new:
            if p["at"] in merged:
                merged[p["at"]]["items"] += [i for i in p["items"] if i not in merged[p["at"]]["items"]]
            else:
                merged[p["at"]] = {"items": list(p["items"]), "at": p["at"], "fallback": p.get("fallback", "")}
        return sorted(merged.values(), key=lambda p: p["at"])

    def known_extras(self, note: Dict[str, Any]) -> set:
        """Steps the user places with the routine that aren't in its note (Brush teeth), lowercased."""
        if self.db is None:
            return set()
        steps = {s.lower() for s in self.steps(note)}
        return {i.lower() for p in self.book().get(note["id"]).parts for i in p["items"] if i.lower() not in steps}

    def daily_titles(self) -> set:
        """Titles that happen again by themselves (anchors, routines and their extra steps): they never carry over
        to the next day, and yesterday's copies leave the Today note."""
        if self.db is None:
            return set()
        book = self.book()
        out = {a.key for a in book.anchors()}
        for note in self.a._routines():
            out.add(note["title"].strip().lower())
            out |= {i.lower() for p in book.get(note["id"]).parts for i in p["items"]}
        return out

    # --- the morning set-up ---

    def _setup_key(self, day: date) -> str:
        return f"day_set_up:{day.isoformat()}"

    def set_up_state(self, day: date) -> Dict[str, Any]:
        raw = self.db.get_sync_meta(self._setup_key(day)) if self.db is not None else None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def on_today_for(self, day: date) -> set:
        """What the set-up put on the Today note for that day (the morning reset leaves it there)."""
        return {t.lower() for t in self.set_up_state(day).get("on_today", [])}

    @staticmethod
    def _bedtime(plan: "DayPlan") -> datetime:
        return min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start
                    and s.status != SpanStatus.SKIPPED), default=plan.day_end)

    def set_up_current(self) -> str:
        """Before a turn: set up the day being lived, which near sunrise is the coming one (planning at 6:30 on a
        dark morning is planning that day)."""
        if self.db is None or self.pt.scheduler is None:
            return ""
        out = [self.set_up_day(self.pt.day_plan)]
        if self.pt._now() >= self.pt.day_plan.day_end - timedelta(hours=3):
            out.append(self.set_up_day(self.pt.plan_for(self.pt.day_plan.target_date + timedelta(days=1))))
        return " ".join(o for o in out if o)

    def set_up_day(self, plan: Optional["DayPlan"] = None) -> str:
        """Once per day, before the morning message: the day's due routines and its anchors go on the plan, where
        they usually go, without asking (R1). Work and Sleep stay as they are; things sit alongside them. A part
        whose time has passed goes in the first free thirty from now (on a work day that's after work). Returns a
        short summary for the morning message, or ''."""
        plan = plan or self.pt.day_plan
        if self.db is None or self.pt.scheduler is None or not self.pt.is_current(plan):
            return ""
        key = self._setup_key(plan.target_date)
        if self.db.get_sync_meta(key):
            return ""
        now = self.pt._now()
        if now >= self._bedtime(plan) - timedelta(minutes=30):
            return ""  # that day's waking hours are over (6:30 AM: yesterday's day isn't set up any more)
        self.db.set_sync_meta(key, json.dumps({"state": "running"}))  # first: placing writes to Today, which asks
        saved, self.pt.day_plan, self.placing = self.pt.day_plan, plan, True
        state: Dict[str, Any] = {"state": "done", "placed": [], "moved": [], "unplaced": [], "on_today": []}
        try:
            self.a._day_begun()  # the morning reset of the Today note comes before anything is written to it
            self._place_routines(plan, now, state)
            self._place_anchors(plan, now, state)
        except Exception as e:  # never in the way of the user's turn
            logger.exception("Setting up %s failed: %s", plan.target_date, e)
        finally:
            self.pt.day_plan, self.placing = saved, False
            self.db.set_sync_meta(key, json.dumps(state))
        if state["placed"]:
            logger.info("Set up %s: %s", plan.target_date, "; ".join(state["placed"]))
        return self.summary(state)

    @staticmethod
    def summary(state: Dict[str, Any]) -> str:
        if not state.get("placed"):
            return ""
        out = "On today by themselves: " + "; ".join(state["placed"]) + "."
        if state.get("moved"):
            out += " " + " ".join(state["moved"])
        return out

    def _spot(self, plan: "DayPlan", at: str, now: datetime, minutes: int = 30) -> Tuple[Optional[datetime], str]:
        """Where a part (or anchor) goes today: its usual time; past a sleep-in, when sleep ends; once its time has
        passed, the first free thirty from now (None if nothing's free before bed)."""
        start = _at(at, plan.day_start)
        bed = self._bedtime(plan)
        if start >= bed and start.hour < 12 and start.date() > plan.day_start.date():
            start = thirty_ceil(plan.day_start)  # before sunrise (6:30 AM in winter): the day's first thirty
        for s in plan.spans:  # sleeping in (a weekend): when this morning's sleep ends
            if (s.category == SpanCategory.SLEEP and s.start_dt <= start < s.end_dt
                    and s.end_dt <= plan.day_start + timedelta(hours=12)):
                start = thirty_ceil(s.end_dt)
        if start + timedelta(minutes=minutes) > now:
            return start, ""
        for a, b in self.pt.open_windows(after=now):
            a = max(a, thirty_ceil(now)) if a < now else a
            if b - a >= timedelta(minutes=30) and a + timedelta(minutes=30) <= bed:
                return a, "missed"
        return None, "missed"

    def _place_routines(self, plan: "DayPlan", now: datetime, state: Dict[str, Any]) -> None:
        from thirties_core.assistant_tools import ToolError
        book = self.book()
        for note in self.a._routines():
            r = book.get(note["id"], note["title"])
            if not is_due(r, plan.target_date, self.gen, self.last_done(note, r)):
                continue
            parts, _ = self.parts_for(note, r, plan.target_date)
            if not parts:
                state["unplaced"].append(f"{note['title']}: no usual time yet")
                continue
            ahead = [(p, self._spot(plan, p["at"], now)) for p in parts]
            ahead.sort(key=lambda x: x[1][1] == "missed")  # parts still ahead first: missed ones fill in after
            times = []
            for part, (start, why) in ahead:
                if why == "missed":
                    start, why = self._spot(plan, part["at"], now)  # what's free now that the others are on
                if start is None:
                    state["unplaced"].append(f"{note['title']} ({', '.join(part['items'])}): no free time left")
                    continue
                try:
                    out = self.a.put_routine(routine=note["title"], parts=[{"start": format_clock(start),
                                                                            "items": part["items"]}])
                except ToolError as e:
                    state["unplaced"].append(f"{note['title']}: {e}")
                    continue
                if out.endswith("is already on the plan."):
                    continue  # placed by hand already
                times.append(format_clock(start))
                if why == "missed":
                    state["moved"].append(f"({', '.join(part['items'][:2])}{'…' if len(part['items']) > 2 else ''} "
                                          f"usually goes at {_clock_of(part['at'])}; that's passed, so it's at "
                                          f"{format_clock(start)}.)")
            if times:
                state["placed"].append(f"{note['title']} at " + " and ".join(times))
                state["on_today"].append(note["title"].strip())

    def _place_anchors(self, plan: "DayPlan", now: datetime, state: Dict[str, Any],
                       anchors: Optional[List[Anchor]] = None) -> None:
        from thirties_core.plan_tools import NOT_DONE
        planned = {s.title.strip().lower() for s in plan.spans if s.status != SpanStatus.SKIPPED}
        for anchor in self.book().anchors() if anchors is None else anchors:
            if not anchor.on(plan.target_date) or anchor.key in planned:
                continue
            start = _at(anchor.at, plan.day_start)
            if start + timedelta(minutes=anchor.minutes) <= now or any(
                    s.category == SpanCategory.SLEEP and s.start_dt <= start < s.end_dt for s in plan.spans):
                continue  # its time has passed (it's moot), or it falls in sleep on this day
            args = {"title": anchor.title, "start": format_clock(start), "duration": f"{anchor.minutes} minutes",
                    "fixed": anchor.fixed}
            out = self.pt.execute("add_event", args)
            if out.startswith(NOT_DONE) and not anchor.fixed:
                near = self._near(start, anchor.minutes)  # its thirty is taken: the nearest free one within the hour
                if near is not None:
                    out = self.pt.execute("add_event", {**args, "start": format_clock(near)})
                    start = near
            if out.startswith(NOT_DONE):
                out = self.pt.execute("add_event", {**args, "alongside": True})
            if out.startswith(NOT_DONE):
                state["unplaced"].append(f"{anchor.title}: {out[len(NOT_DONE):].lstrip(': ')}")
                continue
            state["placed"].append(f"{anchor.title} at {format_clock(start)}")
            state["on_today"].append(anchor.title.strip())

    def _near(self, start: datetime, minutes: int) -> Optional[datetime]:
        need = timedelta(minutes=minutes)
        options = []
        for a, b in self.pt.open_windows():
            for t in (max(a, start - timedelta(hours=1)), min(b - need, start + timedelta(hours=1))):
                if a <= t and t + need <= b and abs(t - start) <= timedelta(hours=1) and t == thirty_of(t):
                    options.append(t)
        return min(options, key=lambda t: (abs(t - start), t)) if options else None

    # --- what the model sees ---

    def anchor_offer_due(self) -> bool:
        """The default anchors haven't been offered yet (they're offered once, on the first day it comes up)."""
        if self.db is None or self.book().anchors(include_removed=True):
            return False
        offered = self.db.get_sync_meta("anchors_offered")
        return not offered or offered == self.a._today().isoformat()

    def prompt_section(self) -> str:
        """ROUTINES AND DAILY ANCHORS for the Frontier prompt: each routine's schedule, placement and today's state,
        the anchors, the week's work and sleep, and the one-time offer of default anchors."""
        if self.db is None or self.pt.scheduler is None:
            return ""
        plan, book = self.pt.day_plan, self.book()
        lines = []
        on_plan = {s.title.strip().lower(): s for s in plan.spans if s.status != SpanStatus.SKIPPED}
        for note in self.a._routines():
            r = book.get(note["id"], note["title"])
            done = self.last_done(note, r)
            due = is_due(r, plan.target_date, self.gen, done)
            parts, source = self.parts_for(note, r, plan.target_date)
            line = f"- {note['title']}: {describe_repeat(r.schedule)}"
            if r.schedule.get("every") == "days":
                upcoming = next_due(r, plan.target_date - timedelta(days=0 if done == plan.target_date else 1),
                                    self.gen, done)  # done today: the next one
                line += f"; last done {done.strftime('%b %-d') if done else 'unknown'}" + (
                    f"; next due {upcoming.strftime('%a %b %-d')}" if upcoming and upcoming != plan.target_date else "")
            if parts:
                how = {"user": "confirmed by the user", "learned": "learned from how it was last placed",
                       "history": "read from recent plans"}.get(source, source)
                line += f". Usual placement ({how}): " + "; ".join(
                    f"{_clock_of(p['at'])}: {', '.join(p['items'])}" + (f" (if missed: {p['fallback']})"
                                                                        if p.get("fallback") else "") for p in parts)
            else:
                line += ". No usual time yet: when the user places it, that becomes its placement"
            if r.snoozed_until and plan.target_date < _day_of(r.snoozed_until):
                line += f". Skipped until {_day_of(r.snoozed_until).strftime('%a %b %-d')}"
            if due:
                there = [p for p in parts if any(i.lower() in on_plan for i in p["items"])]
                line += ". Today: " + ("on the plan" if there and len(there) == len(parts) else
                                       "partly on the plan" if there else "due, not on the plan")
            else:
                line += ". Not due today"
            lines.append(line + ".")
        anchors = book.anchors()
        if anchors:
            lines.append("Daily anchors: " + "; ".join(
                f"{a.title} {_clock_of(a.at)} ({format_duration(a.minutes)}{', fixed' if a.fixed else ''}"
                f"{', ' + describe_days(a.days) if a.days else ''})" for a in anchors) + ".")
        state = self.set_up_state(plan.target_date)
        if state.get("placed"):
            lines.append("Put on this morning by Python: " + "; ".join(state["placed"]) + ".")
        if state.get("unplaced"):
            lines.append("Couldn't be placed: " + "; ".join(state["unplaced"]) + ".")
        if self.anchor_offer_due():
            if not self.db.get_sync_meta("anchors_offered"):
                self.db.set_sync_meta("anchors_offered", self.a._today().isoformat())
            lines.append("OFFER ONCE (today only): no daily anchors yet. Offer to put these on every day by "
                         "themselves: " + ", ".join(f"{t} at {_clock_of(at)} ({format_duration(m)})"
                                                     for t, at, m in DEFAULT_ANCHORS)
                         + ", and ask what else happens at a set time each day (like feeding a pet). Record each "
                           "with set_anchor; if they don't want them, set_anchor decline=true.")
        if not lines and not getattr(self.gen, "week", None):
            return ""
        from thirties_core.week import describe_week
        lines.append("Usual week (work and sleep; change with set_week):\n" + describe_week(self.gen))
        return ("ROUTINES AND DAILY ANCHORS (each morning Python puts the due ones on the plan by itself, at their "
                "usual times; never ask whether to add them)\n" + "\n".join(lines) + "\n\n")

    def skip_choices(self, note_ref: Any, day: Optional[date] = None) -> List[Dict[str, Any]]:
        """The three ways to skip, with the day each brings the routine back (for buttons in the app, and the
        question in chat)."""
        note = note_ref if isinstance(note_ref, dict) else self._note(str(note_ref))
        day = day or self.pt.day_plan.target_date
        r = self.book().get(note["id"], note["title"])
        done = self.last_done(note, r)
        back_today = next_due(replace(r, snoozed_until=(day + timedelta(days=1)).isoformat()), day, self.gen, done)
        back_cycle = next_due(replace(r, skipped_on=day.isoformat()), day, self.gen, done)
        return [{"how": "today", "label": "Skip today", "back": back_today},
                {"how": "this_time", "label": "Skip this time", "back": back_cycle},
                {"how": "until", "label": "Skip until…", "back": None}]

    def day_routines(self, plan: Optional["DayPlan"] = None) -> List[Dict[str, Any]]:
        """The routines a day holds that can still be skipped, for the app's Agenda: each due one, and each on the
        plan (one put on by request too), unless every step of it on the plan is already marked. Each comes with
        its state in a few words and its three skip choices (skip_choices)."""
        if self.db is None or self.pt.scheduler is None:
            return []
        plan = plan or self.pt.day_plan
        day, book = plan.target_date, self.book()
        out = []
        for note in self.a._routines():
            r = book.get(note["id"], note["title"])
            names = {s.lower() for s in self.steps(note)} | {i.lower() for p in r.parts for i in p["items"]}
            steps = [s for s in plan.spans if not s.is_envelope and s.title.strip().lower() in names]
            planned = [s for s in steps if s.status == SpanStatus.PLANNED]
            due = is_due(r, day, self.gen, self.last_done(note, r))
            if not planned and (steps or not due):
                continue  # all marked (done or skipped), or not today's
            state = (f"On the plan at {format_clock(min(s.start_dt for s in planned))}" if planned
                     else "Due, not on the plan yet")
            out.append({"title": note["title"], "note_id": note["id"], "state": state,
                        "choices": self.skip_choices(note, day)})
        return out

    # --- tools ---

    def set_routine(self, routine: str = "", repeat: Any = None, last_done: Any = None, parts: Any = None,
                    confirm: Any = None, fallback: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        if self.db is None:
            raise ToolError("Routines can't be saved here.")
        note = self._note(routine)
        book = self.book()
        r = book.get(note["id"], note["title"])
        said = []
        if repeat not in (None, ""):
            try:
                r.repeat = parse_repeat(repeat, self.gen)
            except ValueError as e:
                raise ToolError(str(e))
            said.append(f"it repeats {describe_repeat(r.repeat)}")
        if last_done not in (None, ""):
            try:
                r.last_done = parse_day(str(last_done), self.a._today()).isoformat()
            except ClockError as e:
                raise ToolError(str(e))
            said.append(f"last done {_day_of(r.last_done).strftime('%a %b %-d')}")
        if parts not in (None, "", []):
            new = self._normalize_parts(note, parts)
            if not new:
                raise ToolError("Give parts as a list of {start, items}.")
            r.parts, r.placement_source = new, "user"
            said.append("its usual placement")
        elif _true(confirm):
            r.parts, r.placement_source = self._from_plan(note, r), "user"
            if not r.parts:
                raise ToolError(f"{note['title']} isn't on this day's plan, so there's no placement to keep. Give parts.")
            said.append("today's placement as the usual one")
        if fallback not in (None, ""):
            fb = _as_list(fallback) if isinstance(fallback, list) or str(fallback).strip().startswith("[") else [fallback]
            if not r.parts:
                r.parts, src = self.parts_for(note, r)
                r.placement_source = r.placement_source or ("learned" if src == "history" else src)
            if not r.parts:
                raise ToolError(f"{note['title']} has no usual placement yet; give parts first.")
            for k, p in enumerate(r.parts):
                p["fallback"] = str(fb[k] if k < len(fb) else fb[-1]).strip()
            said.append("where a missed part goes")
        if not said:
            raise ToolError("Say what to record: repeat, last_done, parts, confirm or fallback.")
        book.save(r)
        out = f"Noted for {note['title']}: " + ", ".join(said) + "."
        if r.parts and ("its usual placement" in said or "today's placement as the usual one" in said):
            out += " Usual placement: " + "; ".join(f"{_clock_of(p['at'])}: {', '.join(p['items'])}" for p in r.parts) + "."
        if r.schedule.get("every") == "days":
            upcoming = next_due(r, self.pt.day_plan.target_date - timedelta(days=1), self.gen, self.last_done(note, r))
            if upcoming:
                out += f" Next due {upcoming.strftime('%a %b %-d')}."
        return out

    def _from_plan(self, note: Dict[str, Any], r: Routine) -> List[Dict[str, Any]]:
        """The routine as it is on the day's plan, as parts: each thirty holding its steps (and the extra steps
        the user places with it)."""
        names = {s.lower(): s for s in self.steps(note)}
        names.update({i.lower(): i for p in r.parts for i in p["items"]})
        groups: Dict[str, List[str]] = {}
        for span in sorted(self.pt.day_plan.spans, key=lambda s: s.start_dt):
            name = names.get(span.title.strip().lower())
            if name and span.status != SpanStatus.SKIPPED and not span.is_envelope:
                groups.setdefault(_hhmm(thirty_of(span.start_dt)), []).append(name)
        old = {p["at"]: p.get("fallback", "") for p in r.parts}
        return [{"items": items, "at": at, "fallback": old.get(at, "")} for at, items in sorted(groups.items())]

    def skip_routine(self, routine: str = "", how: str = "", until: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        if self.db is None:
            raise ToolError("Routines can't be saved here.")
        note = self._note(routine)
        day = self.pt.day_plan.target_date
        how = re.sub(r"[\s-]+", "_", str(how or "").strip().lower())
        how = {"for_today": "today", "today_only": "today", "this_one": "this_time", "once": "this_time",
               "cycle": "this_time", "until_a_day": "until"}.get(how, how)
        if how == "" and until not in (None, ""):
            how = "until"
        if how not in ("today", "this_time", "until"):
            choices = self.skip_choices(note, day)
            back = {c["how"]: c["back"] for c in choices}

            def when(d: Optional[date]) -> str:
                return d.strftime("%a %b %-d") if d else "only when asked"
            raise ToolError(f"Ask the user which, in one short question: skip {note['title']} today (it's back "
                            f"{when(back['today'])}), skip this time (the next one is {when(back['this_time'])}), "
                            "or skip it until a day they pick. Then call again with how.")
        book = self.book()
        r = book.get(note["id"], note["title"])
        if how == "today":
            r.snoozed_until = (day + timedelta(days=1)).isoformat()
        elif how == "this_time":
            r.skipped_on = day.isoformat()
        else:
            if until in (None, ""):
                raise ToolError("Until which day? Give until.")
            try:
                back = parse_day(str(until), self.a._today())
            except ClockError as e:
                raise ToolError(str(e))
            if back <= day:
                raise ToolError(f"{back.strftime('%a %b %-d')} isn't after {day.strftime('%a %b %-d')}.")
            r.snoozed_until = back.isoformat()
        book.save(r)
        off = self._off_the_day(note, r)
        upcoming = next_due(r, day, self.gen, self.last_done(note, r))
        return (f"Skipped {note['title']}" + (f" ({off})" if off else "") + ". "
                + (f"Next: {upcoming.strftime('%a %b %-d')}." if upcoming else "It's next only when asked."))

    def _off_the_day(self, note: Dict[str, Any], r: Routine) -> str:
        """A skipped routine's planned steps on the day are marked skipped (they didn't happen; a daily thing
        skipped isn't "put off", B2), and its group leaves the Today note."""
        names = {s.lower() for s in self.steps(note)} | {i.lower() for p in r.parts for i in p["items"]}
        steps = [s for s in self.pt.day_plan.spans if s.status == SpanStatus.PLANNED and not s.is_envelope
                 and s.title.strip().lower() in names]
        for s in steps:
            s.status = SpanStatus.SKIPPED
            self.pt._persist(s)
        if steps and self.pt.is_current(self.pt.day_plan):
            today, writer = self.a._today_note(), self.a._writer()
            if today is not None and writer is not None:
                writer.submit({"op": "remove", "note_id": today["id"], "title": note["title"].strip()})
                self.a._today_groups.pop((self.a._today().isoformat(), note["title"].strip().lower()), None)
        return f"{len(steps)} planned step{'s' if len(steps) != 1 else ''} off today's plan" if steps else ""

    def set_anchor(self, title: str = "", time: Any = None, duration: Any = None, fixed: Any = None, days: Any = None,
                   remove: Any = None, decline: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        if self.db is None:
            raise ToolError("Anchors can't be saved here.")
        self.db.set_sync_meta("anchors_offered", "answered")
        if _true(decline) and not str(title or "").strip():
            return "Noted: no daily anchors for now."
        if not str(title or "").strip():
            raise ToolError("Give the anchor's title.")
        book = self.book()
        current = next((a for a in book.anchors(include_removed=True) if a.key == str(title).strip().lower()), None)
        if _true(remove):
            if current is None or current.removed:
                raise ToolError(f"There's no daily anchor '{title}'.")
            current.removed = True
            book.save_anchor(current)
            return f"{current.title} won't go on the plan by itself any more."
        anchor = current or Anchor(str(title).strip(), "")
        anchor.removed = False
        if time not in (None, ""):
            try:
                anchor.at = parse_clock(str(time)).strftime("%H:%M")
            except ClockError as e:
                raise ToolError(str(e))
        if not anchor.at:
            raise ToolError(f"What time is {title} usually? Give time.")
        if duration not in (None, ""):
            anchor.minutes = parse_duration_minutes(duration)
        if fixed not in (None, ""):
            anchor.fixed = _true(fixed)
        if days not in (None, ""):
            try:
                found = parse_days(days, self.gen)
            except ValueError as e:
                raise ToolError(str(e))
            anchor.days = [] if len(found) == 7 else found
        book.save_anchor(anchor)
        out = (f"{anchor.title} goes on the plan by itself at {_clock_of(anchor.at)} ({format_duration(anchor.minutes)}"
               f"{', fixed' if anchor.fixed else ''}), {describe_days(anchor.days) if anchor.days else 'every day'}.")
        plan = self.pt.day_plan
        if (self.pt.is_current(plan) and anchor.on(plan.target_date) and self.set_up_state(plan.target_date)
                and not any(s.title.strip().lower() == anchor.key for s in plan.spans)):
            state = {"placed": [], "unplaced": [], "on_today": []}  # the day is already set up: add it today too
            self._place_anchors(plan, self.pt._now(), state, [anchor])
            if state["placed"]:
                out += f" It's on today's plan at {state['placed'][0].split(' at ')[-1]}."
        return out

    def set_week(self, days: Any = "", work_start: Any = None, work_end: Any = None, no_work: Any = None,
                 bedtime: Any = None, wake: Any = None, **_: Any) -> str:
        from thirties_core.assistant_tools import ToolError
        from thirties_core.plan_tools import ENVELOPE_WORDS_RE
        from thirties_core.week import describe_week, thirty_index
        if self.db is None or self.pt.scheduler is None:
            raise ToolError("The week can't be saved here.")
        said = self.pt.user_message
        if said is not None and not ENVELOPE_WORDS_RE.search(said):
            raise ToolError("Work and sleep hours only change when the user talks about them.")
        gen = self.gen
        try:
            chosen = parse_days(days, gen)
        except ValueError as e:
            raise ToolError(str(e))
        try:
            start = thirty_index(parse_clock(str(work_start))) if work_start not in (None, "") else None
            end = (thirty_index(parse_clock(str(work_end)), round_up=True) - 1) % 48 if work_end not in (None, "") else None
            bed = thirty_index(parse_clock(str(bedtime))) if bedtime not in (None, "") else None
            up = thirty_index(parse_clock(str(wake)), round_up=True) if wake not in (None, "") else None
        except ClockError as e:
            raise ToolError(str(e))
        if not (start is not None or end is not None or _true(no_work) or bed is not None or up is not None):
            raise ToolError("Say what's usual on those days: work_start and work_end, no_work, bedtime or wake.")
        from thirties_core.settings import Settings
        settings = Settings(self.db)
        week = {k: dict(v) for k, v in (getattr(gen, "week", None) or {}).items()}
        workdays = [d for d in DAYS if d in work_days(gen)]
        if _true(no_work):
            workdays = [d for d in workdays if d not in chosen]
        elif start is not None or end is not None:
            workdays = [d for d in DAYS if d in workdays or d in chosen]
        changes: Dict[str, Any] = {}
        if workdays != [d for d in DAYS if d in work_days(gen)]:
            changes["general.work_days"] = workdays
        for name, value in (("work_start_thirty", start), ("work_end_thirty", end), ("sleep_start_thirty", bed),
                            ("sleep_end_thirty", up)):
            if value is None:
                continue
            group = workdays if name.startswith("work") else list(DAYS)
            if set(group) and set(group) <= set(chosen):
                # the usual hours themselves (what Preferences shows): those days' differences go
                changes[f"general.{name}"] = value
                for d in chosen:
                    week.get(d, {}).pop(name, None)
            else:
                for d in chosen:
                    week.setdefault(d, {})[name] = value
        week = {d: v for d, v in week.items() if v}
        if week != (getattr(gen, "week", None) or {}):
            changes["general.week"] = week
        for key, value in changes.items():
            settings.set(key, value)
            setattr(gen, key.split(".", 1)[1], value)  # live, for everything holding this config
        refreshed = self._refresh_envelopes()
        return ("Noted the usual week:\n" + describe_week(gen)
                + (f"\nWork and Sleep on the plan follow it from today ({refreshed} updated)." if refreshed else ""))

    def _refresh_envelopes(self, days: int = 14) -> int:
        """Default Work and Sleep spans nobody changed by hand follow the new pattern, from today on. One the user
        moved (or removed) stays as it is."""
        from thirties_core.models import EventSpan
        sched, db = self.pt.scheduler, self.db
        first = self.a._today()
        changed = 0
        for k in range(days):
            d = first + timedelta(days=k)
            if not db.get_day_meta(d).routines_applied:
                continue  # not built yet: it's made from the new pattern when it is
            fresh = {s.id: s for s in sched.routine_spans(d)}
            for kind in ("work", "sleep"):
                sid = f"routine-{d.isoformat()}-{kind}"
                with db._get_connection() as conn:
                    row = conn.execute("SELECT data, deleted FROM spans WHERE id = ?", (sid,)).fetchone()
                if row is None:
                    if sid in fresh:  # a new work day
                        db.upsert_span(fresh[sid])
                        changed += 1
                    continue
                stored = EventSpan.from_dict(json.loads(row["data"]))
                if row["deleted"] or stored.source != "routine" or stored.status != SpanStatus.PLANNED:
                    continue  # changed or removed by hand
                if sid not in fresh:
                    db.delete_span(sid)
                    changed += 1
                elif (fresh[sid].start_dt, fresh[sid].end_dt) != (stored.start_dt, stored.end_dt):
                    db.upsert_span(fresh[sid])
                    changed += 1
        if changed:
            self._reload_plans()
        return changed

    def _reload_plans(self) -> None:
        """The plans held in memory see the refreshed Work and Sleep."""
        pt = self.pt
        plan = pt.day_plan
        stored = {s.id: s for s in pt.scheduler.state_db.get_spans_between(plan.day_start, plan.day_end)
                  if s.id.startswith("routine-")}
        plan.spans = [s for s in plan.spans if not s.id.startswith("routine-")] + list(stored.values())
        plan.spans.sort(key=lambda s: s.start_dt)
        pt._other_days.clear()

    def handlers(self) -> Dict[str, Any]:
        return {"set_routine": self.set_routine, "skip_routine": self.skip_routine, "set_anchor": self.set_anchor,
                "set_week": self.set_week}


__all__ = ["RoutineBook", "RoutineTools", "Routine", "Anchor", "ROUTINE_TOOL_SPECS", "DEFAULT_ANCHORS",
           "parse_repeat", "describe_repeat", "is_due", "next_due"]
