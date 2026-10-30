"""How the Timeline page lays out a day and a week, with no GUI in it (src/views/timeline.py draws what this decides).

The design agreed with the user over four mockup rounds (10-30), replacing the old timeline ("fugly and confusing,
not very GNOME HIG"):

- **Day**: the waking day as a stack of slots, one per thirty, top = waking up, bottom = bedtime. No clock times
  anywhere ("try it with none"); an item's real times are shown on hover or tap (AGENTS.md §7.1: events keep their
  real times; blocks are a view). A left rail has the sky (daylight or dark, to the minute, from the plan's sunrise
  and sunset). Work is one hatched card under whatever is placed during it. Items are cards on their slots, side by
  side in lanes when they share time (item 17: nothing hides another), each with "pips", one per thirty it spans.
  Free time is what has nothing in it and isn't Work or Sleep, from now on, shown with pips and no words.
  Items can be dragged to another thirty, resized from either edge, or removed; each gesture becomes one tool call
  (``move_call``, ``resize_call``, ``delete_call``), the same tools the assistant uses.
- **Week**: seven shrunken waking days on one axis, with each day's free time, and the items pinned to the week but
  not to a day yet (horizons.py) as chips to drop onto a day.

The display rules of the Day page and the old timeline carry over (day_layout.py, stream I): a moot item with
nothing said reads "Not reported"; a done session on something ongoing reads "Worked on"; someone else's calendar
events ("show only") and unanswered invites are quiet notes where they start, never cards of their own, and never
make the user busy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from thirties_core.clock import format_clock, thirty_of
from thirties_core.day_layout import WORKED_ON
from thirties_core.kinds import DAILY, ONGOING, kind_by_wording
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

THIRTY = timedelta(minutes=30)

# Free time in blocks (thirties) or hours: one setting about the person, so synced (settings.py), blocks by default.
UNITS_SETTING = "timeline.free_units"
UNITS = ("blocks", "hours")
DEFAULT_UNITS = "blocks"

Ongoing = Callable[[EventSpan], bool]


def _default_ongoing(span: EventSpan) -> bool:
    return kind_by_wording(span.title) == ONGOING


# --- units ----------------------------------------------------------------------------------------------------------

def amount_text(blocks: int, units: str = DEFAULT_UNITS) -> str:
    """'7 blocks', '1 block', or in hours '3½ h', '½ h', '0 h'. Units are always shown."""
    blocks = max(0, int(blocks))
    if units == "hours":
        whole, half = divmod(blocks, 2)
        number = (str(whole) if whole else "") + ("½" if half else "")
        return f"{number or '0'} h"
    return f"{blocks} block" + ("" if blocks == 1 else "s")


def free_text(blocks: int, units: str = DEFAULT_UNITS) -> str:
    """'7 blocks free', '1 block free', '3½ h free'."""
    return amount_text(blocks, units) + " free"


def normalise_units(value: Any) -> str:
    return value if value in UNITS else DEFAULT_UNITS


# --- the day --------------------------------------------------------------------------------------------------------

@dataclass
class Slot:
    """One thirty of the waking day, as a row of the page."""
    row: int
    index: int            # the block's index on the plan's canvas
    start: datetime
    end: datetime
    work: bool = False    # Work overlaps it
    sleep: bool = False   # Sleep covers it (only shown when something is planned in it)
    free: bool = False    # nothing in it, not Work, not Sleep, and not over yet
    past: bool = False


@dataclass
class Band:
    """A stretch of the rail, in rows (fractions of a slot, to the minute)."""
    top: float
    bottom: float
    daylight: bool = True


@dataclass
class FreeStretch:
    row: int
    rows: int             # how many blocks free
    start: datetime
    end: datetime


@dataclass
class Card:
    """An item on its slots."""
    span: EventSpan
    row: int              # first slot it sits on
    rows: int             # slots it covers on the page
    pips: int             # thirties it spans
    look: str = "once"    # the colour: once / daily / ongoing / calendar
    state: str = "planned"  # planned / done / partial / skipped / moot
    status: str = ""      # what it reads, once marked: Done, Worked on, Skipped, Partly done, Not reported
    current: bool = False
    tickable: bool = False      # can be ticked done (planned, the user's, not an unanswered invite)
    finishes_now: bool = False  # ticking it means it finished at the tick (it's happening now)
    lane: int = 0
    lanes: int = 1
    notes: List[str] = field(default_factory=list)  # someone else's events and invites that start here

    @property
    def times(self) -> str:
        """The real times, for the tooltip: '2:10 PM – 3:00 PM', and when it really happened if that differs."""
        s = self.span
        text = f"{format_clock(s.start_dt)} – {format_clock(s.end_dt)}"
        if s.status != SpanStatus.PLANNED and s.actual_start and s.actual_end and \
                (s.actual_start, s.actual_end) != (s.start_dt, s.end_dt):
            text += f" (really {format_clock(s.actual_start)} – {format_clock(s.actual_end)})"
        return text


@dataclass
class SlotNote:
    """A quiet note on a slot with no card: someone else's event, or an invite, starting there."""
    row: int
    text: str
    span: EventSpan


