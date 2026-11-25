"""Where things stand: the one standard view, rendered by Python from the plan as it now stands.

Wherever the day is laid out for the user, it's this view, always in the same shape:

  Now: what's in the block under way
  The rest of today: every row from the next block to the end of the day, with the Free stretches
  Not marked yet: one item per line, by its title, never a time
  If there's time: what the user put under that heading on today's list, one title per line, never a time

(each only when there's something in it; also an item a change moved out of those rows, and someone else's events
in that stretch). Never a section of today's list lines with no time ("On today's list, no time yet"): the user
doesn't want one. Nothing from before now, unless the user asked
for earlier today (``standing``'s ``whole_day``). The model never writes it and is never asked to: it goes under the
reply of a turn that changed something (an add, a done or skip, a move, a drop), and under the reply of a turn that
called show_schedule, once (conversation.py, one_schedule.place). Whether the user asked to see the plan is never
read off their words here: their message only says what they want changed, and the model decides what to show.

The same view reaches the user wherever the schedule otherwise wouldn't: under a start notice or a check-in (when
it says something new: ``SentSummaries``), as Telegram's /now and its "What's next" button, after a change made by a
tick in the app's day panel, after a change that couldn't be made, and after a reply that talked the plan over
(``follows_turn``).

The setting (shared, every device): "nothing", "rest" (the rest of today, every row) or "few" (only the next few
rows, for someone who asks for less)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from thirties_core.clock import format_clock, slice_lines, thirty_ceil, thirty_of
from thirties_core.messages import missed_items
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

MODES = ("nothing", "rest", "few")
DEFAULT_MODE = "rest"
NEXT_ROWS = 3  # "few": the next few rows after the current block
FREE_MIN = timedelta(hours=1)  # an open stretch this long or longer gets its own "Free" line
HALF = timedelta(minutes=30)
IF_TIME_HEADING = "If there's time:"
EARLIER_HEADING = "Earlier today:"
# What Python puts under a reply, said to the model as a fact (show_schedule's result, the turn's NOW section)
PLACED = ("Python puts the standard view under your reply by itself, exactly once: Now, The rest of today, and "
          "Not marked yet (titles only, never times), then If there's time when it isn't empty. Never copy, rewrite or summarise it; write only your own "
          "sentences.")

Snapshot = Dict[str, Tuple[float, float, str]]


def mode_of(value) -> str:
    """A stored or given setting as one of MODES. "next" is what earlier versions kept for their default, which
    showed only the next few rows: it now means the default, the rest of today (a short form the user asks for is
    kept as "few"). Anything unknown: the default."""
    value = str(value or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def asked_mode(config, db, day) -> str:
    """The form a view the user asks for outright takes (/now, the "What's next" button): the rest of today, or
    the next few rows when that's what they chose; whole even when replies leave it out."""
    from thirties_core.reply_extras import effective
    return "few" if mode_of(effective(config, db, day)["whats_next"]) == "few" else "rest"


def snapshot(plan: DayPlan) -> Snapshot:
    """Each item's times and status, to see what a turn moved."""
    return {s.id: (s.start_dt.timestamp(), s.end_dt.timestamp(), s.status.value) for s in plan.spans}


def moved_since(before: Snapshot, plan: DayPlan) -> List[EventSpan]:
    """The items that were on the plan before and now have other times (still planned)."""
    out = []
    for s in plan.spans:
        old = before.get(s.id)
        if old is not None and (old[0], old[1]) != (s.start_dt.timestamp(), s.end_dt.timestamp()) \
                and s.status == SpanStatus.PLANNED and not s.is_envelope:
            out.append(s)
    return out


def _mine(plan: DayPlan) -> List[EventSpan]:
    """The user's own things still to come, as the schedule lists them (_row_items), planned only."""
    from thirties_core.messages import _row_items
    return [s for s in _row_items(plan) if s.status == SpanStatus.PLANNED and not s.tentative]


