"""The day's list beyond the plan: things pencilled in, days ahead, and pings.

- **Not yet placed** (``pencilled_in``): Today lines no planned item stands for yet, outside the "If there's
  time" section (if_time.py). They are never shown as untimed: the user wants them today, so the next plan build
  or sync gives each a time, or, the day full, moves the lowest item on (full_day.py).
- **Days ahead** (O2, 10-24: where an item for tomorrow shows up): what's
  planned for another day is on that day's plan, and reaches the Today note on that
  day's morning (the morning reset), never today's. Things meant for a later day
  with no time wait in Thirties' own list for that day (the Today note only ever
  holds today).
- **Pings** (B5, C4): "remind me" is a thing to do; a bare notification only when
  the user asks for one in those words (10-27: the user shouldn't have to watch their wording).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from thirties_core.clock import format_clock, thirty_of
from thirties_core.joplin_engine import CHECKBOX_REGEX
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.scheduler import utc_now_iso

PING_WORDS = re.compile(r"\b(ping|alarm|buzz|notif\w*|alert|text me|message me)\b", re.IGNORECASE)


def explicit_ping(message: Optional[str]) -> bool:
    """A bare ping only when the person asked for one in those words ("ping me at 3", "set an alarm"). With nobody
    talking (a script, a test), the call is taken as given."""
    return message is None or bool(PING_WORDS.search(message))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_list_ahead (
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    home_id TEXT NOT NULL DEFAULT '',
    home_note_id TEXT NOT NULL DEFAULT '',
    home_title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'waiting',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, title)
);
"""


class AheadLists:
    """Things meant for a later day's list, with no time: they go on the Today note that morning. Synced."""

    def __init__(self, db) -> None:
        self.db = db
        with db._get_connection() as conn:
            conn.executescript(_SCHEMA)

    def add(self, day: date, title: str, home=None) -> None:
        with self.db._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO day_list_ahead (day, title, home_id, home_note_id, home_title, state, "
                         "updated_at) VALUES (?, ?, ?, ?, ?, 'waiting', ?)",
                         (day.isoformat(), title.strip(), getattr(home, "id", "") or "",
                          (getattr(home, "parent_note_id", "") or getattr(home, "id", "")) if home else "",
                          getattr(home, "title", "") or "", utc_now_iso()))

    def for_day(self, day: date) -> List[Dict[str, str]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM day_list_ahead WHERE day = ? AND state = 'waiting' "
                                                  "ORDER BY updated_at", (day.isoformat(),)).fetchall()]

    def set_state(self, day: date, title: str, state: str) -> None:
        with self.db._get_connection() as conn:
            conn.execute("UPDATE day_list_ahead SET state = ?, updated_at = ? WHERE day = ? AND lower(title) = ?",
                         (state, utc_now_iso(), day.isoformat(), title.strip().lower()))

    def all_records(self) -> List[Dict[str, Any]]:
        with self.db._get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM day_list_ahead").fetchall()]

    def merge_record(self, record: Dict[str, Any]) -> bool:
        cols = ["day", "title", "home_id", "home_note_id", "home_title", "state", "updated_at"]
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT updated_at FROM day_list_ahead WHERE day = ? AND title = ?",
                               (record["day"], record["title"])).fetchone()
            if row and row["updated_at"] >= record["updated_at"]:
                return False
            conn.execute(f"INSERT OR REPLACE INTO day_list_ahead ({', '.join(cols)}) VALUES "
                         f"({', '.join('?' * len(cols))})", [record.get(c, "") for c in cols])
        return True


def _plannable(span: EventSpan) -> bool:
    return (not span.is_envelope and span.source != "calendar"
            and span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK))


