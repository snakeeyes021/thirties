"""A one-time thing whose day is over never rolls into tomorrow by itself (carry.py, rule 3).

A leftover is unmarked, so its status is unknown (rule 1). Python gathers what the item carries, and the morning
model weighs them; whether an item has gone moot is never read from its wording by Python.

The facts, for each leftover in the morning proposal (``gather``): whether it was planned at a time on a past day
and whether that time was the user's (a time they stated for it, or one typed at the start of its line), whether it
has a home list or lives only on the Today note, its kind, how many mornings it has already been offered, and the
dates its name gives. They go into the leftover list the morning model reads (``line``), and propose_leftovers
answers with them.

What follows from them:

- A one-off the user put at a stated time on a day that is now over gets no day from Python's first guess: it is
  "not marked yet" (``unknown``), in the morning's one question ("did it happen?"), never "tomorrow" by itself.
- The model may end it (propose_leftovers ``end``): a one-time thing that lived only for its day and time is moot
  unless the user said it moved. Ending means "not carried", never ticked done: its record is archived as ended, a
  Today copy of a listed item comes off Today (its home item is untouched), and a line that lives only on Today is
  filed in the month's Dropped note (``end_now``), so nothing is lost and undrop puts it back.
- An item offered in one morning's proposal is never offered or placed again by itself (``offered_before``).

Invented example: a bike co-op volunteer had "Pick up the donated frames" at 2:40 and "Run the tyre swap stall" at
3:10, times they gave, on yesterday's plan and never marked either. This morning neither is put on tomorrow: both
are asked about once ("not marked yet"), unless the morning model ends them as over.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

OFFERED_KEY = "leftovers_offered"  # {title lower: [days it was in a morning proposal]}
LOOK_BACK_DAYS = 7
UNKNOWN_WHEN = "not marked yet"
ENDED_WHY = "its day is over"
# for the morning model, beside the leftover list (never for the user)
RULE = ("Leftovers whose slot has passed get no new day from you: list them under unknown in propose_leftovers, "
        "where they wait without a day for the one question about whether they happened. Tasks stay open after "
        "their planned slot goes by. "
        "An item offered on an earlier morning is never offered again.")

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RE = re.compile(rf"\b(\d{{4}}-\d{{1,2}}-\d{{1,2}}|{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?|"
                      rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}|\d{{1,2}}/\d{{1,2}})\b", re.IGNORECASE)


# --- offered once, never again ---

def _offers(db) -> Dict[str, List[str]]:
    try:
        data = json.loads(db.get_sync_meta(OFFERED_KEY) or "{}") if db is not None else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def record_offered(db, titles: Iterable[str], day: date) -> None:
    """The mornings each item was in the proposal (kept 60 days)."""
    if db is None:
        return
    cutoff = (day - timedelta(days=60)).isoformat()
    data = {k: [d for d in v if d >= cutoff] for k, v in _offers(db).items() if isinstance(v, list)}
    for t in titles:
        days = data.setdefault(str(t).strip().lower(), [])
        if day.isoformat() not in days:
            days.append(day.isoformat())
    db.set_sync_meta(OFFERED_KEY, json.dumps({k: v for k, v in data.items() if v}, sort_keys=True))


def times_offered(db, title: str, before: date) -> int:
    """How many earlier mornings had this item in their proposal."""
    from thirties_core.carry import ASKED_KEY, _load
    days = {d for d in _offers(db).get(str(title).strip().lower(), []) if d < before.isoformat()}
    asked = _load(db, ASKED_KEY).get(str(title).strip().lower()) if db is not None else None
    if asked and asked < before.isoformat():
        days.add(asked)  # asked before this record was kept
    return len(days)


def offered_before(db, title: str, day: date) -> bool:
    """Already in an earlier morning's proposal: never offered or placed again by itself (carry.py, rule 3)."""
    return times_offered(db, title, day) > 0


# --- the facts ---