def _moved_line(moved: List[EventSpan], plan: DayPlan, said: str) -> str:
    """"Also moved: Shower, then Journal, now 7:30 PM": by the block each now starts in, joined as the rows are,
    unless the user gave that exact time themselves (then it's theirs, to the minute)."""
    from thirties_core.facts import gave_time
    groups: Dict[str, List[EventSpan]] = {}
    for s in sorted(moved, key=lambda s: s.start_dt.timestamp()):
        exact = s.start_dt != thirty_of(s.start_dt) and gave_time(said, s.start_dt)
        when = format_clock(s.start_dt if exact else thirty_of(s.start_dt))
        if s.start_dt >= plan.day_end:
            when += f" on {s.start_dt.strftime('%A')}"
        groups.setdefault(when, []).append(s)
    return "Also moved: " + "; ".join(
        ", then ".join(slice_lines([(s.start_dt, s.end_dt, s.title) for s in items])) + f", now {when}"
        for when, items in groups.items())


def render(plan: DayPlan, now: datetime, moved: Iterable[EventSpan] = (), mode: Optional[str] = None,
           said: str = "", unmarked: Optional[List[EventSpan]] = None, config=None, db=None,
           untimed: Sequence[Tuple[str, Sequence[str]]] = (), with_now: bool = True, free: bool = True) -> str:
    """The slice, its sections a blank line apart (messages.join_sections): "Now:" and what's in the current block
    (none when nothing is), every row from there to the end of today (or only the next few, with the mode "few")
    laid out by the schedule's own row formatter,
    any moved item not among them, "Not marked yet:" (none when nothing is), and someone else's events in that
    stretch on a "Not yours" line. "" outside the day, or with the mode "nothing". ``said``: the user's message, so a time they gave
    keeps its minutes. ``unmarked``: what's past its time with nothing said (messages.missed_for, which leaves out
    what the user asked not to be asked about); worked out from the plan when not given. ``untimed``: today's list
    items under "If there's time", as (heading, titles) sections (untimed_for), after Not marked yet. ``with_now``: False under a start notice, which
    says what's on now itself. ``free``: a "Free" line for each open stretch of an hour or more among the rows.

    ``mode`` given: that, whole (asked for outright). Left out: as the person wants it (reply_extras.py, from
    ``config`` and ``db``): nothing at all, or only the next few rows, when they said so. Its Not marked yet list
    is there whenever anything is unmarked, and its "Now" line whenever something is on: the view keeps one
    shape."""
    from thirties_core.messages import plain_rows, schedule_rows
    if mode is None:
        from thirties_core.reply_extras import effective
        wanted = effective(config, db, plan.target_date)
        mode = mode_of(wanted["whats_next"])
    mode = mode_of(mode)
    if mode == "nothing" or not (plan.day_start <= now < plan.day_end):
        return ""
    block = thirty_of(now)
    mine = _mine(plan)
    here = [s for s in mine if s.start_dt < block + HALF and s.end_dt > block]
    on_top = [s for s in here if not s.is_envelope]
    here = on_top or here  # what's placed on Work is what's happening; Work alone when nothing is
    now_line = "Now: " + ", then ".join(slice_lines([(max(s.start_dt, block), s.end_dt, s.title) for s in here])) \
        if here else ""
    upcoming = sorted((s for s in mine if s.start_dt >= block + HALF), key=lambda s: s.start_dt.timestamp())
    rows = plain_rows(schedule_rows(plan, block + HALF, planned_only=True))  # the whole rest: "(until" as shown
    shown_rows = rows[:NEXT_ROWS] if mode == "few" else rows  # every row to the end of the day, none cut
    listed = _with_free(plan, now, rows, len(shown_rows)) if free else shown_rows
    next_block = "\n".join(["Next:" if mode == "few" else "The rest of today:"] + listed) if shown_rows \
        else "\n".join(["Nothing else is planned today."] + listed)
    shown_until = block + HALF
    if shown_rows:
        cut = _items_in_rows(upcoming, len(shown_rows)) if mode == "few" else upcoming
        shown_until = max((s.end_dt for s in cut), default=block + HALF)
    shown_ids = {s.id for s in here} | {s.id for s in upcoming if s.start_dt < shown_until}
    elsewhere = [s for s in moved if s.id not in shown_ids]
    theirs = [s for s in plan.spans_in_view() if s.shown_only and s.end_dt > block and s.start_dt < shown_until]
    behind = unmarked_lines(missed_items(plan, now) if unmarked is None else unmarked)  # today's only
    from thirties_core.messages import join_sections
    return join_sections(  # Now, the rest of today, Also moved, Not marked yet, If there's time, Not yours
        now_line if with_now else "", next_block, _moved_line(elsewhere, plan, said) if elsewhere else "",
        "\n".join(behind), *(untimed_lines(heading, titles) for heading, titles in untimed),
        "Not yours: " + "; ".join(f"{s.title}, {format_clock(s.start_dt)}"
                                  for s in sorted(theirs, key=lambda s: s.start_dt.timestamp())) if theirs else "")


