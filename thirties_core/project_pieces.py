"""Planning a project's pieces: real small pieces, kept together, and time past a piece's end counted as progress.

A project list (a note whose role is "project": a one-off effort with an end) holds items; an item's indented
lines are its pieces. Four rules, all Python, none of them judgement:

1. Time past a piece's end is progress. When the user reports a piece ending after its planned end (a finish time
   past it), its real times are recorded, and the minutes past its end count for the piece and for its
   project. A small record keeps the piece, the minutes and what it displaced.
2. Displaced isn't missed. A planned piece whose time the overrun used (by any reported overrun, of any project or
   none) didn't happen because the user chose to keep going, not because they avoided it. It's never marked
   skipped or missed and never counts for the back-off (backoff.py). It goes later today if there's room, else it
   waits for the next day's plan (first among what's meant for that day).
3. A project's pieces go back to back. When a day has several pieces of one project, they're placed in one
   stretch, with anything unrelated before or after it. Fixed items and routines keep their times, and a meal
   may sit inside the stretch.
4. Sub-items first. An item with open indented lines is offered as its first open one, in the note's order,
   titled "Item: piece". When its last open piece is ticked, the item itself stays open: the user ticks it.

5. Steady pieces (11-14). Every active project gets one small piece offered each day, whether or not
   anything in it is pressing: any work on the project is useful, so it never waits for a due date. The piece is
   its momentum part's next piece if it has one, else the first open item's first piece in the note's order. A
   slot in the day's options is kept for it, so pressing items elsewhere don't crowd it out.
6. Momentum across days. When the user ran past a piece's end on the same part of a project (an item and its
   pieces) on MOMENTUM_DAYS of the last MOMENTUM_WINDOW days, that part leads its project's next stretch and gets
   more time (its usual length plus what the overruns averaged, to whole thirties, at most MOMENTUM_MAX_EXTRA
   more). A meal inside the stretch doesn't break it (rule 3).

What's most startable, how to split an item with no pieces, and whether momentum is real are judgement: not here.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

OVERRUNS_KEY = "overruns"   # [{"day", "piece", "task_id", "project", "minutes", "displaced": [{"id", "title"}]}]
KEEP_DAYS = 60
MOMENTUM_DAYS = 2           # overrun days on one part, out of ...
MOMENTUM_WINDOW = 3         # ... the last this many days, before the day planned (the proposal's two of three)
MOMENTUM_MAX_EXTRA = 90     # minutes momentum may add to a piece
STEADY_SLOTS = 2            # the day's options keep at most this many places for steady project pieces


# --- what's a project, and which project a piece is part of ---

def project_notes(db, with_backlogs: bool = False) -> Dict[str, str]:
    """{note id: its title} for every list whose role is project (and, with_backlogs, every project backlog: time
    on those counts for the project too, but their tickets are never offered one by one)."""
    if db is None:
        return {}
    from thirties_core.lists import ListCatalog
    roles = ("project", "backlog") if with_backlogs else ("project",)
    return {nid: r.get("title") or "" for nid, r in ListCatalog(db).roles().items() if r.get("role") in roles}


def part_of(assistant, name: str, notes: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The project list (note id) a name belongs to, when the name is a part of the project being worked on rather
    than one of the list's own lines: a part the user named (project_facts), a heading in the list, or time on the
    list by its title ("Time on Lighthouse Radio")."""
    from thirties_core.project_facts import ProjectFacts, norm
    db = assistant._db if assistant is not None else None
    notes = project_notes(db, with_backlogs=True) if notes is None else notes
    words = norm(name)
    if not words or not notes:
        return None
    if db is not None:
        for key, rec in ProjectFacts(db).all("part").items():
            if rec.get("note_id") in notes and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", words):
                return rec["note_id"]
    for t in assistant.s.tasks:  # a heading in a project's list
        if t.parent_note_id in notes and t.section and norm(t.section) == words:
            return t.parent_note_id
    for nid, title in notes.items():
        t = norm(title)
        bare = re.sub(r"\s+(dev|development)$", "", t)
        if t and (words in (t, f"time on {t}", f"work on {t}", f"working on {t}")
                  or (bare and words in (bare, f"time on {bare}", f"{bare} dev"))):
            return nid
    return None


