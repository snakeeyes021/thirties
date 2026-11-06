"""What happens to yesterday's unmarked items: the rule set that ends the pile-up.

11-04, 11-05: every open line on the Today note came back every morning, a week's worth piled up. Matt (11-05):
a person planning a day tracks whole items, at several horizons, and works out when each can go; an assistant
that can't see what was done has to find out, once.

The rules, applied once a day when the day is built (after the user's first reply, wake.py):

1. Unmarked means unknown: not "not done", and not "do it today".
2. Some things never roll. Once their day is over they end, unasked: routine steps, meals and daily anchors,
   time on a project and other ongoing activities, calendar events and anything fixed to a time, and anything
   gone moot. Nothing is "on no list": what Thirties plans lands on the Today note at least, so its type decides.
3. Each one-off left over is in one morning proposal, after the user is up, and never again: keep it at a
   natural horizon (today, tomorrow, this week, this weekend, before <day>, by <day>, next week, next month,
   someday), or drop it. Python makes the first guess, from what the item carries (first match wins):
   - today: it can't wait (urgency "now", or its horizon is today/tomorrow), it's due today or tomorrow, or
     it's quick (15 minutes or less, as the user said);
   - by its deadline: a due date further off;
   - someday: the user put it there (someday, a someday section, a pick-from menu list);
   - this week: everything else; when unsure, this week, never today.
   The morning model revises it (propose_leftovers): drops what has gone moot (a one-time event whose day is
   gone), moves what fits better elsewhere. Python works out the days of each horizon (resolve_when).
4. Kept items wait at their horizon straight away; drops wait for a yes (a reply, or a 👍). The user can say
   what to change (place_leftover) or what's already done. No answer: kept items stay where proposed, and
   nothing is dropped. Recurring things never enter the pool: a missed one skips to its next turn.
5. The daily proposal draws from the pool by fit: anything due or urgent first, then what's meant for today
   (or whose window ends today), then what's within its window where free time fits; never before its window,
   never someday or dropped ones. An item leaves the pool once it's done, or after two weeks (it's still on its
   list).
6. The Today note holds today's plan: the morning reset takes leftovers off it when their home item is still
   open on its list. An item with no other list stays, and anything put on Today today is never touched.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

POOL_KEY = "open_pool"          # {title lower: {"title", "task_id", "since", "horizon", "due"}}
TODAY, WEEK, DEADLINE, SOMEDAY, DROPPED = "today", "week", "deadline", "someday", "dropped"
POOL_HORIZONS = (TODAY, WEEK, DEADLINE, SOMEDAY, DROPPED)
SAID = {TODAY: "today", WEEK: "this week", DEADLINE: "by its deadline", SOMEDAY: "someday", DROPPED: "dropped"}
QUICK_MINUTES = 15
DEADLINE_PULL_DAYS = 7
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
        """Every record; with ``today``, only those still waiting to be offered (not dropped, not two weeks
        old). Dropped ones are kept, in the archive (rows()): nothing is ever deleted."""
        data = _load(self.db, POOL_KEY)
        if today is not None:
            cutoff = (today - timedelta(days=POOL_DAYS)).isoformat()
            data = {k: v for k, v in data.items() if v.get("since", "") >= cutoff and v.get("horizon") != DROPPED}
        return data

    def apply_drops(self, today: date) -> List[str]:
        """A drop proposed on an earlier day that the user never touched is applied once that day is over
        (11-06: proposed for a day, then applied by itself); it goes to the archive, with its reason."""
        data = _load(self.db, POOL_KEY)
        done = []
        for rec in data.values():
            if rec.get("pending_drop") and rec.get("proposed_on", today.isoformat()) < today.isoformat():
                rec.update({"horizon": DROPPED, "pending_drop": False, "dropped_on": today.isoformat(),
                            "dropped_by": "app"})
                done.append(rec["title"])
        if done:
            _save(self.db, POOL_KEY, data)
        return done

    def set(self, title: str, **fields) -> Optional[Dict[str, str]]:
        data = _load(self.db, POOL_KEY)
        key = title.strip().lower()
        rec = data.get(key) or next((v for k, v in data.items() if key and key in k), None)
        if rec is None:
            return None
        rec.update(fields)
        data[rec["title"].strip().lower()] = rec
        _save(self.db, POOL_KEY, data)
        return rec

    def add(self, items: Iterable[Dict[str, str]], since: date) -> None:
        """New items come in at their horizon; an item already waiting keeps its first day and, if the user
        placed it, their horizon."""
        data = _load(self.db, POOL_KEY)  # every record: an old or dropped one is kept, never overwritten away
        for item in items:
            key = item["title"].strip().lower()
            old = data.get(key)
            rec = {"title": item["title"].strip(), "task_id": item.get("task_id") or "",
                   "since": (old or {}).get("since") or since.isoformat(),
                   "horizon": item.get("horizon") or WEEK, "due": item.get("due") or ""}
            rec.update({k: item[k] for k in ("when", "start", "end", "why", "pending_drop", "proposed_on", "slot")
                        if k in item})
            if old and old.get("placed_by") == "user":
                rec.update({k: old[k] for k in ("horizon", "when", "start", "end", "placed_by") if k in old})
            data[key] = rec
        _save(self.db, POOL_KEY, data)

    def place(self, title: str, horizon: str, window: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """The user moved it: their word stands until it's done."""
        data = _load(self.db, POOL_KEY)
        key = title.strip().lower()
        rec = data.get(key) or next((v for k, v in data.items() if key in k), None)
        if rec is None:
            return None
        rec["horizon"], rec["placed_by"] = horizon, "user"
        rec.pop("pending_drop", None)
        if window is not None:
            rec.update(window)
        data[rec["title"].strip().lower()] = rec
        _save(self.db, POOL_KEY, data)
        return rec

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