def unmarked_lines(spans: List[EventSpan]) -> List[str]:
    """"Not marked yet:" and one plain line per item, its title only (never a time: the same list the schedule and
    a check-in show, messages.missed_block), oldest first: every one of them, however many (a count in place of
    names leaves the user to work out which ones). Nothing at all when there's nothing."""
    from thirties_core.messages import missed_block
    items = sorted(spans, key=lambda s: (s.start_dt.timestamp(), s.end_dt.timestamp()))
    block = missed_block(items)
    return block.splitlines() if block else []


def untimed_lines(heading: str, titles: Sequence[str]) -> str:
    """A heading and one plain line per item, every one of them."""
    titles = [t for t in titles if str(t).strip()]
    if not titles:
        return ""
    return "\n".join([heading] + list(titles))


def bedtime(plan: DayPlan) -> datetime:
    """When the day's waking time ends: the night's sleep, else the end of the canvas."""
    return min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start
                and s.status != SpanStatus.SKIPPED), default=plan.day_end)


def free_stretches(plan: DayPlan, now: datetime, until: Optional[datetime] = None) -> List[Tuple[datetime, datetime]]:
    """Open stretches from now to bedtime (or ``until``) with nothing of the user's planned in them, an hour or
    more each, in whole thirties: exactly where the rows show nothing (done or skipped items free their time,
    someone else's events don't take it)."""
    end = min(until or bedtime(plan), bedtime(plan))
    start = thirty_of(now)
    busy = sorted(((s.start_dt, s.end_dt) for s in _mine(plan) if s.end_dt > start and s.start_dt < end),
                  key=lambda b: b[0])
    out, cursor = [], start
    for a, b in busy + [(end, end)]:
        if a > cursor:
            lo, hi = thirty_ceil(cursor), thirty_of(min(a, end))
            if hi - lo >= FREE_MIN:
                out.append((lo, hi))
        cursor = max(cursor, b)
        if cursor >= end:
            break
    return out


def free_line(a: datetime, b: datetime, now: Optional[datetime] = None) -> str:
    """"Free 12:30 PM – 3:00 PM", or "Free until 3:00 PM" when it has already begun: every time with AM/PM."""
    if now is not None and a <= now:
        return f"Free until {format_clock(b)}"
    return f"Free {format_clock(a)} – {format_clock(b)}"


_ROW_TIME_RE = re.compile(r"^(\d{1,2}:\d{2} [AP]M) –")


def _with_free(plan: DayPlan, now: datetime, rows: List[str], shown: int) -> List[str]:
    """The first ``shown`` rows with a "Free" line in each open stretch of an hour or more among them (and after
    them, when they're the last rows of the day)."""
    from thirties_core.clock import resolve_clock
    starts = []
    for row in rows:
        m = _ROW_TIME_RE.match(row)
        starts.append(resolve_clock(m.group(1), plan.day_start) if m else None)
    cutoff = starts[shown] if shown < len(rows) and starts[shown] is not None else None
    windows = [w for w in free_stretches(plan, now) if cutoff is None or w[0] < cutoff]
    out: List[Tuple[datetime, str]] = []
    last = thirty_of(now)
    for row, at in zip(rows[:shown], starts[:shown]):
        at = at or last
        out.append((at, row))
        last = at
    out += [(a, free_line(a, b, now)) for a, b in windows]
    return [line for _, line in sorted(out, key=lambda x: x[0])]  # stable: a row before a stretch at its time


def _items_in_rows(upcoming: List[EventSpan], n_rows: int) -> List[EventSpan]:
    """The items the first ``n_rows`` rows of agenda_lines cover (rows are thirties: count distinct start thirties,
    an item that runs on keeps its row)."""
    starts = sorted({thirty_of(s.start_dt) for s in upcoming})
    last = starts[min(n_rows, len(starts)) - 1] if starts else None
    return [s for s in upcoming if last is not None and thirty_of(s.start_dt) <= last]