def _key(title: str) -> str:
    from thirties_core.step_times import key
    return key(title)


def _last_planned(assistant, title: str, task_id: str, day: date):
    """The latest row for the item on a plan before ``day`` (within a week), with its day: (span, day) or None."""
    keys = {_key(title)}
    for back in range(1, LOOK_BACK_DAYS + 1):
        d = day - timedelta(days=back)
        try:
            plan = assistant._plan_peek(d) if hasattr(assistant, "_plan_peek") else assistant.pt.plan_for(d)
        except Exception:
            continue
        if plan is None or getattr(plan, "target_date", d) != d:
            continue
        rows = [s for s in plan.spans if not s.is_envelope
                and ((task_id and s.task_id == task_id) or _key(s.title) in keys)]
        if rows:
            return max(rows, key=lambda s: s.start_dt), d
    return None


def named_dates(title: str, day: date) -> List[str]:
    """The calendar dates the item's name gives ("Swap meet table, Nov 21"), as ISO days. A bare weekday names no date."""
    from thirties_core.clock import parse_day
    out = []
    for m in _DATE_RE.finditer(str(title or "")):
        try:
            d = parse_day(m.group(1), day)
        except Exception:
            continue
        if d is not None and d.isoformat() not in out:
            out.append(d.isoformat())
    return out


def gather(assistant, title: str, task_id: str, day: date, line: str = "") -> Dict[str, Any]:
    """What a leftover carries, for the morning model: {"planned_day", "planned_at", "time_by_user", "home",
    "kind", "offered", "dates"}; "planned_day" is "" when no earlier plan had it at a time."""
    from thirties_core.clock import format_clock, thirty_of
    from thirties_core.step_times import split_leading_time
    out: Dict[str, Any] = {"planned_day": "", "planned_at": "", "time_by_user": False, "home": "", "kind": "",
                           "offered": 0, "dates": []}
    hit = _last_planned(assistant, title, task_id, day)
    if hit is not None:
        span, d = hit
        given = getattr(span, "given_start", None)
        out["planned_day"] = d.isoformat()
        if given is not None:
            out["planned_at"], out["time_by_user"] = format_clock(given), True
        else:
            out["planned_at"] = format_clock(thirty_of(span.start_dt))
        out["category"] = span.category.value
    for text in (line, title):
        _, said, _ = split_leading_time(text or "")
        if said is not None:  # a time typed at the start of its line: the user's
            out["time_by_user"] = True
            if not out["planned_day"]:
                out["planned_day"] = (day - timedelta(days=1)).isoformat()
            if not out["planned_at"] or hit is None or getattr(hit[0], "given_start", None) is None:
                out["planned_at"] = format_clock(datetime.combine(day, said))
            break
    tasks = {t.id: t for t in getattr(assistant.s, "tasks", [])}
    home = tasks.get(task_id or "")
    if home is not None and home.list_role != "today":
        out["home"] = home.parent_note_title or "a list"
    try:
        out["kind"] = assistant.kind_of(title)
    except Exception:
        out["kind"] = ""
    out["offered"] = times_offered(getattr(assistant, "_db", None), title, day)
    out["dates"] = named_dates(title, day)
    return out


def past_time(facts: Dict[str, Any], day: date) -> bool:
    """A user-stated time on a day that has ended, or a named date already gone: Python picks no day for it."""
    if not facts:
        return False
    if facts.get("time_by_user") and facts.get("planned_day") and facts["planned_day"] < day.isoformat():
        return True
    dates = facts.get("dates") or []
    return bool(dates) and max(dates) < day.isoformat()