def mark_asked(db, titles: Iterable[str], day: date) -> None:
    """Recorded when the question is really put to the user, never just because the day was sorted."""
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
        gone = pool.apply_drops(day)  # drops proposed on an earlier day and never touched: applied, archived
        if gone:
            try:
                assistant._drop_leftovers([{"title": t} for t in gone])  # off the Today note too
            except Exception:
                pass
        done = {t.lower() for t in _done_titles(assistant, day)}
        pool.drop([k for k in pool.all() if k in done])  # done since: out of the pool
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
        task = tasks.get((home or {}).get("home_id", "")) or listed.get(low)
        return {"title": title, "task_id": (home or {}).get("home_id") or (task.id if task is not None else ""),
                "horizon": choose_horizon(assistant, task, title, day), "due": (task.due or "")[:10] if task else ""}

    # yesterday's plan: what ended with no outcome
    for s in unmarked:
        low = s.title.strip().lower()
        if low in seen or low in done_titles or (s.task_id and s.task_id in done_ids):
            continue
        seen.add(low)
        # no item is ever "on no list" (11-05): what Thirties plans lands on the Today note at least, so its type
        # decides: a calendar event or anything fixed to a time ends; a task is proposed
        if (s.category == SpanCategory.CALENDAR or s.is_firm or pt._is_fixed(s) or s.status == SpanStatus.UNKNOWN
                or never_rolls(s.title, s)):
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
        link = home_of(title)  # leaves Today only if its home item is really still open on its list
        has_home = bool(link and link.get("home_id") in tasks and tasks[link["home_id"]].list_role != "today") \
            or low in listed
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


def where_text(items: List[Dict[str, str]], short=lambda t: t) -> str:
    """Where the not-done ones wait, in a sentence: this week unless said otherwise, naming only the others."""
    if not items:
        return ""

    def label(i) -> str:
        h = i.get("horizon") or WEEK
        if h == DEADLINE and i.get("due"):
            return f"by {date.fromisoformat(i['due']).strftime('%a %b %-d')}"
        return SAID[h]
    others = [f"{short(i['title'])} ({label(i)})" for i in items if (i.get("horizon") or WEEK) != WEEK]
    if len(others) == len(items):
        return "Whatever wasn't done waits on its list: " + ", ".join(others) + "."
    return "Whatever wasn't done waits on its list for this week" + (
        ", except " + ", ".join(others) if others else "") + "."


def choose_horizon(assistant, task, title: str, day: date, said_minutes: Optional[int] = None) -> str:
    """The pool horizon for an item not done (rules above, first match wins), from what it already carries:
    its urgency and horizon, its due date, its length as the user gave it, its list."""
    due = None
    if task is not None and task.due:
        try:
            due = date.fromisoformat(task.due[:10])
        except ValueError:
            due = None
    horizon = ""
    if task is not None:
        try:
            from thirties_core.horizons import HorizonPlanner
            horizon = HorizonPlanner(assistant.pt).horizon_of(task) or ""
        except Exception:
            horizon = ""
    minutes = said_minutes
    if minutes is None:
        try:
            minutes = assistant.lasting_facts(task.title if task is not None else title).minutes
        except Exception:
            minutes = None
    if task is not None and (task.someday or task.list_role in ("menu", "backlog")) or horizon == "someday":
        return SOMEDAY
    if (task is not None and task.urgency == "now") or horizon == "day" or (due is not None and (due - day).days <= 1) \
            or (minutes is not None and 0 < minutes <= QUICK_MINUTES):
        return TODAY
    if due is not None:
        return DEADLINE
    return WEEK


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


# --- the morning proposal: what to do with each leftover (11-05: the model proposes, the user confirms) ---