def summary(tools, now: datetime, mode: Optional[str] = None, moved: Iterable[EventSpan] = (), said: str = "",
            unmarked: Optional[List[EventSpan]] = None, with_now: bool = True) -> str:
    """What's next for a PlanTools' day as it stands, with today's list items that have no time: what a reply, a
    notice, /now and the "What's next" button all show. ``mode`` left out: as the person wants it (render with
    the settings); given: asked for outright, whole (/now, the button). ``unmarked`` defaults to everything past
    its time with nothing said, muted or not (a mute quiets check-ins, never where things stand)."""
    from thirties_core.messages import missed_for
    if unmarked is None:
        unmarked = missed_for(tools, now, muted_too=True)
    scheduler = getattr(tools, "scheduler", None)
    from thirties_core.item_path import labelled, labelled_plan, tasks_of  # a nested item with its parents
    tasks = tasks_of(tools)
    plan, moved, unmarked = labelled_plan(tools.day_plan, tasks), labelled(list(moved), tasks), \
        labelled(unmarked, tasks)
    return render(plan, now, moved, mode, said, unmarked, config=getattr(scheduler, "config", None),
                  db=getattr(tools, "_state_db", None), untimed=untimed_for(tools), with_now=with_now)



def untimed_for(tools) -> List[Tuple[str, List[str]]]:
    """Today's If there's time section, as its clean list (if_time.open_items): only what the user asked to keep
    without a time. Nothing for another day, or without the task backend."""
    assistant = getattr(tools, "assistant", None)
    if assistant is None or not tools.is_current(tools.day_plan):
        return []
    from thirties_core import if_time
    listed = if_time.open_items(assistant)
    return [(IF_TIME_HEADING, listed)] if listed else []

def earlier_lines(plan: DayPlan, now: datetime) -> List[str]:
    """What today held before the block under way, as a record ("Earlier today:" and the whole day's rows before
    it, done items too: messages.whole_day_lines), for the user who asked for earlier today. [] when nothing was."""
    from thirties_core.messages import whole_day_lines
    block = thirty_of(now)
    rows = [row for row in whole_day_lines(plan, now) if not row.startswith("Now:")
            and (_row_start(row, plan) or block) < block]
    return [EARLIER_HEADING] + rows if rows else []


def _row_start(row: str, plan: DayPlan) -> Optional[datetime]:
    from thirties_core.clock import resolve_clock
    m = _ROW_TIME_RE.match(row)
    return resolve_clock(m.group(1), plan.day_start) if m else None


def standing(tools, now: datetime, whole_day: bool = False) -> str:
    """The standard view, asked for through show_schedule, with every row left today, whatever replies are set
    to leave out (asked_mode), every unmarked item, muted or not; with ``whole_day`` (the user asked for earlier
    today too), what today held before now above it. "" outside the day being lived."""
    plan = tools.day_plan
    if not (tools.is_current(plan) and plan.day_start <= now < plan.day_end):
        return ""
    scheduler = getattr(tools, "scheduler", None)
    mode = asked_mode(getattr(scheduler, "config", None), getattr(tools, "_state_db", None), plan.target_date)
    view = summary(tools, now, mode)
    if not whole_day:
        return view
    from thirties_core.item_path import labelled_plan, tasks_of
    from thirties_core.messages import join_sections
    return join_sections("\n".join(earlier_lines(labelled_plan(plan, tasks_of(tools)), now)), view)


# --- sent where: so a notice doesn't repeat the slice every half hour ---

RESEND_AFTER = timedelta(hours=2)


def plan_signature(plan: DayPlan) -> str:
    """What the slice is made of: every item of the user's today, its times and its status. Any change to the
    plan (an add, a move, a tick, here or in the app) changes it."""
    from thirties_core.messages import _row_items
    items = sorted((s.id, s.start_dt.isoformat(), s.end_dt.isoformat(), s.status.value, bool(s.tentative))
                   for s in _row_items(plan))
    return hashlib.sha1(json.dumps(items).encode("utf-8")).hexdigest()[:16]


def covered_until(plan: DayPlan, now: datetime, mode: str = DEFAULT_MODE) -> Optional[datetime]:
    """When a slice shown now runs out: the start of the last row it lists (once that's begun, it has nothing
    left to say ahead). None when it lists nothing ahead."""
    block = thirty_of(now)
    starts = sorted({thirty_of(s.start_dt) for s in _mine(plan) if s.start_dt >= block + HALF})
    if not starts:
        return None
    return starts[min(NEXT_ROWS, len(starts)) - 1] if mode_of(mode) == "few" else starts[-1]


