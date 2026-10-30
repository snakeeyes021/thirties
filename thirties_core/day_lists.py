"""The day's list beyond the plan: things pencilled in, days ahead, and pings.

- **Pencilled in** (O3, 10-29: "pencil them out (not pen)… items that are on today's
  to-do list but are not scheduled?"): on today's list, with no time. Python works
  out which Today items have no time on the plan; replies say "on today's list, no
  time yet", the Day page lists them, and they can be given a time later (scheduling
  one finds it already on the list).
- **Days ahead** (O2, 10-24: "should I see it in the day plan for tomorrow?"): what's
  planned for another day is on that day's plan, and reaches the Today note on that
  day's morning (the morning reset), never today's. Things meant for a later day
  with no time wait in Thirties' own list for that day (the Today note only ever
  holds today).
- **Pings** (B5, C4): "remind me" is a thing to do; a bare notification only when
  the user asks for one in those words (10-27: "I'm always censoring my wording").
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

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
    spans = [s for s in plan.spans if _plannable(s) and s.status != SpanStatus.SKIPPED]
    titles = {s.title.strip().lower() for s in spans}
    task_ids = {s.task_id for s in spans if s.task_id}
    links = TodayLinks(db)
    out = []
    for line in (note.get("body") or "").splitlines():
        m = CHECKBOX_REGEX.match(line)
        if not m or m.group(1):
            continue
        text = m.group(2).strip()
        if text.lower() in routines or text.lower() in titles:
            continue
        link = links.home_of(text)
        if link and (link["home_id"] in task_ids or link["home_title"].strip().lower() in titles):
            continue
        out.append(text)
    return out


def pencilled_text(assistant, plan=None) -> str:
    """A line for the schedule text, so replies can say what's on the list with no time yet."""
    try:
        items = pencilled_in(assistant, plan)
    except Exception:
        return ""
    if not items:
        return ""
    plan = plan or assistant.pt.day_plan
    whose = "today's list" if assistant.pt.is_current(plan) else f"{plan.target_date.strftime('%A')}'s list"
    return f"\nOn {whose}, no time yet (pencilled in): " + "; ".join(items)


def ahead_note(assistant, plan) -> str:
    """Said when something goes on a later day's plan: where the user will see it."""
    if plan.target_date <= assistant.pt.today() or assistant.pt.is_current(plan):
        return ""
    return (f"(It's on {plan.target_date.strftime('%A')}'s plan, and goes on the Today list that morning, not on "
            "today's.)")


def add_ahead(assistant, items: List[str]) -> str:
    """Things for a later day's list, with no time: kept until that morning."""
    plan = assistant.pt.day_plan
    store = AheadLists(assistant._db)
    added = []
    for name in items:
        task = assistant._exact_task(str(name))
        title = task.title if task is not None else str(name).strip()
        if title:
            store.add(plan.target_date, title, task)
            added.append(title)
    if not added:
        return "Nothing to add."
    return (f"On {plan.target_date.strftime('%A')}'s list, no time yet: {', '.join(added)}. It goes on the Today "
            "list that morning.")


def onto_today(assistant, day: date) -> List[str]:
    """The morning reset, for the day starting: what was planned ahead for it goes on the Today note (a list item as
    its linked copy, a routine as its group), and so does what was waiting for that day's list. Returns what was
    done, for the log."""
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
        known = {t.id for t in assistant.s.tasks}  # linked by its id while its list item is still there
        names = singles + [r["home_id"] if r["home_id"] in known else r["title"] for r in waiting]
        if names:
            out = assistant.add_to_today(names)
            if out.startswith("Added"):
                done.append(out.rstrip("."))
        for r in waiting:
            AheadLists(assistant._db).set_state(day, r["title"], "listed")
    except ToolError as e:
        done.append(f"couldn't fill Today: {e}")
    return done


def planned_ahead_text(assistant) -> str:
    """For the morning message: what's already planned for today, by block, and what's on the list with no time."""
    pt = assistant.pt
    plan = pt.day_plan
    items = sorted((s for s in plan.spans if _plannable(s) and s.status == SpanStatus.PLANNED
                    and s.end_dt > pt._now()), key=lambda s: s.start_dt)
    lines = []
    for s in items:
        when = format_clock(s.start_dt) if pt._is_fixed(s) else format_clock(thirty_of(s.start_dt))
        lines.append(f"- {when} – {s.title}")
    out = ("Already planned for today:\n" + "\n".join(lines)) if lines else ""
    loose = pencilled_in(assistant, plan)
    if loose:
        out += ("\n" if out else "") + "On today's list, no time yet: " + ", ".join(loose) + "."
    return out


__all__ = ["AheadLists", "explicit_ping", "pencilled_in", "pencilled_text", "ahead_note", "add_ahead", "onto_today",
           "planned_ahead_text"]