@dataclass
class DayLayout:
    plan: DayPlan
    slots: List[Slot]
    sky: List[Band]
    work: List[Card]       # Work, one card each, drawn under the items
    cards: List[Card]
    free: List[FreeStretch]
    notes: List[SlotNote]
    now_row: Optional[float] = None   # where now is, in rows; None when now isn't on the waking day shown
    now_daylight: bool = True

    @property
    def free_blocks(self) -> int:
        return sum(f.rows for f in self.free)

    @property
    def start(self) -> datetime:
        return self.slots[0].start

    @property
    def end(self) -> datetime:
        return self.slots[-1].end

    def row_at(self, t: datetime) -> float:
        """Where a time falls, in rows (clamped to the page)."""
        return _row_at(self.slots, t)

    def stretch_at(self, row: int) -> Optional[FreeStretch]:
        return next((f for f in self.free if f.row <= row < f.row + f.rows), None)


def _row_at(slots: Sequence[Slot], t: datetime) -> float:
    ts = t.timestamp()
    if ts <= slots[0].start.timestamp():
        return 0.0
    for s in slots:
        lo, hi = s.start.timestamp(), s.end.timestamp()
        if lo <= ts < hi:
            return s.row + (ts - lo) / (hi - lo)
    return float(len(slots))


def _shown_as_note(span: EventSpan) -> bool:
    """Someone else's event, or an invite not answered: shown, but the user isn't busy with it (stream I's rule)."""
    return span.shown_only or span.tentative


def _awake_range(plan: DayPlan, items: List[EventSpan]) -> Tuple[int, int]:
    """The waking day: the longest run of thirties Sleep doesn't cover, widened to take in anything planned outside
    it (a late movie past bedtime is shown, in the dark, rather than hidden). With no Sleep, the whole canvas.

    A wake-up before the sunrise thirty falls at the end of the previous diurnal day's canvas (the canvas starts at
    sunrise); that early stretch is then the bottom of the day before, not shown here unless something is in it."""
    sleeps = [s for s in plan.active_spans() if s.category == SpanCategory.SLEEP and s.status != SpanStatus.SKIPPED]
    asleep = [any(s.start_dt.timestamp() <= b.start_dt.timestamp() and b.end_dt.timestamp() <= s.end_dt.timestamp()
                  for s in sleeps) for b in plan.blocks]
    best, run_start = (0, len(plan.blocks) - 1), None
    best_len = 0
    for i, sleeping in enumerate(asleep + [True]):
        if not sleeping and run_start is None:
            run_start = i
        elif sleeping and run_start is not None:
            if i - run_start > best_len:
                best, best_len = (run_start, i - 1), i - run_start
            run_start = None
    if best_len == 0 and any(asleep):
        best = (0, len(plan.blocks) - 1)
    lo, hi = best
    for s in items:
        first = plan.block_at(max(s.start_dt, plan.day_start))
        last = plan.block_at(min(s.end_dt, plan.day_end) - timedelta(microseconds=1))
        if first is not None:
            lo = min(lo, first.index)
        if last is not None:
            hi = max(hi, last.index)
    return lo, hi