def line(facts: Dict[str, Any], day: date) -> List[str]:
    """The facts as the model's leftover list says them, each a short phrase."""
    if not facts:
        return []
    out = []
    if facts.get("planned_day"):
        d = date.fromisoformat(facts["planned_day"])
        when = "yesterday" if (day - d).days == 1 else d.strftime("%A %b %-d")
        at = facts.get("planned_at") or ""
        said = f"on {when}'s plan" if when == "yesterday" else f"on the plan for {when}"
        out.append(said + (f" at {at}" if at else "") + (
            " (the user's own time)" if facts.get("time_by_user") else " (a slot Thirties chose)"))
        if past_time(facts, day):
            out.append("its slot has passed")
    out.append(f"home list {facts['home']}" if facts.get("home") else "lives only on the Today note")
    if facts.get("kind"):
        out.append(f"kind {facts['kind']}")
    n = int(facts.get("offered") or 0)
    out.append("never offered before" if n == 0 else f"offered {n} morning{'s' if n != 1 else ''} before")
    if facts.get("dates"):
        out.append("names " + ", ".join(date.fromisoformat(x).strftime("%a %b %-d") for x in facts["dates"]))
    return out


# --- ended: not carried, never done ---

def end_now(assistant, items: List[Dict[str, Any]], day: date) -> List[str]:
    """End leftovers whose day is over: their pool records are archived as ended (never offered again), a Today
    copy of an item with a home list comes off Today (the home item is untouched), and a line that lives only on
    Today is filed in the month's Dropped note, so undrop puts it back. Nothing is ticked done."""
    from thirties_core.carry import DROPPED, OpenPool
    from thirties_core.done_log import FILED_ROLES, file_dropped_op
    from thirties_core.today import TodayLinks, note_items
    db = getattr(assistant, "_db", None)
    if db is None or not items:
        return []
    pool = OpenPool(db)
    for i in items:
        if pool.set(i["title"], horizon=DROPPED, pending_drop=False, dropped_on=day.isoformat(), dropped_by="app",
                    ended=True, why=i.get("why") or ENDED_WHY) is None:
            pool.add([{**i, "horizon": DROPPED}], day - timedelta(days=1))
            pool.set(i["title"], dropped_on=day.isoformat(), dropped_by="app", ended=True,
                     why=i.get("why") or ENDED_WHY)
    note, writer = assistant._today_note(), assistant._writer()
    if note is None or writer is None or not getattr(writer, "allowed", True):
        return [i["title"] for i in items]
    open_today = {x for x, ticked in note_items(note) if not ticked}
    links = TodayLinks(db)
    until = {"valid_until": assistant.valid_until(day)}  # this morning's sorting, for this day only
    for i in items:
        t = i["title"]
        line_text = next((x for x in open_today if x.strip().lower() == t.strip().lower()), None)
        if line_text is None:
            continue
        has_home = links.home_of(line_text) is not None or any(
            x.title.strip().lower() == t.strip().lower() and x.list_role in FILED_ROLES for x in assistant.s.tasks)
        if has_home:
            writer.submit({**until, "op": "remove", "note_id": note["id"], "title": line_text})  # its Today copy
            links.unlink(line_text)
        else:
            op = file_dropped_op(note["id"], line_text, note.get("title", ""), day, 0, i.get("why") or ENDED_WHY)
            status, _ = writer.submit({**until, **op})
            if status not in ("done", "queued"):
                continue  # not filed: its line stays where it is
            pool.set(t, filed_key=op["key"])
        assistant.s.tasks = [x for x in assistant.s.tasks
                             if not (x.parent_note_id == note["id"] and x.title == line_text)]
    return [i["title"] for i in items]


def question(names: List[str]) -> str:
    """The one question about what isn't marked yet ("" when nothing)."""
    if not names:
        return ""
    if len(names) == 1:
        return f"Not marked yet: {names[0]}. Did it happen?"
    return "Not marked yet: " + ", ".join(names[:-1]) + f" and {names[-1]}. Did they happen?"


__all__ = ["gather", "past_time", "line", "end_now", "question", "record_offered", "times_offered",
           "offered_before", "named_dates", "UNKNOWN_WHEN", "ENDED_WHY", "RULE"]
