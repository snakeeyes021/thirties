"""How long an unfinished item rides along from day to day: the rule that ends the pile-up.

11-04, 11-05: every open line on the Today note was offered again every morning, so items from a week before
piled up with yesterday's ("the cascading events from prior days ... is a massive headache"), and the same pile
sat in "on the list, no time yet" all day.

The rule, per item (by its title):
- The first morning it's still open, it carries over by itself ("From yesterday, still open").
- The next morning it's still open, it's asked about once: "Still open since <day>: keep or drop?"
- Kept: it starts again from that day. Not kept (dropped, or no answer by the next morning): it leaves today's
  list. Its own list still has it; an item with no other list goes to the drop box, or stays put if there's none.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional

KEY = "carry_ledger"

CARRY, ASK, DROP = "carry", "ask", "drop"


class CarryLedger:
    """Per open item: the day it was first carried (``since``) and the day it was asked about (``asked``)."""

    def __init__(self, db) -> None:
        self.db = db

    def _load(self) -> Dict[str, Dict[str, str]]:
        raw = self.db.get_sync_meta(KEY) if self.db is not None else None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Dict[str, str]]) -> None:
        if self.db is not None:
            self.db.set_sync_meta(KEY, json.dumps(data, sort_keys=True))

    def sort(self, titles: Iterable[str], today: date,
             first_seen: Optional[Callable[[str], Optional[date]]] = None) -> Dict[str, List[str]]:
        """Which of the open ``titles`` carry over quietly, which are asked about, and which go. Recorded, so
        the same morning gives the same answer, and anything no longer open is forgotten."""
        data = self._load()
        out: Dict[str, List[str]] = {CARRY: [], ASK: [], DROP: []}
        seen: Dict[str, Dict[str, str]] = {}
        for title in titles:
            key = title.strip().lower()
            if not key or key in seen:
                continue
            rec = dict(data.get(key) or {})
            if not rec.get("since"):
                start = (first_seen(title) if first_seen else None) or today - timedelta(days=1)
                rec["since"] = min(start, today - timedelta(days=1)).isoformat()
            asked = rec.get("asked") or ""
            age = (today - date.fromisoformat(rec["since"])).days
            if asked and asked < today.isoformat():
                out[DROP].append(title)
            elif asked or age >= 2:
                rec["asked"] = today.isoformat()
                out[ASK].append(title)
            else:
                out[CARRY].append(title)
            seen[key] = rec
        self._save(seen)
        return out

    def since(self, title: str) -> Optional[date]:
        rec = self._load().get(title.strip().lower()) or {}
        try:
            return date.fromisoformat(rec["since"]) if rec.get("since") else None
        except ValueError:
            return None

    def kept(self, titles: Iterable[str], today: date) -> None:
        """The user kept these: each starts again from today (carried tomorrow, asked about the day after)."""
        data = self._load()
        for title in titles:
            data[title.strip().lower()] = {"since": today.isoformat(), "asked": ""}
        self._save(data)

    def forget(self, titles: Iterable[str]) -> None:
        data = self._load()
        for title in titles:
            data.pop(title.strip().lower(), None)
        self._save(data)


def link_day(links, title: str) -> Optional[date]:
    """When an item was put on today's list, from its link to its home item (UTC date is close enough)."""
    rec = links.home_of(title) if links is not None else None
    try:
        return datetime.fromisoformat(rec["updated_at"]).date() if rec and rec.get("updated_at") else None
    except ValueError:
        return None


def weekday(day: Optional[date]) -> str:
    return day.strftime("%A") if day else "before"