def _assign_lanes(cards: List[Card]) -> None:
    """Things sharing time sit side by side (item 17: nothing hides another). A cluster is a run of cards that
    overlap one another in rows; every card in it gets the cluster's lane count, so widths line up."""
    cards.sort(key=lambda c: (c.row, -c.rows, c.span.start_dt.timestamp(), c.span.title))
    cluster: List[Card] = []
    cluster_end = -1
    lane_ends: List[int] = []

    def close() -> None:
        for c in cluster:
            c.lanes = max(1, len(lane_ends))

    for c in cards:
        if cluster and c.row >= cluster_end:
            close()
            cluster, lane_ends = [], []
        lane = next((i for i, end in enumerate(lane_ends) if end <= c.row), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(0)
        lane_ends[lane] = c.row + c.rows
        c.lane = lane
        cluster.append(c)
        cluster_end = max(cluster_end, c.row + c.rows)
    if cluster:
        close()


_STATE = {SpanStatus.PLANNED: "planned", SpanStatus.DONE: "done", SpanStatus.PARTIAL: "partial",
          SpanStatus.SKIPPED: "skipped", SpanStatus.UNKNOWN: "moot"}
_STATUS = {SpanStatus.DONE: "Done", SpanStatus.PARTIAL: "Partly done", SpanStatus.SKIPPED: "Skipped",
           SpanStatus.UNKNOWN: "Not reported"}


def look_of(span: EventSpan, ongoing: Optional[Ongoing] = None) -> str:
    """The card's colour: a calendar event, an ongoing item (sessions), a daily item, or a one-off."""
    if span.category == SpanCategory.CALENDAR or span.source == "calendar":
        return "calendar"
    if (ongoing or _default_ongoing)(span):
        return "ongoing"
    if kind_by_wording(span.title) == DAILY:
        return "daily"
    return "once"


def tickable(span: EventSpan) -> bool:
    """What the Day page lets you mark done (agenda._markable): planned activities and tasks, and the user's own
    calendar events (soak 1: the outcome is kept in Thirties, the calendar untouched)."""
    if span.status != SpanStatus.PLANNED or span.tentative or span.shown_only or span.is_envelope:
        return False
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK, SpanCategory.CALENDAR)


def _status(span: EventSpan, ongoing: Ongoing) -> str:
    if span.status == SpanStatus.DONE and ongoing(span):
        return WORKED_ON
    return _STATUS.get(span.status, "")


