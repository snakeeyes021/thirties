"""After repeated deferrals, an offer to break the item down into smaller pieces.

Code, not a prompt line:
- **When.** The morning's one question about an item missed three days running (backoff.py) gets a fourth choice,
  "Break it into smaller pieces", when the item can be broken down. An item put off DEFER_AFTER or more times (skipped,
  or moved to another day: its deferral count) gets the offer on its own line, once. ``morning_question`` writes
  both, in the morning's Before today message.
- **Which items.** A thing to do once, on a list or a project (not a routine, a menu option, a daily or ongoing thing,
  a meal, a ticket, someday or a wishlist item), with no open pieces under it yet: one that has them is already offered
  piece by piece (project_pieces.first_pieces).
- **Then.** Choosing it (``answer``, through still_want) holds the item back from the day's options until it has
  pieces, and tells the model to propose a few small first steps; the user agrees, and break_down_task writes them
  under the item in Joplin (the existing safe write). Once the item has open pieces (``release``, each morning and
  each turn), its run of misses ends and its first piece is offered like any project piece ("Item: piece"); the
  last piece ticked ticks the item.

Which steps, and how to word them, is judgement: the model's.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFER_AFTER = 3            # times put off before the offer
OFFERS_KEY = "break_down_offers"   # {task_id: {"title", "asked", "answer"}}: deferral offers, asked once
BREAK = "break"
BREAK_LINE = " / Break it into smaller pieces"
DEFER_QUESTION = "{title} has been put off {n} times. Break it into smaller pieces? Break it down / Keep as it is"
BREAKABLE_ROLES = ("list", "project", "dropbox", "")


def _task(assistant, title: str = "", task_id: str = ""):
    tasks = list(assistant.s.tasks)
    if task_id:
        hit = next((t for t in tasks if t.id == task_id), None)
        if hit is not None:
            return hit
    low = (title or "").strip().lower()
    hits = [t for t in tasks if t.title.strip().lower() == low and t.list_role != "today"]
    return hits[0] if len(hits) == 1 else None


def _has_open_pieces(assistant, task) -> bool:
    from thirties_core.project_pieces import open_children
    db = assistant._db
    done = db.completed_since(assistant.pt.day_plan.target_date - timedelta(days=366)) if db is not None else set()
    return bool(open_children(assistant.s.tasks, task, done))


def can_break(assistant, task) -> bool:
    """Whether an item can be offered to be broken down (see the module notes)."""
    if task is None or str(task.id).startswith("media:") or task.someday:
        return False
    if task.list_role not in BREAKABLE_ROLES:
        return False
    from thirties_core.kinds import is_meal, kind_of
    from thirties_core.backlog_tickets import is_ticket
    if is_ticket(task) or is_meal(task.title):
        return False
    try:
        kind = kind_of(task.title, task)
    except Exception:
        kind = "once"
    if kind in ("daily", "ongoing"):
        return False
    try:
        said = assistant.lasting_facts(task.title).kind
    except Exception:
        said = ""
    if said in ("daily", "ongoing"):
        return False
    return not _has_open_pieces(assistant, task)


# --- the deferral offers' own record ---

def _offers(db) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(db.get_sync_meta(OFFERS_KEY) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(db, data: Dict[str, Dict[str, Any]]) -> None:
    db.set_sync_meta(OFFERS_KEY, json.dumps(data))


def morning_question(assistant, still: List[Dict[str, Any]], short=lambda t: t) -> str:
    """The morning's question lines: each item missed three days running with its choices (and breaking it down,
    where it can be), then each breakable item put off DEFER_AFTER times that wasn't asked about yet. Marks the
    deferral offers asked (the caller marks the missed ones)."""
    from thirties_core.backoff import QUESTION
    lines = []
    asked_ids = set()
    for r in still:
        task = _task(assistant, r.get("title", ""), r.get("task_id", ""))
        line = QUESTION.format(title=short(r["title"]))
        if can_break(assistant, task):
            line += BREAK_LINE
        lines.append(line)
        if task is not None:
            asked_ids.add(task.id)
    db = assistant._db
    if db is not None:
        offers = _offers(db)
        today = assistant.pt.day_plan.target_date.isoformat()
        for t in assistant.s.tasks:
            if (t.deferred_count >= DEFER_AFTER and t.id not in offers and t.id not in asked_ids
                    and can_break(assistant, t)):
                offers[t.id] = {"title": t.title, "asked": today, "answer": ""}
                lines.append(DEFER_QUESTION.format(title=short(t.title), n=t.deferred_count))
        _save(db, offers)
    return "\n".join(lines)


def open_offers(db) -> List[Dict[str, Any]]:
    """Deferral offers asked and not answered: Before today's rows."""
    return [{"task_id": k, **v} for k, v in _offers(db).items() if v.get("asked") and not v.get("answer")]


