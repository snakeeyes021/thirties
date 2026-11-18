"""A whole invented day, run through the background service, counting every message Thirties sends unasked.

The person: works a late shift at a pottery studio (11:00 AM - 6:00 PM), paddles a canoe in the morning, glazes
test tiles in the evening. They're up at 7:45, write a few times, mark some things and leave others unmarked, as
people do. The service ticks every five minutes from waking to midnight; each message that goes out is sorted by
its kind (the bell at a block's start, a check-in, a list of what's not marked yet, a reminder, an offer, the
morning and the evening).

    python3 -m tests.message_day           # prints the counts for each message level

Used by tests/test_fewer_messages.py to hold the day to a small number of messages.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import timedelta
from typing import Dict, List, Tuple
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.messages import MISSED_HEADING
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

os.environ.setdefault("THIRTIES_OFFLINE", "1")

# (id, title, start, end, source): routine steps come from the person's routine, the rest are their own plans
ITEMS = (
    ("r1", "Roll out the yoga mat", "08:00", "08:15", "routine"),
    ("r2", "Wind the clock", "08:15", "08:30", "routine"),
    ("r3", "Mist the ferns", "08:30", "08:45", "routine"),
    ("r4", "Soak the sponges", "08:45", "09:00", "routine"),
    ("cn", "Paddle the canoe", "09:00", "10:30", "user"),
    ("sn", "Pack a pear and cheese", "10:30", "11:00", "user"),
    ("sp", "Supper", "18:30", "19:00", "user"),
    ("gl", "Glaze test tiles", "19:00", "20:00", "user"),
    ("oil", "Oil the paddle", "20:00", "20:30", "user"),
    ("kl", "Read about kilns", "20:30", "21:30", "user"),
    ("e1", "Set out tomorrow's apron", "22:00", "22:15", "routine"),
    ("e2", "Write three lines in the logbook", "22:15", "22:30", "routine"),
)

# What the person does: (time, what) where what is ("says", ...) for a message to Thirties, or ("done", ids)
DAY_OF = (
    ("07:45", ("says",)),               # up: the first message builds the day
    ("09:05", ("done", "r1", "r2")),    # on the dock, ticks two steps
    ("09:05", ("says",)),
    ("10:40", ("done", "cn")),
    ("18:35", ("says",)),               # home, chatting while supper cooks
    ("18:50", ("says",)),
    ("19:05", ("says",)),
    ("21:35", ("done", "kl")),
)

Sent = Tuple[str, str, str]  # (clock, kind, text)


def service(level: str = "heavy", block_notices: bool = True):
    set_debug_time(at("07:00"))
    sched = make_scheduler()
    cfg = sched.config
    cfg.assistant.message_level = level
    cfg.telegram.notify_block_transitions = block_notices
    cfg.general.work_start_thirty, cfg.general.work_end_thirty = 22, 35  # 11:00 AM - 6:00 PM
    cfg.general.sleep_end_thirty = 15  # up around 7:30
    for sid, title, start, end, source in ITEMS:
        sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY, source=source))
    joplin = MagicMock()
    joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
    joplin.api_client.ping.return_value = True
    out: List[Sent] = []
    svc = BackgroundService(cfg, lambda title, body, kind: out.append(("", kind, body)), scheduler=sched,
                            joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
    svc.today = lambda: DAY
    svc.run_in_threads = False
    svc.sort_lists_enabled = False
    return svc, out


def _act(svc, now, what) -> None:
    db = svc.scheduler.state_db
    if what[0] == "says":
        mark_up(db, now)
        if not db.get_sync_meta(f"day_built_at:{DAY.isoformat()}"):
            db.set_sync_meta(f"day_built_at:{DAY.isoformat()}", now.isoformat())
            from thirties_core import morning
            morning.mark_done(db, DAY, now)  # the first message's reply was the morning (a turn, not a sim)
    else:
        for sid in what[1:]:
            span = next(s for s in db.get_spans_between(at("00:00"), at("23:59", 1)) if s.id == sid)
            span.status = SpanStatus.DONE
            db.upsert_span(span)
        svc._manager = None


def simulate(level: str = "heavy", block_notices: bool = True, step_minutes: int = 5) -> List[Sent]:
    """The day's unasked output as (clock, kind, text) tuples, oldest first."""
    svc, out = service(level, block_notices)
    plan_moves = sorted(DAY_OF, key=lambda x: x[0])
    t, end = at("07:00"), at("23:55")
    try:
        while t <= end:
            set_debug_time(t)
            for clock, what in plan_moves:
                if at(clock) <= t < at(clock) + timedelta(minutes=step_minutes):
                    _act(svc, t, what)
            before = len(out)
            svc.tick(t)
            for i in range(before, len(out)):
                out[i] = (t.strftime("%H:%M"),) + out[i][1:]
            t += timedelta(minutes=step_minutes)
    finally:
        set_debug_time(None)
    return out


def kind_of(text: str, kind: str) -> str:
    """Which kind of message the user read."""
    first = text.lstrip()[:2]
    if text.startswith("🔔"):
        return "block start"
    if text.startswith("🗓"):
        return "stretch start"
    if text.startswith("🌙 Bedtime"):
        return "bedtime"
    if text.startswith("🌙") or kind == "checkin" and not text.startswith("💼"):
        return "check-in"
    if text.startswith("💼"):
        return "work times"
    if kind == "heads_up":
        return "heads-up"
    if kind == "reminder":
        return "reminder"
    if kind in ("morning", "rollover"):
        return "morning"
    if kind in ("dropbox", "joplin_peer", "notice") or first.startswith("📅"):
        return "offer"
    return kind


def count(sent: List[Sent]) -> Dict[str, int]:
    """Messages by kind, plus how many carried a list of what's not marked yet and how many repeated one sent
    before word for word."""
    c = Counter(kind_of(text, kind) for _, kind, text in sent)
    c["total"] = len(sent)
    c["with an unmarked list"] = sum(MISSED_HEADING in text for _, _, text in sent)
    seen, repeats = set(), 0
    for _, _, text in sent:
        repeats += text in seen
        seen.add(text)
    c["repeated"] = repeats
    return dict(c)


if __name__ == "__main__":
    for level in ("heavy", "medium", "light"):
        msgs = simulate(level)
        print(f"== {level}: {count(msgs)}")
        for clock, kind, text in msgs:
            print(f"  {clock} [{kind}] " + text.replace("\n", " | "))