def day_layout(plan: DayPlan, now: Optional[datetime] = None, ongoing: Optional[Ongoing] = None) -> DayLayout:
    """Lay a day out. ``now`` is the real time (always pass it): free time starts at the current thirty, so a past
    day has none and a later day all of it. ``ongoing`` says which items are ongoing (the assistant's reading of
    kinds); by default, the wording."""
    ongoing = ongoing or _default_ongoing
    in_view = [s for s in plan.spans_in_view() if not s.is_envelope]
    items = [s for s in in_view if not _shown_as_note(s)]
    lo, hi = _awake_range(plan, items)
    blocks = plan.blocks[lo:hi + 1]
    busy = [s for s in plan.active_spans() if s.status != SpanStatus.SKIPPED]
    now_ts = now.timestamp() if now is not None else None

    slots: List[Slot] = []
    for row, b in enumerate(blocks):
        there = [s for s in busy if s.overlaps(b.start_dt, b.end_dt)]
        sleep = any(s.category == SpanCategory.SLEEP and s.start_dt.timestamp() <= b.start_dt.timestamp()
                    and b.end_dt.timestamp() <= s.end_dt.timestamp() for s in there)
        past = now_ts is not None and b.end_dt.timestamp() <= now_ts
        slots.append(Slot(row, b.index, b.start_dt, b.end_dt,
                          work=any(s.category == SpanCategory.WORK for s in there), sleep=sleep,
                          free=not there and not past, past=past))

    # free stretches: runs of free slots
    free: List[FreeStretch] = []
    for s in slots:
        if not s.free:
            continue
        if free and free[-1].row + free[-1].rows == s.row:
            free[-1].rows += 1
            free[-1].end = s.end
        else:
            free.append(FreeStretch(s.row, 1, s.start, s.end))

    start, end = slots[0].start, slots[-1].end

    # the sky, to the minute: dark until sunrise, daylight until sunset, dark until the next sunrise. The next
    # sunrise is taken a day after this one (a minute or two off at most, and only for a waking day that runs on
    # into the next morning).
    edges = sorted({t for t in (plan.sunrise, plan.sunset, plan.sunrise + timedelta(days=1))
                    if start < t < end} | {start, end}, key=lambda t: t.timestamp())
    sky: List[Band] = []
    for a, b in zip(edges, edges[1:]):
        mid = a + (b - a) / 2
        daylight = plan.sunrise.timestamp() <= mid.timestamp() < plan.sunset.timestamp()
        top, bottom = _row_at(slots, a), _row_at(slots, b)
        if sky and sky[-1].daylight == daylight:
            sky[-1].bottom = bottom
        elif bottom > top:
            sky.append(Band(top, bottom, daylight))

    by_index = {s.index: s for s in slots}
    cards: List[Card] = []
    for s in items:
        first = plan.block_at(max(s.start_dt, plan.day_start, key=lambda t: t.timestamp()))
        last = plan.block_at(min(s.end_dt, plan.day_end, key=lambda t: t.timestamp()) - timedelta(microseconds=1))
        if first is None or last is None or first.index not in by_index or last.index not in by_index:
            continue
        row = by_index[first.index].row
        rows = by_index[last.index].row - row + 1
        current = now_ts is not None and s.covers(now)
        cards.append(Card(
            span=s, row=row, rows=rows, pips=last.index - first.index + 1, look=look_of(s, ongoing),
            state=_STATE.get(s.status, "planned"), status=_status(s, ongoing), current=current,
            tickable=tickable(s),
            finishes_now=current and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)))
    _assign_lanes(cards)

    # Work: one card for its whole time, under what's placed during it (10-30: "a monolith", not a block per thirty)
    work: List[Card] = []
    for s in sorted((s for s in busy if s.category == SpanCategory.WORK), key=lambda s: s.start_dt.timestamp()):
        first = plan.block_at(max(s.start_dt, start, key=lambda t: t.timestamp()))
        last = plan.block_at(min(s.end_dt, end, key=lambda t: t.timestamp()) - timedelta(microseconds=1))
        if first is None or last is None or first.index not in by_index or last.index not in by_index:
            continue
        row = by_index[first.index].row
        work.append(Card(span=s, row=row, rows=by_index[last.index].row - row + 1, pips=last.index - first.index + 1,
                         look="work"))

    # someone else's events and invites: a note on the card where they start, else on the slot
    notes: List[SlotNote] = []
    for s in in_view:
        if not _shown_as_note(s):
            continue
        text = f"{s.title} ({'not yours' if s.shown_only else 'invite, not answered'})"
        at = plan.block_at(max(s.start_dt, start, key=lambda t: t.timestamp()))
        if at is None or at.index not in by_index:
            continue
        row = by_index[at.index].row
        host = next((c for c in sorted(cards, key=lambda c: c.lane) if c.row <= row < c.row + c.rows), None)
        if host is not None:
            host.notes.append(f"{text}, {format_clock(s.start_dt)} – {format_clock(s.end_dt)}")
        else:
            notes.append(SlotNote(row, text, s))

    now_row, now_daylight = None, True
    if now is not None and start.timestamp() <= now.timestamp() < end.timestamp():
        now_row = _row_at(slots, now)
        now_daylight = plan.sunrise.timestamp() <= now.timestamp() < plan.sunset.timestamp()
    return DayLayout(plan, slots, sky, work, cards, free, notes, now_row, now_daylight)


# --- the week -------------------------------------------------------------------------------------------------------

@dataclass
class WeekBar:
    """An item in a day's column, as fractions of the week's axis (0 = top)."""
    span: EventSpan
    top: float
    bottom: float
    look: str = "once"
    state: str = "planned"
    lane: int = 0
    lanes: int = 1


@dataclass
class WeekColumn:
    day: date
    awake: Tuple[float, float]          # the waking day on the axis
    bars: List[WeekBar]
    work: List[Tuple[float, float]]
    free_blocks: int
    is_today: bool = False
    is_past: bool = False
    now: Optional[float] = None         # today: where now is on the axis