def task_of(assistant, span) -> Any:
    """The list item a planned span stands for (its home item, for a line on the Today note)."""
    if assistant is None:
        return None
    tasks = {t.id: t for t in assistant.s.tasks}
    task = tasks.get(getattr(span, "task_id", None) or "")
    if task is None or task.list_role == "today":
        try:
            from thirties_core.today import TodayLinks
            home = TodayLinks(assistant._db).home_of(span.title) if assistant._db is not None else None
        except Exception:
            home = None
        if home and home.get("home_id") in tasks:
            task = tasks[home["home_id"]]
    return task


def project_of(assistant, span, notes: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The project list (its note id) a span is a piece of, or None: by the item it stands for, else by a part
    of the project it names (part_of)."""
    notes = project_notes(assistant._db, with_backlogs=True) if notes is None else notes
    task = task_of(assistant, span)
    note = getattr(task, "parent_note_id", None) if task is not None else None
    if note in notes:
        return note
    if task is not None and getattr(task, "parent_note_id", None):
        return None  # an item on another list
    try:
        return part_of(assistant, getattr(span, "title", ""), notes)
    except Exception as e:
        logger.debug("Couldn't look up the project of %s: %s", getattr(span, "title", ""), e)
        return None


# --- 4. sub-items first ---

def piece_title(tasks: Iterable[Any], task) -> str:
    """A piece by its item and its own words ("Paint the shed: Primer coat"); an item with no parent, or under a
    line that only says what to do with the lines under it ("Choose one of these:"), as itself."""
    if task is None:
        return ""
    parent = next((t for t in tasks if task.parent_task_id and t.id == task.parent_task_id), None)
    from thirties_core.item_path import is_instruction
    from thirties_core.list_reading import name_of
    if parent is None:  # a parent that's no item (ticked): the line's own path still names it
        path = getattr(task, "path", None) or []
        up = str(path[-1]).rstrip(":").strip() if path else ""
    else:
        up = name_of(parent).rstrip(":").strip()  # a label line's colon isn't doubled
    return f"{up}: {task.title}" if up and not is_instruction(up) else task.title


def open_children(tasks: List[Any], task, done: Iterable[str] = ()) -> List[Any]:
    """The open lines indented under ``task`` that are still to do (list_reading.live: not dropped, on hold, an
    option...), in the note's order."""
    from thirties_core.list_reading import live
    done = set(done)
    return [t for t in tasks if t.parent_task_id == task.id and t.id not in done and live(t)]


def first_piece(tasks: List[Any], task, done: Iterable[str] = (), taken: Iterable[str] = ()) -> Optional[Any]:
    """The first open piece of an item, going down through pieces of pieces; the item itself when it has none.
    None when every open piece is already taken (on the plan)."""
    done, taken = set(done), set(taken)
    kids = open_children(tasks, task, done)
    if not kids:
        return None if task.id in taken else task
    for kid in kids:
        found = first_piece(tasks, kid, done, taken)
        if found is not None:
            return found
    return None


def first_pieces(assistant, cands: List[Dict[str, Any]], day: date, on_plan: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The day's candidates with each item that has open pieces offered as its first open one."""
    from thirties_core.backlog import DONE_HIDES_DAYS
    from thirties_core.day_proposal import usual_minutes
    tasks = list(assistant.s.tasks)
    db = assistant._db
    done = db.completed_since(day - timedelta(days=DONE_HIDES_DAYS)) if db is not None else set()
    out, seen = [], set()
    for c in cands:
        t = c.get("task")
        if t is None:
            out.append(c)
            continue
        piece = first_piece(tasks, t, done, on_plan)
        if piece is None or piece.id in seen:
            continue  # every open piece is on the plan already, or offered by another candidate
        seen.add(piece.id)
        if piece is t and not t.parent_task_id:
            out.append(c)
            continue
        out.append({**c, "task": piece, "title": piece_title(tasks, piece),
                    "minutes": c["minutes"] if piece is t else usual_minutes(assistant, piece, piece.title)})
    return out


def last_piece_note(assistant, task) -> str:
    """The last open piece of an item was ticked: the item itself stays open (a parent is done only when the user
    ticks it or says so; its list of pieces may not be complete). The model is told, so it can ask."""
    if not task.parent_task_id:
        return ""
    parent = next((t for t in assistant.s.tasks if t.id == task.parent_task_id), None)
    if parent is None or getattr(parent, "line_kind", "") not in ("", "entry"):  # a label is never done
        return ""
    db = assistant._db
    done = db.completed_since(assistant.pt.day_plan.target_date - timedelta(days=1)) if db is not None else set()
    if open_children(assistant.s.tasks, parent, done | {task.id}):
        return ""
    from thirties_core.list_reading import name_of
    return (f"That was the last open piece under {name_of(parent)}; {name_of(parent)} itself stays open until the "
            f"user says it's done (ask whether it's finished)")


# --- 5 and 6. a steady piece of each project, and momentum across days ---

def top_item(tasks: Iterable[Any], task) -> Any:
    """The item a piece is part of, at the top of its note (the item itself when it has no parent)."""
    by_id = {t.id: t for t in tasks}
    seen = set()
    while task is not None and task.parent_task_id and task.parent_task_id in by_id and task.id not in seen:
        seen.add(task.id)
        task = by_id[task.parent_task_id]
    return task


def _task_for_record(assistant, rec: Dict[str, Any]) -> Any:
    """The list item an overrun record was for: its task id, else its words (a line on the Today note)."""
    tasks = list(assistant.s.tasks)
    by_id = {t.id: t for t in tasks}
    task = by_id.get(rec.get("task_id") or "")
    if task is not None and task.list_role != "today":
        return task
    words = (rec.get("piece") or "").strip().lower()
    for t in tasks:
        if t.list_role != "today" and (t.title.strip().lower() == words or piece_title(tasks, t).lower() == words):
            return t
    try:
        from thirties_core.today import TodayLinks
        home = TodayLinks(assistant._db).home_of(rec.get("piece") or "") if assistant._db is not None else None
    except Exception:
        home = None
    return by_id.get(home.get("home_id")) if home else None


def momentum(assistant, day: date) -> Dict[str, Dict[str, Any]]:
    """{top item id: {"days", "minutes" (the overruns' average), "project"}} for each part of a project the user
    ran past its end on MOMENTUM_DAYS or more of the MOMENTUM_WINDOW days before ``day``."""
    db = assistant._db
    if db is None:
        return {}
    since = day - timedelta(days=MOMENTUM_WINDOW)
    notes = project_notes(db)
    tasks = list(assistant.s.tasks)
    seen: Dict[str, Dict[str, Any]] = {}
    for rec in _records(db):
        try:
            d = date.fromisoformat(rec.get("day", ""))
        except ValueError:
            continue
        if not since <= d < day:
            continue
        task = _task_for_record(assistant, rec)
        if task is None or task.parent_note_id not in notes:
            continue
        top = top_item(tasks, task)
        info = seen.setdefault(top.id, {"days": set(), "total": 0, "count": 0, "project": task.parent_note_id})
        info["days"].add(d)
        info["total"] += int(rec.get("minutes") or 0)
        info["count"] += 1
    return {tid: {"days": len(i["days"]), "minutes": i["total"] // max(1, i["count"]), "project": i["project"]}
            for tid, i in seen.items() if len(i["days"]) >= MOMENTUM_DAYS}


def momentum_minutes(usual: int, extra: int) -> int:
    """A piece with momentum: its usual length plus the overruns' average, in whole thirties."""
    extra = min(MOMENTUM_MAX_EXTRA, max(30, -(-extra // 30) * 30))
    return usual + extra


def steady_pieces(assistant, plan, have: Iterable[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """One piece of each active project list for the day (rule 5), as day candidates, momentum first (rule 6).
    A project with a piece among ``have`` already (something pressing in it), or on the day's plan, gets none.
    Never: someday or done items, items out of season, what rests after missed days, or what's on the plan."""
    from thirties_core.backlog import DONE_HIDES_DAYS
    from thirties_core.backoff import MissStreaks, signature, stated_cant_wait
    from thirties_core.day_proposal import usual_minutes
    from thirties_core.models import SpanStatus
    db = assistant._db
    notes = project_notes(db)
    if not notes:
        return []
    day = plan.target_date
    tasks = [t for t in assistant.s.tasks if t.parent_note_id in notes and not t.id.startswith("media:")]
    if not tasks:
        return []
    done = db.completed_since(day - timedelta(days=DONE_HIDES_DAYS)) if db is not None else set()
    on_plan = {sp.task_id for sp in plan.spans if sp.task_id and sp.status != SpanStatus.SKIPPED}
    on_plan_titles = {sp.title.strip().lower() for sp in plan.spans if sp.status != SpanStatus.SKIPPED}
    covered = set()
    for c in have:
        t = c.get("task")
        if t is not None and t.parent_note_id in notes:
            covered.add(t.parent_note_id)
    for sp in plan.spans:
        if sp.status != SpanStatus.SKIPPED:
            p = project_of(assistant, sp, notes)
            if p:
                covered.add(p)
    try:
        from thirties_core.horizons import HorizonPlanner
        from thirties_core.seasons import in_season
        planner = HorizonPlanner(assistant.pt)
        seasonal = lambda t: in_season(planner.months_of(t), day)  # noqa: E731
    except Exception:
        seasonal = lambda t: True  # noqa: E731
    streaks = MissStreaks(db) if db is not None else None
    hot = momentum(assistant, day)
    from thirties_core.list_reading import live
    out = []
    for note_id in notes:
        if note_id in covered:
            continue
        items = [t for t in tasks if t.parent_note_id == note_id and not t.parent_task_id and not t.someday
                 and t.id not in done and seasonal(t) and live(t)]
        items.sort(key=lambda t: 0 if t.id in hot else 1)  # momentum's part first, then the note's order
        for t in items:
            piece = first_piece(tasks, t, done, on_plan)
            if piece is None:
                continue
            title = piece_title(tasks, piece)
            if title.strip().lower() in on_plan_titles or piece.title.strip().lower() in on_plan_titles:
                continue
            if streaks is not None and streaks.held(day, title, piece.id, signature(piece), piece.due or "",
                                                     urgent=stated_cant_wait(db, piece, title)):
                continue
            minutes = usual_minutes(assistant, piece, piece.title)
            if t.id in hot:
                minutes = momentum_minutes(minutes, hot[t.id]["minutes"])
                why = (f"on a roll: ran past its end on {hot[t.id]['days']} of the last {MOMENTUM_WINDOW} days, "
                       f"so it leads {notes[note_id]} today")
            else:
                why = f"keeps {notes[note_id]} moving"
            out.append({"rank": (4, 0), "task": piece, "title": title, "why": why, "where": notes[note_id],
                        "minutes": minutes, "project": note_id, "momentum": t.id in hot})
            break
    out.sort(key=lambda c: 0 if c["momentum"] else 1)
    return out


def with_steady_pieces(assistant, plan, cands: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The day's candidates with a place kept for each project's steady piece (rule 5): pressing items fill
    the rest, so a project never waits for something in it to be due. At most STEADY_SLOTS places, momentum first.
    A single ticket from a project's backlog is never among them (backlog_tickets.py)."""
    from thirties_core.break_down import held as waits_to_break
    from thirties_core.efforts import apply_to_candidates
    from thirties_core.backlog_tickets import drop_tickets
    cands = [c for c in drop_tickets(cands)  # an item waiting to be broken down: its pieces come once it has them
             if c.get("task") is None or not waits_to_break(assistant._db, c["task"].id)]
    try:
        cands = apply_to_candidates(assistant, cands)  # the user's own estimates (efforts.py)
    except Exception as e:
        logger.warning("Couldn't apply the estimates: %s", e)
    try:
        pieces = steady_pieces(assistant, plan, cands[:limit])
    except Exception as e:  # the morning never fails for want of a project piece
        logger.warning("Couldn't find the projects' steady pieces: %s", e)
        return cands[:limit]
    ids = {c["task"].id for c in cands[:limit] if c.get("task") is not None}
    pieces = [c for c in pieces if c["task"].id not in ids][:STEADY_SLOTS]
    if not pieces:
        return cands[:limit]
    return cands[:max(0, limit - len(pieces))] + pieces


def momentum_first(assistant, day: date) -> Callable[[Any], int]:
    """For ordering a project's stretch: 0 for a piece of a part with momentum (it leads), else 1."""
    hot = momentum(assistant, day) if assistant is not None else {}
    if not hot:
        return lambda s: 1
    tasks = list(assistant.s.tasks)

    def rank(span) -> int:
        task = task_of(assistant, span)
        top = top_item(tasks, task) if task is not None else None
        return 0 if top is not None and top.id in hot else 1
    return rank


# --- 1 and 2. time past a piece's end, and what it displaced ---

def _records(db) -> List[Dict[str, Any]]:
    raw = db.get_sync_meta(OVERRUNS_KEY) if db is not None else None
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        data = []
    return data if isinstance(data, list) else []


def overruns(db, day: Optional[date] = None) -> List[Dict[str, Any]]:
    data = _records(db)
    return [r for r in data if day is None or r.get("day") == day.isoformat()]


def displaced_ids(db, day: date) -> set:
    """The items an overrun displaced on ``day``: never a miss (backoff.py, carry.py)."""
    return {d["id"] for r in overruns(db, day) for d in r.get("displaced", [])}


def displaced_titles(db, day: date) -> set:
    return {d["title"].strip().lower() for r in overruns(db, day) for d in r.get("displaced", [])}


def credit_overrun(assistant, span, planned_end: datetime) -> str:
    """A piece reported past its planned end: record it, and move what its time displaced (rules 1 and 2).
    Returns a line for the reply."""
    from thirties_core.clock import format_clock, format_duration
    from thirties_core.models import SpanCategory, SpanStatus
    pt, db = assistant.pt, assistant._db
    minutes = int((span.end_dt - planned_end).total_seconds() // 60)
    if minutes <= 0:
        return ""
    notes = project_notes(db, with_backlogs=True)
    project = project_of(assistant, span, notes)
    used_from = max(planned_end, span.start_dt)
    displaced = [s for s in pt.day_plan.active_spans()
                 if s.id != span.id and s.status == SpanStatus.PLANNED and not s.is_envelope and not s.tentative
                 and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and s.source != "calendar"
                 and not pt._is_fixed(s) and not pt._is_routine(s)
                 and s.start_dt < span.end_dt and s.end_dt > used_from]
    moved = _replace_displaced(assistant, displaced, span.end_dt) if displaced else []
    if db is not None:
        data = [r for r in _records(db)
                if r.get("day", "") >= (pt.day_plan.target_date - timedelta(days=KEEP_DAYS)).isoformat()]
        data.append({"day": pt.day_plan.target_date.isoformat(), "piece": span.title, "task_id": span.task_id or "",
                     "project": notes.get(project or "", ""), "minutes": minutes,
                     "displaced": [{"id": s.id, "title": s.title} for s in displaced]})
        db.set_sync_meta(OVERRUNS_KEY, json.dumps(data))
    line = f"{format_duration(minutes)} past its planned end counts as time on {span.title}"
    line += f" and on {notes[project]}." if project else "."
    if moved:
        line += " Not missed, just moved: " + "; ".join(moved) + "."
    return line


def _replace_displaced(assistant, spans: List[Any], after: datetime) -> List[str]:
    """Each displaced item: later today where it fits (from the end of the overrun), else off today and first
    among what's meant for a later day (the open pool; never tomorrow by itself, not_today.py). Returns how each
    one went."""
    from thirties_core.carry import TODAY, OpenPool
    from thirties_core.clock import first_fitting_thirty, format_clock
    pt = assistant.pt
    plan = pt.day_plan
    ids = {s.id for s in spans}
    saved = list(plan.spans)
    plan.spans = [s for s in plan.spans if s.id not in ids]  # their old times are free now (taken by the overrun)
    out, later = [], []
    try:
        for s in sorted(spans, key=lambda s: s.start_dt):
            length = s.end_dt - s.start_dt
            minutes = int(length.total_seconds() // 60)
            spot = None
            for a, b in pt.open_windows(after=max(after, pt._now()), before=_bedtime(plan)):
                a = max(a, first_fitting_thirty(max(after, pt._now()), minutes))
                if b - a >= length:
                    spot = a
                    break
            if spot is None:
                later.append(s)
                continue
            s.start_dt, s.end_dt = spot, spot + length
            plan.spans.append(s)
            pt._persist(s)
            out.append(f"{s.title}, {format_clock(spot)}")
    except Exception:
        plan.spans = saved  # never lose one: the plan as it was
        raise
    if later:
        from thirties_core.not_today import day_words, where_to
        for s in later:
            if pt._state_db is not None:
                pt._state_db.delete_span(s.id)
        # a later day this week by fit, or the day the user named; never tomorrow by itself (not_today.py)
        days = {s.id: where_to(pt, s.title, int((s.end_dt - s.start_dt).total_seconds() // 60),
                               plan.target_date)[0] for s in later}
        if pt._state_db is not None:
            OpenPool(pt._state_db).add([{"title": s.title, "task_id": s.task_id or "", "horizon": TODAY,
                                         "when": days[s.id].strftime("%A"),
                                         "start": days[s.id].isoformat(), "end": days[s.id].isoformat(),
                                         "why": "displaced when an earlier piece ran long"} for s in later],
                                       plan.target_date)
        out += [f"{s.title}, first thing in {day_words(days[s.id], plan.target_date)}'s plan" for s in later]
    plan.spans.sort(key=lambda s: s.start_dt)
    keep_together(pt, plan)
    return out


def _bedtime(plan) -> datetime:
    from thirties_core.models import SpanCategory, SpanStatus
    return min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start
                and s.status != SpanStatus.SKIPPED), default=plan.day_end)


# --- 3. a project's pieces back to back ---

def group_in_order(spans: List[Any], project: Callable[[Any], Optional[str]],
                   lead: Optional[Callable[[Any], int]] = None) -> List[Any]:
    """The same items, each project's pieces moved up to follow its first one: unrelated items keep their order,
    before or after the stretch. With ``lead``, a stretch starts with the pieces it ranks first (momentum)."""
    out, emitted = [], set()
    for s in spans:
        p = project(s)
        if p is None:
            out.append(s)
        elif p not in emitted:
            emitted.add(p)
            group = [x for x in spans if project(x) == p]
            out += sorted(group, key=lead) if lead is not None else group  # stable: else the order they were in
    return out


def keep_together(pt, plan=None) -> List[str]:
    """Lay the day's flexible items out again so each project's pieces are back to back (rule 3): from the first
    one's time, around everything that keeps its time (fixed items, routines, meals, calendar events), with
    unrelated items never earlier than they were. Changes nothing unless every item still fits before bed.
    Returns the titles that moved."""
    from thirties_core.clock import thirty_ceil
    from thirties_core.meals import window as meal_window
    from thirties_core.models import SpanCategory, SpanStatus
    assistant = pt.assistant
    plan = plan or pt.day_plan
    if assistant is None or assistant._db is None:
        return []
    notes = project_notes(assistant._db, with_backlogs=True)
    if not notes:
        return []
    now = pt._now()
    from_t = thirty_ceil(now) if plan.day_start <= now < plan.day_end else plan.day_start
    flexible = sorted((s for s in plan.active_spans()
                       if s.status == SpanStatus.PLANNED and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)
                       and pt._movable(s) and s.start_dt >= from_t and meal_window(s.title, plan.day_start) is None
                       and not pt._is_fixed(s) and not pt._is_routine(s)), key=lambda s: s.start_dt)
    proj = {s.id: project_of(assistant, s, notes) for s in flexible}
    counts: Dict[str, int] = {}
    for p in proj.values():
        if p:
            counts[p] = counts.get(p, 0) + 1
    together = {p for p, n in counts.items() if n >= 2}
    if not together:
        return []
    key = lambda s: proj[s.id] if proj[s.id] in together else None  # noqa: E731
    order = group_in_order(flexible, key, momentum_first(assistant, plan.target_date))
    ids = {s.id for s in flexible}
    # what keeps its time: everything else on the day that takes time (Work and other envelopes don't: things are
    # placed on them), and bedtime
    taken: List[Tuple[datetime, datetime]] = [
        (s.start_dt, s.end_dt) for s in plan.active_spans()
        if s.id not in ids and not s.is_envelope and not s.tentative and not s.shown_only
        and s.status != SpanStatus.SKIPPED and s.end_dt > from_t]
    bed = _bedtime(plan)
    placed: Dict[str, Tuple[datetime, datetime]] = {}
    cursor = flexible[0].start_dt
    prev_project = None
    stretch_from: Dict[str, datetime] = {}  # a stretch starts where its first piece was, whichever piece leads it
    for s in flexible:
        if key(s) is not None:
            stretch_from.setdefault(key(s), s.start_dt)
    for s in order:
        length = s.end_dt - s.start_dt
        p = key(s)
        earliest = cursor if (p is not None and p == prev_project) else \
            max(cursor, stretch_from.get(p, s.start_dt) if p is not None else s.start_dt)
        spot = _fit(taken, earliest, length, bed)
        if spot is None:
            return []  # it wouldn't all fit before bed: leave the day as it was
        placed[s.id] = (spot, spot + length)
        taken.append(placed[s.id])
        cursor, prev_project = spot + length, p
    moved = []
    for s in flexible:
        if placed[s.id] != (s.start_dt, s.end_dt):
            s.start_dt, s.end_dt = placed[s.id]
            pt._persist(s)
            moved.append(s.title)
    plan.spans.sort(key=lambda s: s.start_dt)
    return moved


def _fit(taken, earliest: datetime, length: timedelta, bed: datetime) -> Optional[datetime]:
    """The first time from ``earliest`` that ``length`` fits without touching anything taken, before bed."""
    t = earliest
    while t + length <= bed:
        clash = max((e for s, e in taken if s < t + length and t < e), default=None)
        if clash is None:
            return t
        t = clash
    return None


__all__ = ["credit_overrun", "displaced_ids", "displaced_titles", "first_piece", "first_pieces", "group_in_order",
           "keep_together", "momentum", "momentum_first", "overruns", "part_of", "piece_title", "project_notes",
           "project_of",
           "steady_pieces", "last_piece_note", "top_item", "with_steady_pieces"]
