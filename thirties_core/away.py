"""Quiet while the user is away: what the background says when nobody is answering.

- **The Not marked yet list goes out again only when it changed.** A bell or a check-in carries the list only if it
  differs from the list as it was last sent (in a notice, a reply or the schedule). The same unchanged list under
  every hourly bell was noise.
- **Two check-ins in a row with no reply, then none.** After ``MAX_UNANSWERED`` check-ins with no message from the
  user in between, no more check-ins go out until the user writes again. Start notices still go out, as their own
  line only (no what's next, no list): enough to see the day went on, short enough not to pile up.

Kept in sync_meta, like the background service's other memories.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Sequence

LAST_SENT_KEY = "not_marked_sent"
UNANSWERED_KEY = "checkins_unanswered"
LAST_MESSAGE_KEY = "last_user_message_at"  # set by ConversationManager at the start of each user turn
MAX_UNANSWERED = 2


def _load(db, key: str) -> dict:
    try:
        data = json.loads(db.get_sync_meta(key) or "{}") if db is not None else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


class LastSentList:
    """The Not marked yet list as it was last sent today: its items' ids."""

    def __init__(self, db) -> None:
        self.db = db

    def same(self, now: datetime, spans: Sequence) -> bool:
        """The list is exactly what was last sent today (an empty list never counts as sent)."""
        data = _load(self.db, LAST_SENT_KEY)
        return bool(spans) and data.get("day") == now.date().isoformat() \
            and data.get("ids") == sorted({s.id for s in spans})

    def mark(self, now: datetime, spans: Sequence) -> None:
        if self.db is None or not spans:
            return
        self.db.set_sync_meta(LAST_SENT_KEY, json.dumps({"day": now.date().isoformat(),
                                                         "ids": sorted({s.id for s in spans})}))


class UnansweredCheckins:
    """How many check-ins went out since the user last wrote."""

    def __init__(self, db) -> None:
        self.db = db

    def _since(self) -> str:
        return (self.db.get_sync_meta(LAST_MESSAGE_KEY) or "") if self.db is not None else ""

    def count(self) -> int:
        data = _load(self.db, UNANSWERED_KEY)
        return int(data.get("count") or 0) if data.get("since", "") == self._since() else 0

    def paused(self) -> bool:
        """No more check-ins: the last MAX_UNANSWERED went unanswered."""
        return self.count() >= MAX_UNANSWERED

    def sent(self) -> None:
        if self.db is None:
            return
        self.db.set_sync_meta(UNANSWERED_KEY, json.dumps({"since": self._since(), "count": self.count() + 1}))


__all__ = ["LastSentList", "UnansweredCheckins", "MAX_UNANSWERED"]