@dataclass
class WeekLayout:
    columns: List[WeekColumn]
    axis: Tuple[float, float]           # the axis, in minutes after each day's own midnight (_minutes)

    @property
    def total_free(self) -> int:
        return sum(c.free_blocks for c in self.columns)


def week_days(today: date, week_start: int = 0) -> List[date]:
    """The seven days of the week holding ``today``, starting on the user's first day (general.week_start)."""
    start = today - timedelta(days=(today.weekday() - week_start) % 7)
    return [start + timedelta(days=i) for i in range(7)]


def _minutes(day: date, t: datetime) -> float:
    """Minutes after the day's midnight (past 1440 after midnight): one axis for every day of the week."""
    midnight = datetime.combine(day, time(0), tzinfo=t.tzinfo)
    return (t.timestamp() - midnight.timestamp()) / 60


def week_layout(plans: Sequence[DayPlan], now: Optional[datetime], today: date,
                ongoing: Optional[Ongoing] = None) -> WeekLayout:
    """Seven shrunken waking days on one axis (by time of day, so 3 PM lines up across the week): items as small
    bars, side by side when they share time, Work as a bar, each day's free time (today's from now on)."""
    days = [(p, day_layout(p, now, ongoing)) for p in plans]
    if not days:
        return WeekLayout([], (0.0, 1.0))
    lo = min(_minutes(p.target_date, d.start) for p, d in days)
    hi = max(_minutes(p.target_date, d.end) for p, d in days)
    span = max(hi - lo, 1.0)

    def pos(day: date, t: datetime) -> float:
        return min(1.0, max(0.0, (_minutes(day, t) - lo) / span))

    columns: List[WeekColumn] = []
    for plan, d in days:
        day = plan.target_date
        bars = [WeekBar(c.span, pos(day, max(c.span.start_dt, d.start, key=lambda t: t.timestamp())),
                        pos(day, min(c.span.end_dt, d.end, key=lambda t: t.timestamp())), c.look, c.state,
                        c.lane, c.lanes) for c in d.cards]
        work = [(pos(day, max(s.start_dt, d.start, key=lambda t: t.timestamp())),
                 pos(day, min(s.end_dt, d.end, key=lambda t: t.timestamp())))
                for s in plan.active_spans() if s.category == SpanCategory.WORK and s.status != SpanStatus.SKIPPED
                and s.overlaps(d.start, d.end)]
        col_now = pos(day, now) if now is not None and d.now_row is not None else None
        columns.append(WeekColumn(day, (pos(day, d.start), pos(day, d.end)), bars, work, d.free_blocks,
                                  is_today=day == today, is_past=day < today, now=col_now))
    return WeekLayout(columns, (lo, hi))


def unplaced_in_week(planner, today: date) -> List[Dict[str, str]]:
    """Items pinned to this week but not to a day of it, still open (horizons.HorizonPlans): the chips of "This
    week, no day yet". What's done, gone from the lists, or already on a day's schedule isn't waiting for a day."""
    from thirties_core.horizons import period_of
    week = period_of("week", today, planner.week_start)
    return [{"item_id": r["item_id"], "title": r["title"], "list": r.get("list_title", "")}
            for r in planner.plan_items(week) if not r.get("slot") and r.get("status") == "open"]


# --- planning something into a free stretch ---------------------------------------------------------------------------

def stretch_options(tools, plan: DayPlan, stretch: FreeStretch, minutes: int = 30, now: Optional[datetime] = None):
    """Where in a free stretch something could go: placement.candidate_slots (the slot_options tool's own
    reasoning: right after what precedes it, in the middle, right before what follows), bounded to the stretch."""
    from thirties_core.placement import candidate_slots
    live = now if now is not None and plan.day_start <= now < plan.day_end else None
    return candidate_slots(tools, plan, minutes, live, after=stretch.start, before=stretch.end)


def what_to_plan(tools, day: date, limit: int = 5) -> List[Dict[str, str]]:
    """What could go into free time, best first (horizons.candidates_for_day: pinned to the day, due, carried, the
    week plan's): offered as one-tap choices when planning something here. Empty when there's no backlog."""
    try:
        from thirties_core.horizons import candidates_for_day
        return [{"task_id": c.task_id, "title": c.title, "why": c.why}
                for c in candidates_for_day(tools, day, limit=limit)]
    except Exception:
        return []


