"""What the user is told about where the day stands: the status line, what's not marked yet, and the schedule.

One place builds these, so replies, notices, Telegram's /now and /day, and the prompt all say the same thing the
same way (10-24 12:03: the app said "travel starting at 1:45", the phone said "1:30 block"). Python lays the
schedule out; the model only decides when to show it (AGENTS.md §7.3).

- **The status line** says which block the user is in and what's in it ("You're in the 10:00 block: Shower + PT").
- **Missed items** ("Not marked yet") come right after it, one per line, never twice, and never items of the block
  the user is still in (the user's own proposal, 10-29 17:27; C2: 10-26 09:15, 10-29 18:00, 10-30 10:10).
- **The schedule** reads from now on unless the whole day is asked for (10-26 10:16: "Just show me 10 o'clock
  onwards"), with no "(until…)" when the next item follows straight on (10-29 16:24), and things sharing time as
  one entry (10-18 15:00: "Redfox + Thirties").
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, Sequence

from thirties_core.clock import agenda_lines, format_clock, slice_lines, thirty_ceil, thirty_of
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

HALF = timedelta(minutes=30)
MISSED_HEADING = "Not marked yet:"


# --- what's behind ---

def missed_items(plan: DayPlan, now: datetime, skip: Optional[Callable[[EventSpan], bool]] = None) -> List[EventSpan]:
    """Planned items whose time is over with nothing said, in order, once each.

    An item counts only once the block it ends in is over: what's in the block the user is still in isn't missed
    (10-26 09:15: "Why are you asking me about items that go to the block that I'm still in?"). The same title
    twice (a copy left by a move, a routine step planned twice) is listed once (10-29 18:00)."""
    block = thirty_of(now)
    out: List[EventSpan] = []
    seen = set()
    for s in sorted(plan.active_spans(), key=lambda s: (s.start_dt, s.end_dt)):
        if s.category not in (SpanCategory.ACTIVITY, SpanCategory.TASK) or s.status != SpanStatus.PLANNED \
                or s.tentative or thirty_ceil(s.end_dt) > block:
            continue
        if skip is not None and skip(s):
            continue
        key = s.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def missed_lines(spans: Iterable[EventSpan]) -> List[str]:
    """One line per item, no times (they're behind; a time would only say how far). Not one line per block:
    10-30 10:10, "Why are throne, shower, pt, and brush teeth listed as a single item?"."""
    out, seen = [], set()
    for s in spans:
        if s.title.strip().lower() not in seen:
            seen.add(s.title.strip().lower())
            out.append(f"- {s.title}")
    return out


def missed_block(spans: Sequence[EventSpan]) -> str:
    return "\n".join([MISSED_HEADING] + missed_lines(spans)) if spans else ""


class MissedTracker:
    """Which missed items the user has already been shown today (in a notice, a schedule reply, or a question).

    Notices add the list only when something new fell behind since it was last shown (C2: 10-25 "every notice
    appends Not marked yet"; 10-26 to 10-27 "the list grows on every notice all evening"), and a reply doesn't ask
    again about what was shown (C5). Kept in sync_meta, per day, like the background service's other memories."""

    KEY = "missed_shown"

    def __init__(self, state_db) -> None:
        self.db = state_db

    def _load(self, day: str) -> dict:
        try:
            data = json.loads(self.db.get_sync_meta(self.KEY) or "{}") if self.db is not None else {}
        except ValueError:
            data = {}
        return data if data.get("day") == day else {"day": day, "ids": [], "titles": []}

    def shown(self, now: datetime) -> dict:
        return self._load(now.date().isoformat())

    def anything_new(self, now: datetime, spans: Sequence[EventSpan]) -> bool:
        data = self.shown(now)
        return any(s.id not in data["ids"] for s in spans)

    def mark(self, now: datetime, spans: Sequence[EventSpan] = (), titles: Sequence[str] = ()) -> None:
        if self.db is None:
            return
        data = self.shown(now)
        data["ids"] = sorted(set(data["ids"]) | {s.id for s in spans})
        data["titles"] = sorted(set(data["titles"]) | {s.title.strip().lower() for s in spans}
                                | {t.strip().lower() for t in titles})
        self.db.set_sync_meta(self.KEY, json.dumps(data))

    def titles_shown(self, now: datetime) -> set:
        return set(self.shown(now)["titles"])


# --- the status line ---

def _row_items(plan: DayPlan) -> List[EventSpan]:
    return [s for s in plan.spans_in_view() if s.category != SpanCategory.SLEEP
            and s.status not in (SpanStatus.SKIPPED,) and s.end_dt > s.start_dt]


def status_line(plan: DayPlan, now: datetime, next_start: Optional[datetime] = None) -> str:
    """Where the user is: the block, and what's in it. An end time only when what's on now runs past the block
    and isn't followed straight on by the next thing listed (10-29: "(until 8:30)" before the next item is
    superfluous)."""
    block = thirty_of(now)
    here = [s for s in _row_items(plan) if not s.tentative and s.status == SpanStatus.PLANNED
            and s.start_dt < block + HALF and s.end_dt > block]
    on_top = [s for s in here if not s.is_envelope]
    here = on_top or here  # what's placed on Work is what's happening; Work alone when nothing is
    if not here:
        return f"You're in the {format_clock(block)} block, with nothing planned in it."

    def said(s: EventSpan) -> str:
        runs_on = s.end_dt > block + HALF and s.end_dt != next_start
        return f"{s.title} (until {format_clock(s.end_dt)})" if runs_on else s.title
    lines = slice_lines([(max(s.start_dt, block), s.end_dt, said(s)) for s in here])
    return f"You're in the {format_clock(block)} block: " + ", then ".join(lines) + "."


# --- the schedule ---

def _note(s: EventSpan) -> str:
    if s.tentative:
        return "unconfirmed"
    return s.status.value if s.status != SpanStatus.PLANNED else ""


def schedule_rows(plan: DayPlan, start: Optional[datetime] = None) -> List[str]:
    """The plan from ``start`` on (the whole day without one), by blocks: each row a block and what's in it,
    things at the same time joined with +, and an end time only before a gap (clock.agenda_lines)."""
    items = [s for s in _row_items(plan) if start is None or s.start_dt >= start]
    rows = [(s.start_dt, s.end_dt, s.title, _note(s)) for s in sorted(items, key=lambda s: s.start_dt)]
    return agenda_lines(rows)


def render_schedule(plan: DayPlan, now: Optional[datetime], whole_day: bool = False,
                    missed: Optional[Sequence[EventSpan]] = None, label: str = "") -> str:
    """The schedule as the user reads it. For the day being lived (``now`` given): the status line, what's not
    marked yet right after it, then the rest of the day (or, asked for, the whole day). For another day: the
    day."""
    if now is None or not (plan.day_start <= now < plan.day_end):
        rows = schedule_rows(plan)
        head = f"Here's {label or plan.target_date.strftime('%A, %B %-d')}:"
        return "\n".join([head] + rows) if rows else f"Nothing is planned for {label or 'that day'} yet."
    block = thirty_of(now)
    rows = schedule_rows(plan, None if whole_day else block + HALF)
    upcoming = [s for s in _row_items(plan) if s.start_dt >= block + HALF]
    next_start = min((thirty_of(s.start_dt) for s in upcoming), default=None)
    missed = list(missed if missed is not None else missed_items(plan, now))
    return with_missed(status_line(plan, now, next_start), missed) + "\n\n" + (
        ("The whole day:\n" if whole_day else "") + "\n".join(rows) if rows else "Nothing else is planned today.")


def with_missed(status: str, missed: Sequence[EventSpan]) -> str:
    """A status (a reply's "you're in…" line, or a notice) with what's not marked yet right under it."""
    return status + ("\n" + missed_block(missed) if missed else "")


def missed_for(tools, now: datetime) -> List[EventSpan]:
    """missed_items for a PlanTools' day, leaving out items the user said not to ask about (mute_checkins)."""
    mutes = tools.services.mutes if getattr(tools, "services", None) else None
    return missed_items(tools.day_plan, now, skip=(lambda s: mutes.is_muted(s.id, now)) if mutes else None)


def now_section(tools) -> str:
    """For the prompt: where the user is and what's behind, worked out here so the model never counts blocks or
    keeps track of what it already said."""
    plan = tools.day_plan
    now = tools._now()
    if not (plan.day_start <= now < plan.day_end):
        return ""
    missed = missed_for(tools, now)
    lines = ["NOW", status_line(plan, now)]
    if missed:
        shown = MissedTracker(tools._state_db).titles_shown(now)
        new = [s.title for s in missed if s.title.strip().lower() not in shown]
        lines.append("Not marked yet (their time is over, nothing said): " + ", ".join(s.title for s in missed) + ". "
                     + ("The user has already been shown all of these: don't ask about them again."
                        if not new else "Not shown to the user yet: " + ", ".join(new) + "."))
    return "\n".join(lines) + "\n\n"


# --- placing the schedule in a reply ---

_ROW_RE = re.compile(r"^\s*(?:[-•*]|\d+\.)\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\s*(?:–|-|—)")
_STATUS_RE = re.compile(r"^\s*You're in the \d{1,2}:\d{2}")


def place_schedule(reply: str, schedule: str) -> str:
    """A reply that shows the schedule shows Python's layout of it. The model's own rows (it was told to copy the
    text as is, and sometimes rewrites it) are replaced by the canonical text; with no rows at all, it goes at
    the end. Nothing else in the reply changes."""
    if not schedule.strip():
        return reply
    if schedule.strip() in reply:
        return reply
    lines = reply.splitlines()
    first_line = schedule.strip().splitlines()[0]
    marks = [i for i, line in enumerate(lines)
             if _ROW_RE.match(line) or _STATUS_RE.match(line) or line.strip() == MISSED_HEADING
             or line.strip() == first_line.strip()]
    if not marks:
        return (reply.rstrip() + "\n\n" + schedule.strip()).strip()
    start, end = marks[0], marks[0]
    for i in range(marks[0] + 1, len(lines)):  # the run: rows, bullets, indented lines, blank lines between
        line = lines[i]
        if i in marks or line.startswith((" ", "\t")) or line.lstrip().startswith(("-", "•", "*")) \
                or line.strip().lower() in ("the whole day:", "coming up:", "next:"):
            end = i
        elif line.strip():
            break
    return "\n".join(lines[:start] + schedule.strip().splitlines() + lines[end + 1:]).strip()


# --- check-in questions that repeat (C5) ---

_QUESTION_RE = re.compile(r"[^.!?\n]*\?")
_CHECKIN_RE = re.compile(r"\b(did|have|were|was|are)\s+you\b|\bany luck\b|\bhow did\b|\bstill on\b", re.IGNORECASE)


def _mentions(sentence: str, title: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(title.strip()) + r"(?!\w)", sentence, re.IGNORECASE))


def drop_repeat_checkins(reply: str, missed: Sequence[EventSpan], already: set) -> tuple:
    """Questions about missed items the user was already shown or asked about are dropped (C5: 10-30 09:37–10:05,
    "Did you get to Caffeinate, Water bottle, Water kitties, or Breakfast?" after each report). Returns the reply
    and the titles a surviving question asked about (they count as shown from now on)."""
    titles = [s.title for s in missed]
    asked: List[str] = []
    out = reply

    for m in list(_QUESTION_RE.finditer(reply)):
        sentence = m.group(0)
        if not _CHECKIN_RE.search(sentence):
            continue
        named = [t for t in titles if _mentions(sentence, t)]
        if not named:
            continue
        if all(t.strip().lower() in already for t in named):
            out = out.replace(sentence, "", 1)
        else:
            asked += named
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if not out:
        return reply, asked
    return out, asked


__all__ = ["MISSED_HEADING", "MissedTracker", "drop_repeat_checkins", "missed_block", "missed_for", "missed_items",
           "missed_lines", "now_section", "with_missed",
           "place_schedule", "render_schedule", "schedule_rows", "status_line"]
