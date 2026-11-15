"""Windows and order the user states for today's items, kept by Python wherever an item is placed.

"The icons have to be done before the 4:00 call", "only between 10 and 3", "not before the kids are dropped off":
the model records them (set_item_facts: after, before, not_before, done_by), for an item already on the plan or
one that isn't yet (by its name, for that day). Every placement then goes through one check: adding an item,
moving one, and a proposed day. A placement Python or the model picked is moved to the nearest time that keeps
every constraint, and the reply says so; a time the user named that breaks one is refused with the reason, so the
assistant asks once instead of quietly breaking what the user said.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

KEY = "day_facts:{day}"
USER_TIME_RE = re.compile(r"\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b|\b(at|by|from|until|till) \d{1,2}(:\d{2})?\b|"
                          r"\bnoon\b|\bmidnight\b", re.IGNORECASE)


def _load(db, day: date) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(db.get_sync_meta(KEY.format(day=day.isoformat())) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def record(db, day: date, name: str, before: List[str] = (), after: List[str] = (), not_before: str = "",
           done_by: str = "") -> Dict[str, Any]:
    """Constraints on an item by its name, for one day ('HH:MM' times; other items by name)."""
    data = _load(db, day)
    rec = data.get(name.strip().lower(), {"title": name.strip()})
    if before:
        rec["before"] = sorted(set(rec.get("before", [])) | {b.strip() for b in before if b.strip()})
    if after:
        rec["after"] = sorted(set(rec.get("after", [])) | {a.strip() for a in after if a.strip()})
    if not_before:
        rec["not_before"] = not_before
    if done_by:
        rec["done_by"] = done_by
    data[name.strip().lower()] = rec
    db.set_sync_meta(KEY.format(day=day.isoformat()), json.dumps(data, sort_keys=True))
    return rec


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _at(day_start: datetime, hhmm: str) -> Optional[datetime]:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    t = day_start.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= day_start else t + timedelta(days=1)


def _rules(pt, title: str, span_id: Optional[str]) -> Dict[str, Any]:
    """Everything that holds for this item today: its span facts and what was said about it by name."""
    out: Dict[str, Any] = {"before": [], "after": [], "not_before": None, "done_by": None}
    plan = pt.day_plan
    db = pt.scheduler.state_db if pt.scheduler is not None else None
    if span_id and db is not None:
        f = pt._facts().get(span_id)
        out["after"] += [s.title for s in plan.spans if s.id in f.after]
        out["before"] += [s.title for s in plan.spans if s.id in getattr(f, "before", [])]
        out["not_before"] = f.not_before
        out["done_by"] = getattr(f, "done_by", None)
    if db is not None:
        said = _load(db, plan.target_date).get(title.strip().lower(), {})
        out["before"] += said.get("before", [])
        out["after"] += said.get("after", [])
        for key in ("not_before", "done_by"):
            t = _at(plan.day_start, said.get(key, "")) if said.get(key) else None
            if t is not None and (out[key] is None or (t > out[key] if key == "not_before" else t < out[key])):
                out[key] = t
    return out


def problem(pt, title: str, span_id: Optional[str], start: datetime, end: datetime) -> Optional[str]:
    """Why placing ``title`` at start-end breaks something the user said, or None."""
    from thirties_core.clock import format_clock
    from thirties_core.models import SpanStatus
    rules = _rules(pt, title, span_id)
    others = [s for s in pt.day_plan.spans if s.id != span_id and not s.is_envelope
              and s.status != SpanStatus.SKIPPED and not _same(s.title, title)]
    if rules["not_before"] is not None and start < rules["not_before"]:
        return f"{title} can't start before {format_clock(rules['not_before'])} (the user said so)"
    if rules["done_by"] is not None and end > rules["done_by"]:
        return f"{title} has to be done by {format_clock(rules['done_by'])} (the user said so)"
    for name in rules["after"]:
        for s in others:
            if _same(s.title, name) and s.end_dt > start:
                return f"{title} has to come after {s.title}, which ends at {format_clock(s.end_dt)} (the user said so)"
    for name in rules["before"]:
        for s in others:
            if _same(s.title, name) and s.start_dt < end:
                return f"{title} has to be done before {s.title} at {format_clock(s.start_dt)} (the user said so)"
    for s in others:  # what was said about the other items, pointing at this one
        theirs = _rules(pt, s.title, s.id)
        if any(_same(n, title) for n in theirs["before"]) and start < s.end_dt:
            return f"{s.title} has to be done before {title}, so {title} can't start before {format_clock(s.end_dt)}"
        if any(_same(n, title) for n in theirs["after"]) and end > s.start_dt:
            return f"{s.title} comes after {title}, so {title} has to end by {format_clock(s.start_dt)}"
    return None


def nearest(pt, title: str, span_id: Optional[str], minutes: int, want: datetime) -> Optional[datetime]:
    """The free start nearest ``want`` (from now on, whole thirties) where ``title`` keeps every constraint."""
    from thirties_core.clock import thirty_ceil
    now = pt._now()
    length = timedelta(minutes=minutes)
    candidates = []
    for a, b in pt.open_windows(after=now):
        t = thirty_ceil(max(a, now)) if a < now else a
        while t + length <= b:
            if problem(pt, title, span_id, t, t + length) is None:
                candidates.append(t)
            t += timedelta(minutes=30)
    return min(candidates, key=lambda t: abs((t - want).total_seconds())) if candidates else None


def user_named_time(message: Optional[str]) -> bool:
    return bool(message and USER_TIME_RE.search(message))


def keep(pt, title: str, span_id: Optional[str], start: datetime, end: datetime) -> Tuple[datetime, datetime, str]:
    """The placement, kept to what the user said: unchanged, or moved to the nearest time that keeps it (with a
    note). Raises ValueError with the reason when the user named the time themselves, or nothing fits."""
    from thirties_core.clock import format_clock
    why = problem(pt, title, span_id, start, end)
    if why is None:
        return start, end, ""
    if user_named_time(pt.user_message):
        raise ValueError(why + ". Ask the user once which matters more, rather than breaking it.")
    minutes = int((end - start).total_seconds() // 60)
    slot = nearest(pt, title, span_id, minutes, start)
    if slot is None:
        raise ValueError(why + ", and no free time today keeps it. Ask the user once what to do.")
    return slot, slot + timedelta(minutes=minutes), f" (at {format_clock(slot)}: {why})"


__all__ = ["record", "problem", "nearest", "keep", "user_named_time"]