class SentSummaries:
    """When the user was last shown where the day stands (the slice, the schedule, /now), and what the plan was
    then. A notice carries the slice only when it would say something new (a bell or a check-in that leaves out what's
    next sends the user asking): nothing shown yet today, the plan changed since, the last one has run out (its last row has
    begun), or two hours have passed. Kept in sync_meta, per day."""

    KEY = "whats_next_sent"

    def __init__(self, state_db) -> None:
        self.db = state_db

    def _load(self, day: str) -> dict:
        try:
            data = json.loads(self.db.get_sync_meta(self.KEY) or "{}") if self.db is not None else {}
        except ValueError:
            data = {}
        return data if data.get("day") == day else {}

    def due(self, plan: DayPlan, now: datetime) -> bool:
        data = self._load(plan.target_date.isoformat())
        if not data:
            return True
        try:
            at = datetime.fromisoformat(data["at"])
            until = datetime.fromisoformat(data["until"]) if data.get("until") else None
        except (KeyError, ValueError):
            return True
        return (data.get("plan") != plan_signature(plan) or now - at >= RESEND_AFTER
                or (until is not None and now >= until) or (until is None and now - at >= HALF))

    def mark(self, plan: DayPlan, now: datetime, mode: str = DEFAULT_MODE, whole_day: bool = False) -> None:
        if self.db is None:
            return
        until = None if whole_day else covered_until(plan, now, mode)
        if whole_day:
            until = max((thirty_of(s.start_dt) for s in _mine(plan)), default=None)
        self.db.set_sync_meta(self.KEY, json.dumps({
            "day": plan.target_date.isoformat(), "at": now.isoformat(), "plan": plan_signature(plan),
            "until": until.isoformat() if until is not None else ""}))


# --- which turns end with it ---

_APOLOGY_RE = re.compile(r"\b(sorry|apolog\w*|my (?:mistake|bad|fault)|you'?re right|that was wrong|i was wrong|"
                         r"i (?:should(?:n'?t)? have|got (?:that|it) wrong|mixed (?:that|it) up))\b", re.IGNORECASE)


# The user questioning the plan: "why is X at 3?", "where did lunch go?", "you said you moved it"
_DISPUTE_RE = re.compile(r"\b(why|what about|how come|where(?:'s| is| are| did| does)|didn'?t you|you said|"
                         r"you (?:put|moved|left|forgot|missed|dropped|skipped)|wrong|not on (?:the|my) "
                         r"(?:schedule|list|plan))\b", re.IGNORECASE)


def _names_an_item(text: str, plan: DayPlan) -> bool:
    """The text names one of the user's own items of today that's still to come or not marked yet."""
    low = (text or "").lower()
    for s in plan.spans:
        title = s.title.strip()
        if len(title) < 3 or s.is_envelope or s.shown_only or s.status != SpanStatus.PLANNED:
            continue
        if re.search(r"(?<!\w)" + re.escape(title.lower()) + r"(?!\w)", low):
            return True
    return False


def follows_turn(tool_messages: Sequence[dict], reply: str, said: str, plan: DayPlan,
                 read_only: Iterable[str], not_done: str) -> bool:
    """Whether a turn's reply ends with the slice:
    a change was made (an add, a done or skip, a move, a drop);
    a change was tried and couldn't be made (10-26: some moves failed, three confusing replies, then the ask): the
    reply says what failed, the slice shows the plan as it really stands;
    or the reply apologised, or answered the user questioning today's plan by talking its items over, without
    changing anything (a dispute settled, then "So where does that leave today?"), unless it ends on a question the
    user should answer first. A plain question about one item ("when's dinner?") gets its answer alone."""
    read_only = set(read_only)
    for m in tool_messages:
        if m.get("role") != "tool" or m.get("name") in read_only:
            continue
        if str(m.get("content") or "").strip():  # done, or tried and refused (NOT_DONE)
            return True
    text = (reply or "").strip()
    if not text or text.splitlines()[-1].rstrip().endswith("?"):
        return False
    return bool(_APOLOGY_RE.search(text)) or (bool(_DISPUTE_RE.search(said or "")) and _names_an_item(text, plan))


__all__ = ["MODES", "DEFAULT_MODE", "PLACED", "standing", "earlier_lines", "mode_of", "asked_mode", "SentSummaries", "snapshot",
           "moved_since", "render", "summary", "unmarked_lines", "untimed_for", "untimed_lines", "free_stretches", "free_line",
           "follows_turn", "plan_signature", "covered_until"]