NATURAL = ("today", "tomorrow", "this week", "this weekend", "before <day>", "by <day>", "next week", "this month",
           "next month", "someday")


def _week_bounds(day: date, week_start: str = "mon") -> tuple:
    first = 6 if str(week_start).lower().startswith("sun") else 0  # date.weekday(): Monday is 0
    start = day - timedelta(days=(day.weekday() - first) % 7)
    return start, start + timedelta(days=6)


def resolve_when(text: str, day: date, week_start: str = "mon") -> Dict[str, str]:
    """A natural horizon ("today", "this weekend", "before friday", "next month", "someday") as a window of days,
    worked out in Python: {"when": the words, "start", "end"} ("" for someday). Raises ValueError if unclear."""
    import calendar
    import re
    from thirties_core.clock import parse_day
    raw = " ".join(str(text or "").strip().lower().split())
    if not raw:
        raise ValueError("say when: today, tomorrow, this week, this weekend, before <day>, next week, next month "
                         "or someday")

    def window(a: date, b: date) -> Dict[str, str]:
        a = max(a, day)
        return {"when": raw, "start": a.isoformat(), "end": max(a, b).isoformat()}
    if raw in ("someday", "some day", "later", "backlog", "eventually", "no rush"):
        return {"when": "someday", "start": "", "end": ""}
    w_start, w_end = _week_bounds(day, week_start)
    if raw in ("this week", "sometime this week", "later this week"):
        return window(day, w_end)
    if raw in ("this weekend", "the weekend", "weekend", "over the weekend"):
        sat = day + timedelta(days=(5 - day.weekday()) % 7)
        if day.weekday() == 6:
            return window(day, day)
        return window(sat, sat + timedelta(days=1))
    if raw in ("next week", "sometime next week"):
        return window(w_start + timedelta(days=7), w_end + timedelta(days=7))
    if raw in ("this month", "sometime this month", "later this month"):
        return window(day, date(day.year, day.month, calendar.monthrange(day.year, day.month)[1]))
    if raw in ("next month", "sometime next month"):
        y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return window(date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]))
    m = re.fullmatch(r"(?:sometime |some time |any time )?(before|by|until) (.+)", raw)
    if m:
        target = parse_day(m.group(2), day)
        if target is None:
            raise ValueError(f"which day is '{m.group(2)}'?")
        end = target - timedelta(days=1) if m.group(1) == "before" else target
        return window(day, end)
    target = parse_day(raw, day)
    if target is None:
        raise ValueError(f"'{text}' isn't a horizon I know: " + ", ".join(NATURAL))
    return window(target, target)


def default_when(horizon: str, due: str, day: date, week_start: str = "mon") -> Dict[str, str]:
    """Python's first guess (choose_horizon) as a natural horizon, for when no model can propose."""
    if horizon == TODAY:
        return resolve_when("today", day, week_start)
    if horizon == SOMEDAY:
        return resolve_when("someday", day, week_start)
    if horizon == DEADLINE and due:
        try:
            d = date.fromisoformat(due[:10])
            return {**resolve_when(f"by {d.isoformat()}", day, week_start),
                    "when": f"by {d.strftime('%A') if (d - day).days < 7 else d.strftime('%b %-d')}"}
        except ValueError:
            pass
    return resolve_when("this week", day, week_start)


PROPOSAL_INTRO = "Here's what I'd do with what's left from before:"  # copy-madlibs T3-005
KEEP_HEADING = "Keep:"                                                # T3-006
DROP_HEADING = "Drop:"                                                # T3-007
PROPOSAL_ASK = ("Does that look right? A yes or a 👍 will do. Tell me what to change, or what you already "
                "finished.")                                          # T3-008


def proposal_lines(keep: List[Dict[str, str]], drop: List[Dict[str, str]], short=lambda t: t) -> str:
    """The Keep / Drop block: one item per line, no bullets (11-05)."""
    out = []
    if keep:
        out.append(KEEP_HEADING)
        out += [f"{short(k['title'])} – {k['when']}" for k in keep]
    if drop:
        out.append(DROP_HEADING)
        out += [short(d["title"]) for d in drop]
    return "\n".join(out)


def notice_text(kept: int, on_today: int, drops: int, overflow: List[str]) -> str:
    """The morning's word on the leftovers when no model writes it (copy-madlibs T3-023 to T3-025): what was
    done, not a question; the one question is only for what doesn't fit today."""
    if not kept and not drops:
        return ""
    out = f"Kept {kept} from before ({on_today} on today's plan)"
    out += f", and I'd drop {drops}" if drops else ""
    out += ": see Before today."
    if overflow:
        out += ("\nDoesn't fit today: " + ", ".join(overflow) + ". Drop, move to another day, or squeeze in?")
    return out