def pencilled_in(assistant, plan=None) -> List[str]:
    """What's on the day's list with no time on its plan: for today, open top-level lines of the Today note (not a
    routine's group) that no planned item stands for; for a later day, what's waiting for that day's list."""
    from thirties_core.today import TodayLinks
    pt = assistant.pt
    plan = plan or pt.day_plan
    db = assistant._db
    if db is None:
        return []
    if not pt.is_current(plan):
        return [r["title"] for r in AheadLists(db).for_day(plan.target_date)] if plan.target_date > pt.today() else []
    note = assistant._today_note()
    if note is None:
        return []
    routines = {n["title"].strip().lower() for n in assistant._routines()}
    import json  # yesterday's leftovers aren't today's (carry.py), even before the reset has taken them off
    try:
        left = json.loads(db.get_sync_meta(f"leftovers:{plan.target_date.isoformat()}") or "{}")
    except ValueError:
        left = {}
    routines |= {t.strip().lower() for k in ("ended", "pooled", "off_today") for t in left.get(k, [])}
    spans = [s for s in plan.spans if _plannable(s) and s.status != SpanStatus.SKIPPED]
    titles = {s.title.strip().lower() for s in spans}
    task_ids = {s.task_id for s in spans if s.task_id}
    # skipped today and planned nowhere else today: not happening today, so not "no time yet" either (its line
    # leaves Today with the skip, leave_today.py, due today or not; this covers the time before Joplin has the change)
    skipped = [s for s in plan.spans if _plannable(s) and s.status == SpanStatus.SKIPPED]
    routines |= {s.title.strip().lower() for s in skipped if s.title.strip().lower() not in titles}
    task_ids |= {s.task_id for s in skipped if s.task_id}
    links = TodayLinks(db)
    out = []
    from thirties_core import if_time
    untimed = {t.lower() for t in if_time.items(note)}  # the If there's time section is its own (if_time.py)
    for line in (note.get("body") or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        if not m or m.group(1):
            continue
        text = m.group(2).strip()
        if text.lower() in routines or text.lower() in titles or text.lower() in untimed:
            continue
        link = links.home_of(text)
        if link and (link["home_id"] in task_ids or link["home_title"].strip().lower() in titles):
            continue
        out.append(text)
    return out


def ahead_note(assistant, plan) -> str:
    """Said when something goes on a later day's plan: where the user will see it."""
    if plan.target_date <= assistant.pt.today() or assistant.pt.is_current(plan):
        return ""
    return (f"(It's on {plan.target_date.strftime('%A')}'s plan, and goes on the Today list that morning, not on "
            "today's.)")


def add_ahead(assistant, items: List[str]) -> str:
    """Things for a later day: each waits in the open pool with that day as its window (not_today.set_aside, as a
    day the user named) and gets its time when that day's plan is built (full_day.settle_loose). Never written to
    the Today note without a time, and never shown as a list with no time."""
    from thirties_core.not_today import set_aside
    plan = assistant.pt.day_plan
    added, deadlines = [], []
    from thirties_core.due_today import move_check  # due today: only on a clear word (due_today.py)
    for name in items:
        task = assistant._exact_task(str(name))
        title = task.title if task is not None else str(name).strip()
        if title:
            deadlines.append(move_check(assistant.pt, title, task.id if task is not None else None,
                                        plan.target_date).strip())
            set_aside(assistant, title, task, plan.target_date, True)
            added.append(title)
    if not added:
        return "Nothing to add."
    deadlines = [d for d in deadlines if d]
    from thirties_core.leave_today import after_ahead  # moved off today's list, it leaves it (leave_today.py)
    off = [o for o in (after_ahead(assistant, t, plan.target_date) for t in added) if o]
    return (f"Kept for {plan.target_date.strftime('%A')}: {', '.join(added)}. Each gets its time when that day's "
            "plan is made." + (" " + " ".join(off) if off else "") + (" " + " ".join(deadlines) if deadlines else ""))


def onto_today(assistant, day: date) -> List[str]:
    """The morning reset, for the day starting: what was planned ahead for it goes on the Today note (a list item as
    its linked copy, a routine as its group). What waited for the day with no time is left to the plan build, which
    gives each its time (full_day.settle_loose). Returns what was done, for the log."""
    from thirties_core.assistant_tools import ToolError
    pt = assistant.pt
    if assistant._db is None or assistant._today_note() is None:
        return []
    plan = pt.plan_for(day)
    done: List[str] = []
    groups: Dict[str, Any] = {}
    singles: List[str] = []
    tasks = {t.id: t for t in assistant.s.tasks}
    adding = getattr(assistant, "_adding", set())  # what the call that started the day is putting on Today itself
    for span in sorted(plan.spans, key=lambda s: s.start_dt):
        if not _plannable(span) or span.status != SpanStatus.PLANNED or span.title.strip().lower() in adding:
            continue
        task = tasks.get(span.task_id) if span.task_id else None
        if task is not None and task.list_role == "today":
            continue
        name = task.title if task is not None else span.title
        routine = assistant._routine_of(name)
        if routine is not None:
            groups.setdefault(routine[0]["id"], (routine[0], []))[1].append(routine[1])
        elif name not in singles:
            singles.append(name)
    try:
        for note, steps in groups.values():
            assistant._routine_on_today(note, steps)
            done.append(f"put {note['title']} on Today")
        waiting = AheadLists(assistant._db).for_day(day)
        # --- dropped since it was set aside for this day: it doesn't come back (not_today.py) ---
        from thirties_core.carry import DROPPED, OpenPool
        pool = OpenPool(assistant._db).all()
        gone = [r for r in waiting if (pool.get(r["title"].strip().lower()) or {}).get("horizon") == DROPPED]
        for r in gone:
            AheadLists(assistant._db).set_state(day, r["title"], "dropped")
        waiting = [r for r in waiting if r not in gone]
        # --- end dropped ---
        # what waited for this day isn't written here: the plan build gives each its time, and the plan puts it on
        # Today (full_day.settle_loose); nothing reaches Today without a time
        if singles:
            out = assistant._add_to_today(singles, "none")  # already on the day's plan: their times stand
            if out.startswith("Added"):
                done.append(out.rstrip("."))
    except ToolError as e:
        done.append(f"couldn't fill Today: {e}")
    return done


def planned_ahead_text(assistant, skip: Iterable[str] = ()) -> str:
    """For the morning message: the whole day already planned, one plain line each (no bullets), time first: each
    routine part with its steps, the meals and other anchors, and anything else on. Nothing on today is without a
    time (full_day.py). ``skip``: kept for callers; yesterday's leftovers are offered on their own (10-31)."""
    pt = assistant.pt
    plan = pt.day_plan
    from thirties_core.today import note_items
    from thirties_core.step_times import key as _step_key
    # each routine's steps (as worded, and by name without a leading time), by the routine they belong to
    steps = {k: note["title"].strip() for note in assistant._routines()
             for text, _ in note_items(note) for k in (text.strip().lower(), _step_key(text))}
    items = sorted((s for s in plan.spans if _plannable(s) and s.status == SpanStatus.PLANNED
                    and s.end_dt > pt._now()), key=lambda s: s.start_dt)
    rows: List[Tuple[datetime, str]] = []  # (when, line)
    parts: Dict[Tuple[datetime, str], List[str]] = {}  # the morning's routines are part of the day's plan, by part
    from thirties_core.item_path import span_label  # a nested list item with its parents
    tasks = list(assistant.s.tasks)
    for s in items:
        from thirties_core.step_times import key as step_key  # a step by name, without a leading time
        routine = steps.get(s.title.strip().lower()) or steps.get(step_key(s.title))
        if routine is not None:
            parts.setdefault((thirty_of(s.start_dt), routine), []).append(s.title)
            continue
        when = format_clock(s.start_dt) if pt._is_fixed(s) else format_clock(thirty_of(s.start_dt))
        rows.append((s.start_dt, f"{when} {span_label(s, tasks)}"))
    rows += [(block, f"{format_clock(block)} {', '.join(titles)}") for (block, _), titles in parts.items()]
    lines = [line for _, line in sorted(rows, key=lambda r: r[0])]
    return ("Already planned for today:\n" + "\n".join(lines)) if lines else ""


__all__ = ["AheadLists", "explicit_ping", "pencilled_in", "ahead_note", "add_ahead", "onto_today",
           "planned_ahead_text"]
