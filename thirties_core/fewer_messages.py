"""How much Thirties says by itself in a day: the rules every unasked message shares.

The background service (background.py) sends notices and questions nobody asked for. Each is worth a buzz only
when it tells the user something new, so:

- an item's status is asked about once a day, whichever duty asks (a check-in, a stretch's question, the evening
  catch-up, the before-bed question): ``AskedOnce`` is the one record they all read and write;
- a notice at the start of a block goes out once for a run of back-to-back items, never again for each block
  inside it (the user is already in it), and never for a run made only of routine steps (the user knows them);
- the telegram.notify_block_transitions switch turns those start notices off, and nothing else;
- the same words never go out twice in a day (``Repeats``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List

# What a repeat is checked for: the notices and questions Thirties sends by itself. Reminders the user set (one may
# say the same words at two times), answers to the user's own messages and the end of a hold are never held back.
UNASKED_KINDS = ("start", "checkin", "heads_up", "contextual", "dropbox", "notice", "joplin_peer")
ASKED_KEY = "checkins_asked"  # per item id: the day it was asked about (the key check_ins has always used)
SENT_KEY = "unasked_sent"


def block_notices_on(config) -> bool:
    """Whether start-of-block notices go out at all (telegram.notify_block_transitions, on by default)."""
    return bool(getattr(getattr(config, "telegram", None), "notify_block_transitions", True))


def is_routine_step(span) -> bool:
    """A step of a routine, as Thirties placed it: the user does these every day and needs no buzz for them."""
    return getattr(span, "source", "") == "routine"


def runs_of(spans) -> List[list]:
    """The items cut into runs of back-to-back items (one starting by the time the one before ends), in order."""
    runs: List[list] = []
    end = None
    for s in sorted(spans, key=lambda s: (s.start_dt, s.end_dt)):
        if runs and s.start_dt <= end:
            runs[-1].append(s)
            end = max(end, s.end_dt)
        else:
            runs.append([s])
            end = s.end_dt
    return runs


class AskedOnce:
    """Which items were already asked about today, by any duty: one question per item, never a second."""

    def __init__(self, db) -> None:
        self.db = db

    def _load(self) -> Dict[str, str]:
        try:
            return json.loads(self.db.get_sync_meta(ASKED_KEY) or "{}")
        except ValueError:
            return {}

    def asked(self, span_id: str) -> bool:
        return span_id in self._load()

    def mark(self, ids: Iterable[str], now: datetime) -> None:
        done = self._load()
        done.update({i: now.date().isoformat() for i in ids})
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        self.db.set_sync_meta(ASKED_KEY, json.dumps({k: v for k, v in done.items() if v >= cutoff}))


class Repeats:
    """Words already sent by Thirties today: the same message twice in a day is noise, whatever caused it."""

    def __init__(self, db) -> None:
        self.db = db

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]

    def _load(self) -> Dict[str, str]:
        try:
            return json.loads(self.db.get_sync_meta(SENT_KEY) or "{}")
        except ValueError:
            return {}

    def seen(self, text: str, day: date) -> bool:
        return self._load().get(self._key(text)) == day.isoformat()

    def mark(self, text: str, day: date) -> None:
        sent = {k: v for k, v in self._load().items() if v >= (day - timedelta(days=1)).isoformat()}
        sent[self._key(text)] = day.isoformat()
        self.db.set_sync_meta(SENT_KEY, json.dumps(sent))


__all__ = ["AskedOnce", "Repeats", "UNASKED_KINDS", "block_notices_on", "is_routine_step", "runs_of"]
