"""What happens to yesterday's unmarked items: the rule set that ends the pile-up.

11-04, 11-05: every open line on the Today note came back every morning, a week's worth piled up, and the
same pile sat in "on the list, no time yet" all day. Matt (11-05): a person planning a day tracks whole items
and works out when each can go; they don't throw everything unmarked into the next day. An assistant that
can't see what was done does have to find out, once.

The rules, applied once a day when the day is built (after the user's first reply, wake.py):

1. Unmarked means unknown: not "not done", and not "do it today".
2. Some things never roll. Once their day is over they simply end, unasked: routine steps, meals and daily
   anchors, time on a project, calendar events and anything fixed to a time, anything that has gone moot, and a
   planned item that's on no list (when unsure, it doesn't roll). They leave the Today note.
3. A one-off to-do that's on a list (its home list, or the Today note) and has no outcome is asked about once,
   in one short batched question in the morning message ("Did any of these get done: A, B, C?"). Never per
   item, never before the user is up, never again for the same item: the answer, or no answer, settles it.
4. Not done (or no answer): it goes into the open-items pool, not into today. It stays on its own list in
   Joplin and leaves the Today note (an item with no other list stays there: nothing is ever lost). When the day
   is proposed, a pool item is one of the options ("left open since Tuesday"), after anything due or urgent, and
   it's proposed only if it fits the free time; otherwise it waits. It leaves the pool once it's done, planned,
   or two weeks old (it's still on its list).
5. The Today note holds today's plan only.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

POOL_KEY = "open_pool"          # {title lower: {"title", "task_id", "since"}}
ASKED_KEY = "status_asked"      # {title lower: day asked}: never asked twice
POOL_DAYS = 14
ASKED_DAYS = 60


def _load(db, key: str) -> Dict[str, Any]:
    raw = db.get_sync_meta(key) if db is not None else None
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(db, key: str, data: Dict[str, Any]) -> None:
    if db is not None:
        db.set_sync_meta(key, json.dumps(data, sort_keys=True))


class OpenPool:
    """Items left open on an earlier day, waiting on their lists for a day they fit."""

    def __init__(self, db) -> None:
        self.db = db

    def all(self, today: Optional[date] = None) -> Dict[str, Dict[str, str]]:
        data = _load(self.db, POOL_KEY)
        if today is not None:
            cutoff = (today - timedelta(days=POOL_DAYS)).isoformat()
            data = {k: v for k, v in data.items() if v.get("since", "") >= cutoff}
        return data

    def add(self, items: Iterable[Dict[str, str]], since: date) -> None:
        data = self.all(since)
        for item in items:
            key = item["title"].strip().lower()
            data.setdefault(key, {"title": item["title"].strip(), "task_id": item.get("task_id") or "",
                                  "since": since.isoformat()})
        _save(self.db, POOL_KEY, data)

    def drop(self, titles: Iterable[str]) -> None:
        data = _load(self.db, POOL_KEY)
        for t in titles:
            data.pop(str(t).strip().lower(), None)
        _save(self.db, POOL_KEY, data)

    def since(self, title: str) -> Optional[date]:
        rec = _load(self.db, POOL_KEY).get(title.strip().lower()) or {}
        try:
            return date.fromisoformat(rec["since"]) if rec.get("since") else None
        except ValueError:
            return None


def note_added_today(db, day: date, titles: Iterable[str]) -> None:
    key = f"today_added:{day.isoformat()}"
    have = set(json.loads(db.get_sync_meta(key) or "[]"))
    db.set_sync_meta(key, json.dumps(sorted(have | {t.strip().lower() for t in titles})))


def _asked(db) -> Dict[str, str]:
    return _load(db, ASKED_KEY)


def _mark_asked(db, titles: Iterable[str], day: date) -> None:
    data = {k: v for k, v in _asked(db).items() if v >= (day - timedelta(days=ASKED_DAYS)).isoformat()}
    for t in titles:
        data[t.strip().lower()] = day.isoformat()
    _save(db, ASKED_KEY, data)


def settle(assistant, day: date) -> Dict[str, Any]:
    """Sort the previous day's unmarked items, once for ``day`` (recorded, so the morning reset and the morning
    message see the same answer): {"ask": titles to ask about, "off_today": Today lines to take off,
    "ended": titles that simply end, "pooled": titles put in the pool}."""
    db = assistant._db
    key = f"leftovers:{day.isoformat()}"
    raw = db.get_sync_meta(key) if db is not None else None
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    out = sort_leftovers(assistant, day)
    if db is not None:
        pool = OpenPool(db)
        done = {t.lower() for t in _done_titles(assistant, day)}
        pool.drop([k for k in pool.all() if k in done])  # done since: out of the pool
        _mark_asked(db, out["ask"], day)
        OpenPool(db).add(out["pool_items"], day - timedelta(days=1))
        db.set_sync_meta(key, json.dumps(out))
    return out


def _done_titles(assistant, day: date) -> List[str]:
    from thirties_core.models import SpanStatus
    out = []
    for back in range(0, POOL_DAYS + 1):
        out += [s.title for s in assistant.pt.plan_for(day - timedelta(days=back)).spans if s.status == SpanStatus.DONE]
    return out


def sort_leftovers(assistant, day: date) -> Dict[str, Any]:
    """The rules above, without recording anything."""
    from thirties_core.kinds import ONCE
    from thirties_core.models import SpanCategory, SpanStatus
    from thirties_core.stats import unreviewed_spans
    from thirties_core.today import TodayLinks, group_items, note_items, with_done
    db = assistant._db
    pt = assistant.pt
    ctx = assistant._kind_context()
    daily = assistant.routines.daily_titles()
    routine_steps = {t.lower() for n in assistant._routines() for t, _ in note_items(n)}
    routine_names = {n["title"].strip().lower() for n in assistant._routines()}
    tasks = {t.id: t for t in assistant.s.tasks}
    listed = {t.title.strip().lower(): t for t in assistant.s.tasks if t.list_role != "today"}
    links = TodayLinks(db) if db is not None else None
    asked_before = _asked(db)
    done_ids = db.completed_since(day - timedelta(days=2)) if db is not None else set()

    yesterday = pt.plan_for(day - timedelta(days=1))
    now = pt._now()
    unmarked = unreviewed_spans(yesterday.spans, now, yesterday.day_start)
    assistant.settle_moot(yesterday, now, ctx)  # yesterday's breakfast isn't eaten today (10-29): it ends
    done_titles = {s.title.strip().lower() for p in (yesterday, pt.plan_for(day)) for s in p.spans
                   if s.status == SpanStatus.DONE}

    note = assistant._today_note()
    today_lines: List[str] = []
    if note is not None:
        open_lines = [t for t, ticked in with_done(note, done_ids) if not ticked]
        children = {c.lower() for t in open_lines for c in (group_items(note, t) or [])}
        today_lines = [t for t in open_lines if t.lower() not in children]  # a group by its name (11-05)
    on_today = {t.lower() for t in today_lines}

    ask: List[str] = []
    ended: List[str] = []
    pool_items: List[Dict[str, str]] = []
    off_today: List[str] = []
    seen = set()

    def never_rolls(title: str, kind_src) -> bool:
        low = title.strip().lower()
        return (low in daily or low in routine_steps or low in routine_names
                or assistant.kind_of(kind_src, ctx) != ONCE)

    def home_of(title: str) -> Optional[Dict[str, str]]:
        return links.home_of(title) if links is not None else None

    def rollable_item(title: str, low: str) -> Dict[str, str]:
        home = home_of(title)
        task = listed.get(low)
        return {"title": title, "task_id": (home or {}).get("home_id") or (task.id if task is not None else "")}

    # yesterday's plan: what ended with no outcome
    for s in unmarked:
        low = s.title.strip().lower()
        if low in seen or low in done_titles or (s.task_id and s.task_id in done_ids):
            continue
        seen.add(low)
        on_a_list = (s.task_id in tasks) or low in on_today or low in listed or home_of(s.title) is not None
        if (s.category == SpanCategory.CALENDAR or s.is_firm or pt._is_fixed(s) or s.status == SpanStatus.UNKNOWN
                or not on_a_list or never_rolls(s.title, s)):
            ended.append(s.title)
            continue
        if low not in asked_before:
            ask.append(s.title)
        pool_items.append(rollable_item(s.title, low))

    tz = pt.day_plan.day_start.tzinfo

    added_today = set(json.loads(db.get_sync_meta(f"today_added:{day.isoformat()}") or "[]")) if db else set()

    def put_on_today_today(title: str) -> bool:  # put on today: today's, not a leftover (a late morning reset)
        if title.strip().lower() in added_today:
            return True
        rec = home_of(title)
        try:
            return bool(rec and rec.get("updated_at")) and \
                datetime.fromisoformat(rec["updated_at"]).astimezone(tz).date() >= day
        except ValueError:
            return False

    # the Today note: what's still open on it from an earlier day
    for title in today_lines:
        low = title.lower()
        if put_on_today_today(title):
            continue
        has_home = home_of(title) is not None or low in listed
        if low in done_titles:
            off_today.append(title)  # done on the plan: the morning reset takes it off
            continue
        if never_rolls(title, title):
            off_today.append(title)
            if low not in seen:
                ended.append(title)
            seen.add(low)
            continue
        if has_home:
            off_today.append(title)  # it waits on its own list
        if low in seen:
            continue
        seen.add(low)
        if low not in asked_before:
            ask.append(title)
        pool_items.append(rollable_item(title, low))
    return {"ask": ask, "off_today": off_today, "ended": ended,
            "pooled": [i["title"] for i in pool_items], "pool_items": pool_items}


def weekday(day: Optional[date]) -> str:
    return day.strftime("%A") if day else "before"


def done_question(titles: List[str]) -> str:
    """One short question for the morning message."""
    if not titles:
        return ""
    if len(titles) == 1:
        return f"Did {titles[0]} get done yesterday?"
    return "Did any of these get done: " + ", ".join(titles[:-1]) + f" or {titles[-1]}?"


def still_unknown(assistant, day: date, titles: List[str]) -> List[str]:
    """Of the titles sorted this morning, those still with no outcome now (one may have been marked done since)."""
    from thirties_core.today import note_items
    db = assistant._db
    done = {t.lower() for t in _done_titles(assistant, day)}
    if db is not None:
        ids = db.completed_since(day - timedelta(days=2))
        done |= {t.title.strip().lower() for t in assistant.s.tasks if t.id in ids}
    note = assistant._today_note()
    done |= {t.lower() for t, ticked in note_items(note) if ticked} if note is not None else set()
    return [t for t in titles if t.strip().lower() not in done]