def answer(assistant, item: str, choice: str) -> Optional[str]:
    """still_want's answer, where it's about breaking down: "break" for a missed item or a deferral offer, or
    "keep" for a deferral offer (it stays as it is, never offered again). None when it's for still_want itself."""
    choice = str(choice or "").strip().lower()
    choice = {"break it down": BREAK, "break it into smaller pieces": BREAK, "break down": BREAK,
              "keep as it is": "keep"}.get(choice, choice)
    db = assistant._db
    if db is None or choice not in (BREAK, "keep"):
        return None
    offers = _offers(db)
    low = (item or "").strip().lower()
    key = next((k for k, v in offers.items() if k == item or v.get("title", "").strip().lower() == low), None)
    from thirties_core.backoff import MissStreaks
    streaks = MissStreaks(db)
    rec = streaks.get(item)
    if choice == "keep":
        if key is None or rec is not None:
            return None  # a missed item's keep: still_want's own
        offers[key]["answer"] = "keep"
        _save(db, offers)
        return f"{offers[key]['title']}: kept as it is."
    task = _task(assistant, item, key or (rec or {}).get("task_id", ""))
    if rec is None and key is None:
        from thirties_core.assistant_tools import ToolError
        raise ToolError(f"'{item}' isn't one Thirties offered to break down.")
    if task is not None and not can_break(assistant, task):
        from thirties_core.assistant_tools import ToolError
        raise ToolError(f"{task.title} can't be broken down here (it has pieces already, or it's a routine, a "
                        "menu option or something done every day).")
    if rec is not None:
        streaks.answer(rec["title"], BREAK)
    if key is not None:
        offers[key]["answer"] = BREAK
        _save(db, offers)
    title = task.title if task is not None else (rec or offers.get(key, {})).get("title", item)
    return (f"{title}: to be broken down. It's held off the day's plan until it has pieces. Now propose 2 to 5 "
            "small, concrete first steps (each one sitting) in one short message; when the user agrees, write them "
            "under it with break_down_task.")


def waiting(assistant) -> List[Any]:
    """Items the user chose to break down that have no pieces yet."""
    db = assistant._db
    if db is None:
        return []
    from thirties_core.backoff import MissStreaks
    out, seen = [], set()
    chosen = [(r.get("title", ""), r.get("task_id", "")) for r in MissStreaks(db)._load().values()
              if r.get("answer") == BREAK]
    chosen += [(v.get("title", ""), k) for k, v in _offers(db).items() if v.get("answer") == BREAK]
    for title, tid in chosen:
        task = _task(assistant, title, tid)
        if task is not None and task.id not in seen and not _has_open_pieces(assistant, task):
            seen.add(task.id)
            out.append(task)
    return out


def release(assistant) -> List[str]:
    """Items broken down since: their run of misses ends (so the first piece is offered), and the offer is closed.
    Returns their titles."""
    db = assistant._db
    if db is None:
        return []
    from thirties_core.backoff import MissStreaks
    streaks = MissStreaks(db)
    out = []
    for r in list(streaks._load().values()):
        if r.get("answer") != BREAK:
            continue
        task = _task(assistant, r.get("title", ""), r.get("task_id", ""))
        if task is not None and _has_open_pieces(assistant, task):
            streaks.reset(r["title"], r.get("task_id", ""), ask_again=False)
            out.append(task.title)
    offers = _offers(db)
    changed = False
    for k, v in offers.items():
        if v.get("answer") == BREAK:
            task = _task(assistant, v.get("title", ""), k)
            if task is not None and _has_open_pieces(assistant, task):
                v["answer"] = "broken"
                changed = True
                if task.title not in out:
                    out.append(task.title)
    if changed:
        _save(db, offers)
    return out


def held(db, task_id: str, title: str = "") -> bool:
    """A deferral offer answered "break" holds the item off the day's options until it's broken down (missed
    items are held by backoff.py)."""
    if db is None:
        return False
    rec = _offers(db).get(task_id)
    return bool(rec and rec.get("answer") == BREAK)


def prompt_section(assistant) -> str:
    """For the model: what the user chose to break down and that still has no pieces."""
    try:
        release(assistant)
        items = waiting(assistant)
    except Exception as e:
        logger.warning("Couldn't list what's to be broken down: %s", e)
        return ""
    if not items:
        return ""
    lines = "\n".join(f"{t.title} ({t.parent_note_title or t.source_notebook}) task_id={t.id}" for t in items)
    return ("TO BREAK DOWN (the user chose to break these into smaller pieces; until they have pieces they're kept off "
            "the day's plan. If you haven't yet, propose 2 to 5 small, concrete first steps, each one sitting, in one "
            "short message; when the user agrees, write them with break_down_task):\n" + lines + "\n\n")


__all__ = ["BREAK", "DEFER_AFTER", "answer", "can_break", "held", "morning_question", "open_offers", "prompt_section",
           "release", "waiting"]