# --- changing the plan by hand: each gesture is one tool call ----------------------------------------------------------

def movable(span: EventSpan) -> bool:
    """What can be dragged, resized or removed: the user's own planned things. Not Work or Sleep (the tools change
    those only when the user talks about them), not calendar events (fixed: their time is the calendar's)."""
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and span.source != "calendar" \
        and not span.tentative and not span.shown_only


def snap_row(rows: float, count: int) -> int:
    """The slot a point falls in (rows from the top of the page), on the page."""
    return max(0, min(count - 1, int(rows // 1)))


def snap_boundary(rows: float, count: int) -> int:
    """The nearest boundary between slots (0 = the top of the first, count = the bottom of the last)."""
    return max(0, min(count, int(round(rows))))


def move_target(layout: DayLayout, card: Card, grab_row: int, drop_rows: float) -> int:
    """Where a card dragged by its ``grab_row``-th thirty lands: its first slot, snapped to a thirty."""
    n = len(layout.slots)
    return max(0, min(n - 1, snap_row(drop_rows, n) - grab_row))


def move_call(layout: DayLayout, card: Card, grab_row: int, drop_rows: float, push: bool = False
              ) -> Optional[Tuple[str, Dict[str, Any]]]:
    """A drag: update_event to the thirty it was dropped on, keeping its length. Dropped onto another item, the two
    share the time (item 17); only with ``push`` (a modifier held) does what follows slide later (push_later)."""
    if not movable(card.span):
        return None
    row = move_target(layout, card, grab_row, drop_rows)
    if row == card.row:
        return None
    args: Dict[str, Any] = {"event": card.span.id, "start": format_clock(layout.slots[row].start)}
    if push:
        args["push_later"] = True
    return "update_event", args


def resize_extent(layout: DayLayout, card: Card, edge: str, rows: float) -> Tuple[int, int]:
    """The card's slots (first, count) while its top or bottom edge is dragged to ``rows``, snapped to thirties; at
    least one thirty."""
    n = len(layout.slots)
    b = snap_boundary(rows, n)
    end_row = card.row + card.rows
    if edge == "top":
        b = min(b, end_row - 1)
        return b, end_row - b
    b = max(b, card.row + 1)
    return card.row, b - card.row


def resize_call(layout: DayLayout, card: Card, edge: str, rows: float) -> Optional[Tuple[str, Dict[str, Any]]]:
    """A resize from the top (a new start, the end kept) or the bottom (a new end, the start kept): update_event."""
    if not movable(card.span):
        return None
    first, count = resize_extent(layout, card, edge, rows)
    span = card.span
    if edge == "top":
        if first == card.row:
            return None
        return "update_event", {"event": span.id, "start": format_clock(layout.slots[first].start),
                                "end": format_clock(span.end_dt)}
    if count == card.rows:
        return None
    return "update_event", {"event": span.id, "end": format_clock(layout.slots[first + count - 1].end)}


def delete_call(span: EventSpan) -> Optional[Tuple[str, Dict[str, Any]]]:
    return ("remove_event", {"event": span.id}) if movable(span) else None


def restore_call(span: EventSpan) -> Tuple[str, Dict[str, Any]]:
    """Undo for a removal: the same item back at its own times (a new id; alongside, so nothing is pushed)."""
    args: Dict[str, Any] = {"title": span.title, "start": format_clock(span.start_dt),
                            "end": format_clock(span.end_dt), "alongside": True}
    if span.task_id:
        args.update(category="task", task_id=span.task_id)
    return "add_event", args


__all__ = ["movable", "snap_row", "snap_boundary", "move_target", "move_call", "resize_extent", "resize_call",
           "delete_call", "restore_call",
           "UNITS", "UNITS_SETTING", "DEFAULT_UNITS", "amount_text", "free_text", "normalise_units", "Slot", "Band",
           "FreeStretch", "Card", "SlotNote", "DayLayout", "day_layout", "look_of", "tickable", "WeekBar",
           "WeekColumn", "WeekLayout", "week_days", "week_layout", "unplaced_in_week", "stretch_options",
           "what_to_plan"]
