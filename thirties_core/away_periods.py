"""Away from home for some days: the fact, kept, and what the model chose to pause for it.

The user says they'll be away for a few days (where, until when, and maybe who looks after something at home
meanwhile). Before, nothing kept that, and the next morning planned the whole at-home day again: routines, meals,
home chores, each to be taken off by hand.

Python keeps the facts; the model decides what they mean for each item:

- **An away period** (``set_away``, ``end_away``): start and end (the last day away), an optional time back on
  that last day, the place and a note, both in the user's own words. Python never reads the user's message to make
  one: the model records it with the tool.
- **What pauses** (``pause_while_away``): the model names the routines, daily anchors and items that can't happen
  while the user is away; things they can do anywhere stay. A pause lasts exactly as long as its period: ending the
  period early ends the pauses with it, and the first morning back everything plans as usual again.
- **The facts** (``prompt_section``, ``morning_facts``): while a day is inside a period (or one starts within the
  week), the turn and the morning are told so plainly, with what's paused and what isn't yet.
- **Never planned while paused**: the morning set-up leaves paused routines and anchors off (routines.py), the
  morning proposal leaves paused items out (day_proposal.py), and no bell or check-in goes out for them
  (background.py).
- **Said once**: the first morning inside a period says, in one plain line, that the user is away until when
  (``morning_line``); a morning reply that leaves it out gets it added (morning_day.place_day_list).

Example (invented): away at "the glaze fair in Elmbrook" from Friday through Monday, back around 7:40 PM Monday,
note "Ines opens the studio". The model pauses the "Kiln room" routine and "Studio supper"; "Sketch new mugs"
stays (it can be done anywhere).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

KEY = "away_periods"  # in shared_settings: synced, last write wins
AHEAD_DAYS = 7  # a period starting within this many days is in the facts already
SAID_KEY = "away_said:{id}"  # sync_meta: the morning (a date) that said the period


AWAY_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "set_away",
        "description": "Record that the user will be away from home for some days, when they say so: where, from "
                       "when, until when, and anything they add (who looks after what at home). Then decide, item "
                       "by item, what can't happen while they're away (the result lists the routines, meals and "
                       "items to decide on) and pause those with pause_while_away; what they can do anywhere "
                       "stays. Changing an existing period: call again with its dates.",
        "parameters": {"start": "The first day away (default: today), e.g. 'Friday' or 'Dec 4'.",
                       "end": "The last day away (the day they get back, if they get back during that day).",
                       "back_at": "The time they're back on the last day, if they said one, e.g. '7:40 PM'.",
                       "place": "Where they'll be, in their own words.",
                       "note": "Anything else they said about it, in their words (e.g. who covers a chore)."},
        "required": ["end"],
    },
    {
        "name": "end_away",
        "description": "The user is back before the planned end of their time away (\"back early\", \"home "
                       "already\"), or the trip is off. Everything paused for it plans as usual again.",
        "parameters": {"cancel": "true when the whole away period is called off."},
        "required": [],
    },
    {
        "name": "pause_while_away",
        "description": "Pause routines, daily anchors (meals and the like) or items for the user's time away: "
                       "they aren't planned, rung or asked about on those days, and come back by themselves the "
                       "first day back. Only what can't happen away from home; what the user can do anywhere "
                       "stays. keep: names to take off the paused list (they said they'll still do it).",
        "parameters": {"names": "A list of routines, anchors or items to pause, by their names.",
                       "keep": "A list of paused names that should go on after all."},
        "required": [],
    },
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- the store ---

def periods(db) -> List[Dict[str, Any]]:
    """Every away period, as {"id", "start", "end", "back_at", "place", "note", "paused": [...]}, in order."""
    if db is None:
        return []
    try:
        data = json.loads(db.get_shared_setting(KEY) or "[]")
    except ValueError:
        return []
    out = [p for p in data if isinstance(p, dict) and p.get("start") and p.get("end")]
    return sorted(out, key=lambda p: p["start"])


def _save(db, items: List[Dict[str, Any]]) -> None:
    db.set_shared_setting(KEY, json.dumps(sorted(items, key=lambda p: p["start"])), _now_iso())


def on(db, day: date) -> Optional[Dict[str, Any]]:
    """The away period this day falls in (start through end, both included), or None."""
    iso = day.isoformat()
    return next((p for p in periods(db) if p["start"] <= iso <= p["end"]), None)


def current_or_next(db, day: date, ahead: int = AHEAD_DAYS) -> Optional[Dict[str, Any]]:
    """Period holding ``day``, else the next one starting within ``ahead`` days."""
    found = on(db, day)
    if found is not None:
        return found
    last = (day + timedelta(days=ahead)).isoformat()
    return next((p for p in periods(db) if day.isoformat() < p["start"] <= last), None)


def _norm(text: str) -> str:
    from thirties_core.step_times import key as step_key
    return step_key(str(text or ""))


def paused_on(db, name: str, day: date) -> bool:
    """``name`` (a routine, an anchor, an item, or one of a paused routine's steps) is paused on ``day``."""
    p = on(db, day)
    if p is None or not name:
        return False
    k = _norm(name)
    return any(k == _norm(x.get("name", "")) or k in (x.get("keys") or []) for x in p.get("paused") or [])


def paused_span(db, span) -> bool:
    """A planned item that's paused on its own day (no bell, no check-in)."""
    try:
        return paused_on(db, span.title, span.start_dt.date())
    except Exception:
        return False


def kept_steps(db, day: date, names: Iterable[str]) -> List[str]:
    """The names (a routine part's steps, a day's rows) that aren't paused on ``day``: the one test every
    renderer and the morning set-up use, so a paused step is never planned or listed while the time away lasts."""
    names = list(names)
    if db is None or on(db, day) is None:
        return names
    return [n for n in names if not paused_on(db, str(n), day)]


def settle(db, plan, persist=None) -> int:
    """Planned items paused for their own day (inside a time away) become skipped, so no list, view, bell or
    check-in shows them. Called by the day builder (scheduler.py), by the set-up, and by pause_while_away. Returns
    the count."""
    from thirties_core.models import SpanStatus
    if db is None or on(db, plan.target_date) is None:
        return 0
    count = 0
    for s in getattr(plan, "spans", []):
        if s.is_envelope or s.status != SpanStatus.PLANNED or s.source == "calendar":
            continue
        if not kept_steps(db, s.start_dt.date(), [s.title]):
            s.status = SpanStatus.SKIPPED
            (persist or db.upsert_span)(s)
            count += 1
    return count


def not_paused(db, day: date, cands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The day's proposal candidates without the paused ones."""
    if on(db, day) is None:
        return cands
    keep = {str(t).lower() for t in kept_steps(db, day, [c.get("title", "") for c in cands])}
    return [c for c in cands if str(c.get("title", "")).lower() in keep]


# --- the words ---

def _day(d: date, today: date) -> str:
    gap = (d - today).days
    return ("today" if gap == 0 else "tomorrow" if gap == 1 else d.strftime("%A")) + d.strftime(", %b %-d")


def describe(p: Dict[str, Any], today: date) -> str:
    """One period in plain words, for the model."""
    start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
    where = f" at {p['place']}" if p.get("place") else ""
    if start <= today:
        out = f"The user is away{where} until {_day(end, today)}"
    else:
        out = f"The user will be away{where} from {_day(start, today)} until {_day(end, today)}"
    if p.get("back_at"):
        out += f"; back {p['back_at']} that day"
    if p.get("note"):
        out += f"; note (their words): {p['note']}"
    return out + "."


def morning_line(db, day: date) -> str:
    """The one plain line the first morning inside a period says ("" on any other morning)."""
    p = on(db, day)
    if p is None:
        return ""
    said = db.get_sync_meta(SAID_KEY.format(id=p["id"])) or ""
    if said and said != day.isoformat():
        return ""  # said on an earlier morning: never again
    end = date.fromisoformat(p["end"])
    line = f"You're away until {end.strftime('%A')}" + (f" ({p['place']})" if p.get("place") else "") + "."
    return line


def mark_said(db, day: date) -> None:
    p = on(db, day)
    if p is not None and not db.get_sync_meta(SAID_KEY.format(id=p["id"])):
        db.set_sync_meta(SAID_KEY.format(id=p["id"]), day.isoformat())


def _candidates(assistant, p: Dict[str, Any], day: date) -> List[str]:
    """Names the model can still pause: routine notes, daily anchors and the day's planned items."""
    paused = {_norm(x.get("name", "")) for x in p.get("paused") or []}
    out: List[str] = []
    try:
        out += [n["title"] for n in assistant._routines()]
    except Exception as e:
        logger.warning("Couldn't list the routines: %s", e)
    try:
        out += [a.title for a in assistant.routines.book().anchors()]
    except Exception as e:
        logger.warning("Couldn't list the anchors: %s", e)
    try:
        daily = assistant.routines.daily_titles()
        from thirties_core.models import SpanStatus
        out += [s.title for s in assistant.pt.day_plan.spans if not s.is_envelope and s.status == SpanStatus.PLANNED
                and s.title.strip().lower() not in daily and not s.id.startswith("routine-")]
    except Exception as e:
        logger.warning("Couldn't list the day's items: %s", e)
    seen, uniq = set(), []
    for t in out:
        k = _norm(t)
        if k and k not in seen and k not in paused:
            seen.add(k)
            uniq.append(t)
    return uniq


def facts(assistant, day: Optional[date] = None) -> str:
    """The away facts for the model ("" when no period is on or near)."""
    db = assistant._db
    if db is None:
        return ""
    day = day or assistant._today()
    p = current_or_next(db, day)
    if p is None:
        return ""
    paused = [x["name"] for x in p.get("paused") or []]
    open_ = _candidates(assistant, p, day)
    lines = [describe(p, day),
             "Paused for that time (not planned, rung or asked about; back by themselves the first day back): "
             + (", ".join(paused) if paused else "nothing yet") + ".",
             "Not paused: " + (", ".join(open_) if open_ else "nothing") + ". For each, decide whether it can "
             "happen while the user is away (things tied to home can't; what they can do anywhere can) and pause "
             "the ones that can't with pause_while_away. Never put a paused one back on the plan unless the user "
             "asks."]
    return "AWAY FROM HOME\n" + "\n".join(lines)


def prompt_section(assistant) -> str:
    """For the turn's context."""
    try:
        text = facts(assistant)
    except Exception as e:  # never costs the user their turn
        logger.warning("Couldn't say the away facts: %s", e)
        return ""
    return text + "\n\n" if text else ""


def morning_facts(assistant, day: date) -> str:
    """Away facts for the morning message, plus its one line on the first morning of a period."""
    db = assistant._db
    text = facts(assistant, day)
    line = morning_line(db, day) if db is not None else ""
    if line:
        text += ("\nFirst morning of the time away: state it once, a single line of your own: " + line)
    return text


def say_once(assistant, day: date, reply: str) -> str:
    """A morning reply on the first morning inside a period carries its line (added if the reply leaves it out);
    the period counts as said from then on."""
    db = getattr(assistant, "_db", None)
    if db is None:
        return reply
    line = morning_line(db, day)
    if not line:
        return reply
    p = on(db, day) or {}
    end = date.fromisoformat(p["end"])
    low = (reply or "").lower()
    named = [w for w in (str(p.get("place") or "").lower(), end.strftime("%A").lower()) if w]
    if not ("away" in low and any(w in low for w in named)):
        reply = line + ("\n\n" + reply if (reply or "").strip() else "")
    mark_said(db, day)
    return reply


# --- the tools ---

def _err(text: str):
    from thirties_core.assistant_tools import ToolError
    return ToolError(text)


def _parse(value: Any, today: date, what: str) -> date:
    from thirties_core.clock import ClockError, parse_day
    try:
        return parse_day(str(value), today)
    except ClockError as e:
        raise _err(f"{what}: {e}")


def set_away(pt, start: Any = None, end: Any = None, back_at: Any = None, place: Any = None, note: Any = None,
             **_: Any) -> str:
    db = pt.scheduler.state_db if pt.scheduler is not None else None
    if db is None:
        raise _err("Time away can't be saved here.")
    today = pt.today()
    first = _parse(start, today, "start") if start not in (None, "") else today
    if end in (None, ""):
        raise _err("Until which day? Give end (the last day away).")
    last = _parse(end, today, "end")
    if last < first and first.month == 12 and last.month == 1:
        last = last.replace(year=first.year + 1)
    if last < first:
        raise _err(f"{last.strftime('%a %b %-d')} is before {first.strftime('%a %b %-d')}.")
    back = ""
    if back_at not in (None, ""):
        from thirties_core.clock import ClockError, format_clock, parse_clock
        try:
            back = format_clock(datetime.combine(last, parse_clock(str(back_at))))
        except ClockError as e:
            raise _err(f"back_at: {e}")
    items = periods(db)
    mine = next((p for p in items if p["start"] <= last.isoformat() and first.isoformat() <= p["end"]), None)
    if mine is None:
        mine = {"id": uuid.uuid4().hex[:12], "paused": []}
        items.append(mine)
    mine.update({"start": first.isoformat(), "end": last.isoformat(), "back_at": back,
                 "place": str(place or mine.get("place") or "").strip(),
                 "note": str(note or mine.get("note") or "").strip()})
    _save(db, items)
    assistant = getattr(pt, "assistant", None)
    out = "Saved: " + describe(mine, today)
    if assistant is not None:
        open_ = _candidates(assistant, mine, today)
        if open_:
            out += (" Pause with pause_while_away whichever of these need home: "
                    + ", ".join(open_) + ".")
    return out


def end_away(pt, cancel: Any = None, **_: Any) -> str:
    db = pt.scheduler.state_db if pt.scheduler is not None else None
    if db is None:
        raise _err("Time away can't be saved here.")
    today = pt.today()
    cancel = str(cancel).strip().lower() in ("true", "1", "yes")
    items = periods(db)
    now_on = on(db, today)
    if cancel:
        target = now_on or next((p for p in items if p["start"] > today.isoformat()), None)
        if target is None:
            raise _err("No away period is stored.")
        items = [p for p in items if p["id"] != target["id"]]
        _save(db, items)
        out = "The time away is off; everything paused for it plans as usual again."
    else:
        if now_on is None:
            raise _err("The user isn't away on record today.")
        if now_on["start"] >= today.isoformat():
            items = [p for p in items if p["id"] != now_on["id"]]
        else:
            for p in items:
                if p["id"] == now_on["id"]:
                    p["end"], p["back_at"] = (today - timedelta(days=1)).isoformat(), ""
        _save(db, items)
        out = "Back home: everything paused for the time away plans as usual again from today."
    placed = _back_on_today(pt)
    return out + (" Put back on today: " + "; ".join(placed) + "." if placed else "")


def _back_on_today(pt) -> List[str]:
    """Today's routines and anchors that were left off for the time away, put on now where there's still time."""
    assistant = getattr(pt, "assistant", None)
    if assistant is None or pt.scheduler is None or not pt.is_current(pt.day_plan):
        return []
    r = assistant.routines
    plan = pt.day_plan
    if not r.set_up_state(plan.target_date):
        return []  # not set up yet: the set-up puts them on as usual
    state: Dict[str, Any] = {"state": "done", "placed": [], "moved": [], "unplaced": [], "on_today": []}
    saved, r.placing = pt.day_plan, True
    try:
        r._place_routines(plan, pt._now(), state)
        r._place_anchors(plan, pt._now(), state)
    except Exception as e:
        logger.warning("Couldn't put today's routines back: %s", e)
    finally:
        pt.day_plan, r.placing = saved, False
    return state["placed"]


def _resolve(pt, name: str) -> Dict[str, Any]:
    """A name the model gave, as a paused entry: a routine (with its steps), an anchor, or an item."""
    assistant = pt.assistant
    from thirties_core.step_times import key as step_key
    for note in assistant._routines():
        if step_key(note["title"]) == step_key(name):
            book = assistant.routines.book()
            rec = book.get(note["id"], note["title"])
            keys = {step_key(s) for s in assistant.routines.steps(note)} | {
                step_key(i) for part in rec.parts for i in part.get("items", [])}
            return {"kind": "routine", "name": note["title"], "keys": sorted(k for k in keys if k)}
    for note in assistant._routines():  # one step of a routine
        for step in assistant.routines.steps(note):
            if step_key(step) == step_key(name):
                return {"kind": "step", "name": step, "keys": [step_key(step)]}
    for a in assistant.routines.book().anchors():
        if a.key == name.strip().lower():
            return {"kind": "anchor", "name": a.title, "keys": [step_key(a.title)]}
    task = assistant._exact_task(name)
    if task is not None:
        return {"kind": "item", "name": task.title, "keys": [step_key(task.title)]}
    if any(step_key(s.title) == step_key(name) for s in pt.day_plan.spans if not s.is_envelope):
        return {"kind": "item", "name": name.strip(), "keys": [step_key(name)]}
    raise _err(f"No routine, anchor or item is called {name!r}.")


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            value = loaded if isinstance(loaded, list) else [value]
        except ValueError:
            value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def pause_while_away(pt, names: Any = None, keep: Any = None, **_: Any) -> str:
    db = pt.scheduler.state_db if pt.scheduler is not None else None
    if db is None or getattr(pt, "assistant", None) is None:
        raise _err("Time away can't be saved here.")
    today = pt.today()
    p = current_or_next(db, today)
    if p is None:
        raise _err("No away period is stored: set_away comes first.")
    names, keep = _as_list(names), _as_list(keep)
    if not names and not keep:
        raise _err("Give names (what to pause) or keep (what goes on after all).")
    entries = [_resolve(pt, n) for n in names]
    items = periods(db)
    mine = next(x for x in items if x["id"] == p["id"])
    paused = [x for x in mine.get("paused") or [] if _norm(x["name"]) not in {_norm(k) for k in keep}]
    have = {_norm(x["name"]) for x in paused}
    paused += [e for e in entries if _norm(e["name"]) not in have]
    mine["paused"] = paused
    _save(db, items)
    off = _off_the_plan(pt, entries)
    end = date.fromisoformat(mine["end"])
    out = []
    if entries:
        out.append(f"Paused through {end.strftime('%a %b %-d')}: " + ", ".join(e["name"] for e in entries) + "."
                   + (f" {off}" if off else ""))
    if keep:
        out.append("Going on as usual: " + ", ".join(keep) + ".")
    return " ".join(out)


def _off_the_plan(pt, entries: Iterable[Dict[str, Any]]) -> str:
    """What's already planned on a paused day for what was just paused is marked skipped (settle): the plans in
    memory now; later days as they're built."""
    db = pt.scheduler.state_db
    plans, seen, count = [pt.day_plan] + list(getattr(pt, "_other_days", {}).values()), set(), 0
    for plan in plans:
        if id(plan) not in seen:
            seen.add(id(plan))
            count += settle(db, plan, pt._persist)
    return f"{count} planned item{'s' if count != 1 else ''} taken off the plan." if count else ""


__all__ = ["AWAY_TOOL_SPECS", "periods", "on", "current_or_next", "paused_on", "paused_span", "kept_steps", "settle",
           "not_paused",
           "describe", "morning_line", "mark_said", "facts", "prompt_section", "morning_facts", "say_once",
           "set_away", "end_away", "pause_while_away"]
