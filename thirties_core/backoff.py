"""Backing off from what keeps being missed.

An item Thirties put on a day and that was skipped, or left unmarked at the day's end, is a miss. Unmarked is
unknown, not "not done": a miss only says the day didn't hold it, so offering it again the next morning, every
morning, asks the same unanswered question day after day. Instead Thirties backs off:

- Each item keeps a count of its consecutive misses (on days it was planned; days it wasn't planned don't break
  the run). Done ends the run, and so does the user placing or pinning it themselves, or a change to the item.
- After the k-th consecutive miss, Thirties doesn't offer or place it again until 2^(k-1) whole days have passed:
  1, 2, then 4 days, so nothing missed comes back the next morning. A due date overrides it: as the date nears
  (2 days or less), or when a session of the work planned back from its deadline falls today, it's offered as
  usual. So does what the user said can't wait (an urgency of "now" they set, or "as soon as possible" in the
  item's own words; never a guessed urgency), but only until the third miss: from then on it rests like the rest,
  so it can't come back day after day.
- Further misses in a row keep doubling the rest (8, then 16 days...), with no further question.
- After the third consecutive miss it also gets one question, in the morning's Before today, with the other
  such items: "Still want this? Keep for this week / Park for later / Drop". Keep starts the count again, Park
  moves it to a month-level horizon, Drop stops it rolling over (its list in Joplin is untouched). Asked once per
  item: never again unless the item changes or the user brings it up.

The count lives in the state database (sync meta), per item, beside the open pool (carry.py).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

KEY = "miss_streaks"   # {key: {"title", "task_id", "misses", "last_miss", "counted", "sig", "asked", "answer"}}
ASK_AFTER = 3          # consecutive misses before the one question
DUE_NEAR_DAYS = 2      # a due date this close overrides the back-off
CHOICES = ("keep", "park", "drop", "break")  # break: held until it's broken into pieces (break_down.py)
QUESTION = "Still want {title}? Keep for this week / Park for later / Drop"   # one line per item


def signature(task) -> str:
    """What makes an item the same item: a change to any of it starts its count again."""
    if task is None:
        return ""
    return "|".join(str(getattr(task, k, "") or "") for k in ("title", "description", "due", "parent_note_id",
                                                              "section"))


def _changed(rec: Dict[str, Any], sig: str, task_id: str) -> bool:
    """The same item (the same id), changed since its misses were counted. A copy under another id (the Today
    note's line for it) is the same thing, not a change."""
    return bool(sig and rec.get("sig") and sig != rec["sig"] and task_id and task_id == rec.get("task_id"))


def rest_days(misses: int) -> int:
    """Whole days it rests after the k-th consecutive miss: 1, 2, 4, 8..."""
    return 2 ** (misses - 1) if misses > 0 else 0


def _as_date(text: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


class MissStreaks:

    def __init__(self, db) -> None:
        self.db = db

    def _load(self) -> Dict[str, Dict[str, Any]]:
        raw = self.db.get_sync_meta(KEY) if self.db is not None else None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        if self.db is not None:
            self.db.set_sync_meta(KEY, json.dumps(data, sort_keys=True))

    @staticmethod
    def _find(data: Dict[str, Dict[str, Any]], title: str = "", task_id: str = "") -> Optional[str]:
        low = (title or "").strip().lower()
        if task_id:
            for k, rec in data.items():
                if rec.get("task_id") == task_id:
                    return k
        return low if low and low in data else None

    def get(self, title: str = "", task_id: str = "") -> Optional[Dict[str, Any]]:
        data = self._load()
        key = self._find(data, title, task_id)
        return data.get(key) if key else None

    # --- what happened on a day ---

    def record_day(self, day: date, missed: Iterable[Dict[str, str]], done: Iterable[Dict[str, str]] = ()) -> None:
        """The outcomes of ``day``, once: each missed item's count goes up by one, each done item's run ends.
        Recording the same day again changes nothing."""
        data = self._load()
        stamp = day.isoformat()
        for item in done:
            key = self._find(data, item.get("title", ""), item.get("task_id", ""))
            if key:
                data[key].update(misses=0, last_miss="", answer="")
        for item in missed:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            key = self._find(data, title, item.get("task_id", "")) or title.lower()
            rec = data.setdefault(key, {"title": title, "task_id": item.get("task_id") or "", "misses": 0,
                                        "last_miss": "", "counted": "", "sig": "", "asked": "", "answer": ""})
            if rec.get("counted", "") >= stamp:
                continue  # this day is counted already
            sig = item.get("sig") or ""
            if _changed(rec, sig, item.get("task_id") or ""):
                rec.update(misses=0, asked="", answer="")  # a changed item starts again
            rec.update(title=title, task_id=item.get("task_id") or rec.get("task_id") or "",
                       misses=int(rec.get("misses") or 0) + 1, last_miss=stamp, counted=stamp, sig=sig or rec.get("sig", ""))
        self._save(data)

    def reset(self, title: str = "", task_id: str = "", ask_again: bool = True) -> bool:
        """The run ends: done, or the user placed, pinned or brought it up. With ``ask_again`` (the user brought it
        up, or it changed) a later run may ask once more."""
        data = self._load()
        key = self._find(data, title, task_id)
        if not key or not (data[key].get("misses") or data[key].get("answer") or (ask_again and data[key].get("asked"))):
            return False
        data[key].update(misses=0, last_miss="", answer="", **({"asked": ""} if ask_again else {}))
        self._save(data)
        return True

    # --- whether Thirties may offer or place it ---

    def held_until(self, day: date, title: str = "", task_id: str = "", sig: str = "", due: str = "",
                   session: bool = False, urgent: bool = False) -> Optional[date]:
        """None when Thirties may offer or place the item on ``day``; else the first day it may (date.max for a
        drop, or a park with no due date near). A changed item is never held (its count starts again).
        ``session``: a session of the work planned back from its deadline falls on ``day``. ``urgent``: the user
        said it can't wait (stated_cant_wait); it counts only before the third miss."""
        data = self._load()
        key = self._find(data, title, task_id)
        if not key:
            return None
        rec = data[key]
        if _changed(rec, sig, task_id):
            rec.update(misses=0, asked="", answer="", sig=sig)
            self._save(data)
            return None
        due_day = _as_date(due)
        misses, last = int(rec.get("misses") or 0), _as_date(rec.get("last_miss"))
        near = session or (due_day is not None and (due_day - day).days <= DUE_NEAR_DAYS) \
            or (urgent and misses < ASK_AFTER)
        if rec.get("answer") == "drop":
            return date.max  # the user said stop rolling it over
        if near:
            return None
        if rec.get("answer") == "park":
            return date.max  # waits at its month-level horizon
        if rec.get("answer") == "break":
            return date.max  # waits to be broken into pieces; then its run ends (break_down.release)
        if misses <= 0 or last is None:
            return None
        free = last + timedelta(days=rest_days(misses) + 1)  # whole days in between, then it may come
        return free if day < free else None

    def held(self, day: date, title: str = "", task_id: str = "", sig: str = "", due: str = "",
             session: bool = False, urgent: bool = False) -> bool:
        return self.held_until(day, title, task_id, sig, due, session, urgent) is not None

    # --- the one question ---

    def to_ask(self) -> List[Dict[str, Any]]:
        """Items missed three or more times running, never asked (or asked before a change), with no answer."""
        return sorted((r for r in self._load().values() if int(r.get("misses") or 0) >= ASK_AFTER
                       and not r.get("asked") and not r.get("answer")), key=lambda r: r["title"].lower())

    def asked_open(self) -> List[Dict[str, Any]]:
        """Asked and not answered yet: Before today's buttons."""
        return sorted((r for r in self._load().values() if r.get("asked") and not r.get("answer")
                       and int(r.get("misses") or 0) >= ASK_AFTER), key=lambda r: r["title"].lower())

    def mark_asked(self, titles: Iterable[str], day: date) -> None:
        data = self._load()
        for t in titles:
            key = self._find(data, t)
            if key:
                data[key]["asked"] = day.isoformat()
        self._save(data)

    def answer(self, title: str, choice: str) -> Optional[Dict[str, Any]]:
        """keep: the count starts again (it's an option this week); park or drop: held, as said. Never asked
        again."""
        data = self._load()
        key = self._find(data, title)
        if key is None:
            low = (title or "").strip().lower()
            key = next((k for k in data if low and low in k), None)
        if key is None:
            return None
        rec = data[key]
        rec.update(answer="" if choice == "keep" else choice, asked=rec.get("asked") or "answered")
        if choice == "keep":
            rec.update(misses=0, last_miss="")
        self._save(data)
        return rec


def stated_cant_wait(db, task, title: str) -> bool:
    """The user said it can't wait: an urgency of "now" they set themselves (never a guess, nor a list's default),
    or "as soon as possible" in the item's own words."""
    from thirties_core.carry import ASAP_RE
    if ASAP_RE.search(title or ""):
        return True
    if task is None or db is None:
        return False
    from thirties_core.lists import ListCatalog
    from thirties_core.today import TodayLinks
    ids = {task.id}
    home = TodayLinks(db).home_of(task.title)  # a line on the Today note stands for its home item
    if home and home.get("home_id"):
        ids.add(home["home_id"])
    urgencies = ListCatalog(db).urgencies()
    return any((urgencies.get(i) or {}).get("urgency") == "now" and (urgencies.get(i) or {}).get("source") == "user"
               for i in ids)


def question_lines(items: List[Dict[str, Any]], short=lambda t: t) -> str:
    return "\n".join(QUESTION.format(title=short(r["title"])) for r in items)


def placing_refs(name: str, args: Dict[str, Any]) -> List[str]:
    """The items a tool call places or pins, by the words or id it names them with."""
    if name == "leftover_action" and str(args.get("action") or "").lower() not in ("keep", "accept", "undrop"):
        return []
    if name == "propose_day":
        items = args.get("items")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except ValueError:
                items = []
        return [str(i.get("item") or i.get("task_id") or i.get("task") or i.get("title") or "")
                for i in (items or []) if isinstance(i, dict)]
    return [str(args[k]) for k in ("task_id", "task", "item", "title", "event") if args.get(k)]


PLACING_TOOLS = {"add_event", "update_event", "place_leftover", "pin_to_period", "set_horizon", "leftover_action",
                 "propose_day"}


__all__ = ["MissStreaks", "ASK_AFTER", "CHOICES", "PLACING_TOOLS", "QUESTION", "placing_refs", "question_lines",
           "rest_days", "signature", "stated_cant_wait"]
