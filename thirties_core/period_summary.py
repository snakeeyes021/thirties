"""What a month or a year holds, for the Timeline's Month and Year tabs, with no GUI in it (src/views/timeline.py
draws what this decides).

- **A month**: a calendar grid of weeks (starting on the user's first day of the week), each day with its items
  by their short titles, in time order, each marked planned, done, partly done, skipped or not reported. Items
  pinned to a day of a week plan and not on that day's schedule yet are listed with the day too, and what's
  pinned to a week with no day yet, with the week. Above the grid, the month plan's efforts with their budgets.
- **A year**: twelve months, each with what was done and what's planned in it, and the efforts pinned to it with
  their budgets; above them, the year plan's efforts as a whole and by quarter.

A budget (budgets.py) reads as sessions done and planned out of sessions budgeted, counted from what's on the
schedule in the budget's own period. Work and Sleep, and other people's events and unanswered invites, aren't
items here: they're the backdrop of a day, shown on the Day tab.

Reading is side-effect free: nothing here builds a day's plan (which sets up a day's Work and Sleep), so looking
at next year changes nothing. ``load_month`` and ``load_year`` read what's stored, the calendars (a month only)
and the plans; ``month_summary`` and ``year_summary`` are the pure parts, given those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from thirties_core.horizons import YEARS, Period, period_of, row_period, slot_period
from thirties_core.models import EventSpan, SpanStatus
from thirties_core.timeline_layout import _STATE, Ongoing, _default_ongoing, look_of, week_days
from thirties_core.titles import title_of

DayOf = Callable[[datetime], date]


# --- what a period holds ---------------------------------------------------------------------------------------------

@dataclass
class Entry:
    """An item on a day's schedule."""
    title: str            # the short title
    state: str            # planned / done / partial / skipped / moot
    look: str             # once / daily / ongoing / calendar (the item colours)
    start: datetime
    span_id: str

    @property
    def done(self) -> bool:
        return self.state in ("done", "partial")


@dataclass
class Effort:
    """Something pinned to a period (a week, a day of a week, a month, a quarter, the year), with its budget if it
    has one: ``sessions`` of ``minutes`` each, ``done`` and ``planned`` counted from the schedule in ``period``."""
    item_id: str
    title: str            # the short title
    list: str
    where: Period         # the finest period it's pinned to (a day of a week plan, a quarter of a year plan)
    status: str           # open / scheduled / done / gone
    period: Period        # the period its budget covers
    sessions: int = 0
    minutes: int = 0
    done: int = 0
    planned: int = 0

    @property
    def budgeted(self) -> bool:
        return self.sessions > 0

    @property
    def left(self) -> int:
        return max(0, self.sessions - self.done - self.planned)

    def progress(self) -> str:
        """'2 of 6 sessions done, 1 planned', '' with no budget."""
        if not self.budgeted:
            return ""
        text = f"{min(self.done, self.sessions)} of {self.sessions} session{'s' if self.sessions != 1 else ''} done"
        return text + (f", {self.planned} planned" if self.planned else "")

    def note(self) -> str:
        """What reads beside it: its budget's progress, else where it stands ('' while it's simply open)."""
        return self.progress() or _NOTES.get(self.status, "")


_NOTES = {"done": "done", "scheduled": "on the schedule", "gone": "not on any list"}


@dataclass
class DaySummary:
    day: date
    items: List[Entry] = field(default_factory=list)
    pinned: List[Effort] = field(default_factory=list)   # pinned to the day, not on its schedule yet
    in_month: bool = True
    is_today: bool = False
    is_past: bool = False

    @property
    def done(self) -> int:
        """The user's own items done (calendar events are shown, not counted: the Year counts the same way)."""
        return sum(1 for e in self.items if e.done and e.look != "calendar")

    @property
    def planned(self) -> int:
        return sum(1 for e in self.items if e.state == "planned" and e.look != "calendar")


@dataclass
class WeekRow:
    start: date
    days: List[DaySummary]
    pinned: List[Effort] = field(default_factory=list)   # pinned to the week, no day yet


@dataclass
class MonthSummary:
    month: Period
    weeks: List[WeekRow]
    efforts: List[Effort]                                # the month plan
    today: date

    @property
    def days(self) -> List[DaySummary]:
        return [d for w in self.weeks for d in w.days if d.in_month]

    def day(self, d: date) -> Optional[DaySummary]:
        return next((x for w in self.weeks for x in w.days if x.day == d), None)

    @property
    def done(self) -> int:
        return sum(d.done for d in self.days)

    @property
    def planned(self) -> int:
        return sum(d.planned for d in self.days)


@dataclass
class MonthCard:
    """One month of a year."""
    month: Period
    efforts: List[Effort]                                # the month plan
    weeks: List[Effort]                                  # pinned to its weeks (or days of them)
    done: int = 0                                        # items done in it
    planned: int = 0                                     # items still planned in it
    is_current: bool = False
    is_past: bool = False


@dataclass
class YearSummary:
    year: Period
    months: List[MonthCard]
    efforts: List[Effort]                                # the year plan as a whole (and a span of years over it)
    quarters: List[tuple]                                # (quarter Period, [Effort]) for the four quarters
    today: date

    @property
    def done(self) -> int:
        return sum(m.done for m in self.months)


# --- the source: what's stored, read once --------------------------------------------------------------------------

@dataclass
class PeriodSource:
    """What a summary is made from. ``spans``: the schedule over the period (and over any budget's period that
    reaches past it); ``rows``: the plans' active rows (horizons.HorizonPlans.rows); ``day_of``: the day a time
    belongs to (a day runs from its sunrise thirty to the next); ``done_since(d)``: ids of items done on or after
    ``d``; ``on_lists``: ids still on the user's lists (None when the lists aren't read)."""
    spans: Sequence[EventSpan]
    rows: Sequence[Dict[str, Any]]
    day_of: DayOf
    done_since: Callable[[date], Set[str]] = lambda d: set()
    on_lists: Optional[Set[str]] = None
    week_start: int = 0
    ongoing: Optional[Ongoing] = None


def _is_item(s: EventSpan) -> bool:
    """An item of the user's day: not Work or Sleep, not someone else's event, not an unanswered invite."""
    return not s.is_envelope and not s.shown_only and not s.tentative


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())


def _entry(s: EventSpan, ongoing: Ongoing) -> Entry:
    return Entry(title_of(s.title), _STATE.get(s.status, "planned"), look_of(s, ongoing), s.start_dt, s.id)


def _by_day(src: PeriodSource) -> Dict[date, List[EventSpan]]:
    out: Dict[date, List[EventSpan]] = {}
    seen: Set[str] = set()
    for s in src.spans:
        if s.id in seen or not _is_item(s):
            continue
        seen.add(s.id)
        out.setdefault(src.day_of(s.start_dt), []).append(s)
    for spans in out.values():
        spans.sort(key=lambda s: (s.start_dt.timestamp(), s.title))
    return out


def _budget_period(r: Dict[str, Any], week_start: int) -> Period:
    from thirties_core.budgets import budget_period
    return budget_period(r, week_start)


def _effort(r: Dict[str, Any], src: PeriodSource, by_day: Dict[date, List[EventSpan]], today: date) -> Effort:
    where = slot_period(r, src.week_start) or row_period(r, src.week_start)
    period = _budget_period(r, src.week_start)
    title = r.get("title") or ""
    in_where = [s for d, spans in by_day.items() if where.contains(d) for s in spans
                if s.task_id == r["item_id"] or _same(s.title, title)]
    if r["item_id"] in src.done_since(where.start) or any(s.status == SpanStatus.DONE for s in in_where
                                                           if not int(r.get("sessions") or 0)):
        status = "done"
    elif any(s.status == SpanStatus.PLANNED for s in in_where):
        status = "scheduled"
    elif src.on_lists is not None and r["item_id"] not in src.on_lists:
        status = "gone"
    else:
        status = "open"
    e = Effort(r["item_id"], title_of(title), r.get("list_title") or "", where, status, period,
               int(r.get("sessions") or 0), int(r.get("minutes") or 0))
    if e.budgeted:
        e.minutes = e.minutes or 60
        for d, spans in by_day.items():
            if not period.contains(d):
                continue
            for s in spans:
                if s.source == "calendar" or not _same(s.title, title):
                    continue
                if s.status == SpanStatus.DONE:
                    e.done += 1
                elif s.status == SpanStatus.PLANNED and d >= today:
                    e.planned += 1
    return e


def _order(efforts: List[Effort]) -> List[Effort]:
    """Budgeted efforts first (the trickle-down work), then the rest, each by title; what's gone from the lists
    last."""
    return sorted(efforts, key=lambda e: (e.status == "gone", not e.budgeted, e.title.lower()))


# --- a month -------------------------------------------------------------------------------------------------------

def month_grid(month: Period, week_start: int = 0) -> List[List[date]]:
    """The month's calendar grid: whole weeks from the one holding its first day to the one holding its last."""
    weeks, d = [], month.start
    while d <= month.end:
        days = week_days(d, week_start)
        weeks.append(days)
        d = days[-1] + timedelta(days=1)
    return weeks


def month_summary(month: Any, src: PeriodSource, today: date) -> MonthSummary:
    """A month (a Period, or any day in it) as a calendar grid of days with their items and pins, and the month
    plan's efforts. Days of the weeks either side that fall in the grid are filled in too (``in_month`` False)."""
    month = month if isinstance(month, Period) else period_of("month", month)
    ongoing = src.ongoing or _default_ongoing
    by_day = _by_day(src)
    grid = month_grid(month, src.week_start)
    weeks: List[WeekRow] = []
    rows = list(src.rows)
    for days in grid:
        week = period_of("week", days[0], src.week_start)
        week_rows = [r for r in rows if r["level"] == "week" and r["period"] == week.start.isoformat()]
        row = WeekRow(days[0], [], [])
        for d in days:
            ds = DaySummary(d, [_entry(s, ongoing) for s in by_day.get(d, [])], [], month.contains(d), d == today,
                            d < today)
            for r in week_rows:
                if r.get("slot") == d.isoformat():
                    e = _effort(r, src, by_day, today)
                    if e.status != "scheduled" and not any(_same(x.title, e.title) for x in ds.items):
                        ds.pinned.append(e)
            row.days.append(ds)
        row.pinned = _order([_effort(r, src, by_day, today) for r in week_rows if not r.get("slot")])
        weeks.append(row)
    efforts = [_effort(r, src, by_day, today) for r in rows
               if r["level"] == "month" and r["period"] == month.start.isoformat()]
    return MonthSummary(month, weeks, _order(efforts), today)


# --- a year --------------------------------------------------------------------------------------------------------

def year_summary(year: Any, src: PeriodSource, today: date) -> YearSummary:
    """A year (a Period, a year number, or any day in it) as twelve months, each with its plan's efforts and its
    weeks' efforts, and what was done and what's planned in it; and the year plan's efforts, as a whole (with any
    span of years over it) and by quarter."""
    if isinstance(year, int):
        year = date(year, 1, 1)
    year = year if isinstance(year, Period) else period_of("year", year)
    by_day = _by_day(src)
    rows = list(src.rows)
    months: List[MonthCard] = []
    for m in range(1, 13):
        month = period_of("month", date(year.start.year, m, 1))
        efforts = [_effort(r, src, by_day, today) for r in rows
                   if r["level"] == "month" and r["period"] == month.start.isoformat()]
        weeks = [_effort(r, src, by_day, today) for r in rows if r["level"] == "week"
                 and month.contains(date.fromisoformat(r.get("slot") or r["period"]))]
        items = [s for d, spans in by_day.items() if month.contains(d) for s in spans if s.source != "calendar"]
        months.append(MonthCard(
            month, _order(efforts), _order(weeks),
            done=sum(1 for s in items if s.status in (SpanStatus.DONE, SpanStatus.PARTIAL)),
            planned=sum(1 for s in items if s.status == SpanStatus.PLANNED),
            is_current=month.contains(today), is_past=month.end < today))
    whole = [_effort(r, src, by_day, today) for r in rows
             if (r["level"] == "year" and r["period"] == year.start.isoformat() and not r.get("slot"))
             or (r["level"] == YEARS and row_period(r, src.week_start).overlaps(year))]
    quarters = []
    for q in range(4):
        quarter = period_of("quarter", date(year.start.year, 3 * q + 1, 1))
        quarters.append((quarter, _order([_effort(r, src, by_day, today) for r in rows if r["level"] == "year"
                                          and r["period"] == year.start.isoformat()
                                          and r.get("slot") == quarter.start.isoformat()])))
    return YearSummary(year, months, _order(whole), quarters, today)


# --- reading it ----------------------------------------------------------------------------------------------------

def diurnal_day_of(config) -> DayOf:
    """The day a time belongs to: a day runs from its sunrise thirty to the next day's (DayPlan.day_start), so
    something at 1 AM is the night of the day before. Cached by date; pure arithmetic."""
    from thirties_core.astronomy import build_base_thirties
    gen = config.general
    starts: Dict[date, datetime] = {}

    def start_of(d: date) -> datetime:
        if d not in starts:
            starts[d] = build_base_thirties(gen.latitude, gen.longitude, d, gen.timezone)[0].start_dt
        return starts[d]

    def day_of(t: datetime) -> date:
        from zoneinfo import ZoneInfo
        local = t.astimezone(ZoneInfo(gen.timezone)) if t.tzinfo else t
        d = local.date()
        return d if local.timestamp() >= start_of(d).timestamp() else d - timedelta(days=1)
    return day_of


def _reach(rows: Iterable[Dict[str, Any]], period: Period, week_start: int) -> Period:
    """The period plus every budget period of a row that touches it: what the schedule is read over."""
    lo, hi = period.start, period.end
    for r in rows:
        try:
            p = _budget_period(r, week_start)
            where = slot_period(r, week_start) or row_period(r, week_start)
        except (KeyError, ValueError):
            continue
        if where.overlaps(period) or p.overlaps(period):
            lo, hi = min(lo, p.start), max(hi, p.end)
    return Period(period.level, lo, hi)


def _source(scheduler, rows, first: date, last: date, tasks=None, week_start: int = 0,
            ongoing: Optional[Ongoing] = None) -> PeriodSource:
    """What's stored from ``first`` to ``last`` (and its last night), with the plans' rows."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(scheduler.config.general.timezone)
    db = scheduler.state_db
    start = datetime.combine(first, time(0), tzinfo=tz)
    end = datetime.combine(last + timedelta(days=2), time(0), tzinfo=tz)  # the last day's night
    spans = list(db.get_spans_between(start, end))
    on_lists = {t.id for t in tasks} if tasks else None
    return PeriodSource(spans, rows, diurnal_day_of(scheduler.config), db.completed_since, on_lists, week_start,
                        ongoing)


def calendar_spans(scheduler, first: date, last: date) -> List[EventSpan]:
    """The calendars' events from ``first`` to ``last`` as items, with the user's answers and recorded outcomes
    applied (what build_day_plan does for one day, without setting a day up). A calendar that can't be read is
    left out."""
    from zoneinfo import ZoneInfo
    from thirties_core.calendar_engine import events_to_spans
    from thirties_core.calendar_outcomes import CalendarOutcomes
    tz = ZoneInfo(scheduler.config.general.timezone)
    events: Dict[str, Any] = {}
    d = first
    while d <= last + timedelta(days=1):
        try:
            for e in scheduler.calendar_engine.fetch_events_for_day(d, tz):
                events.setdefault(e.id, e)
        except Exception:
            pass
        d += timedelta(days=1)
    spans, _pending = events_to_spans(list(events.values()), scheduler.state_db.get_event_decisions(),
                                      getattr(scheduler.config.calendar, "modes", None))
    CalendarOutcomes(scheduler.state_db).apply(spans)
    return spans


def load_month(scheduler, month: Any, today: date, tasks=None, week_start: int = 0,
               ongoing: Optional[Ongoing] = None) -> MonthSummary:
    """The month holding ``month`` (a day or a Period), read from the store, the calendars and the plans."""
    from thirties_core.horizons import HorizonPlans
    month = month if isinstance(month, Period) else period_of("month", month)
    rows = HorizonPlans(scheduler.state_db).rows()
    grid = month_grid(month, week_start)
    shown = Period("month", grid[0][0], grid[-1][-1])
    reach = _reach(rows, shown, week_start)
    src = _source(scheduler, rows, reach.start, reach.end, tasks, week_start, ongoing)
    src.spans = list(src.spans) + calendar_spans(scheduler, shown.start, shown.end)
    return month_summary(month, src, today)


def load_year(scheduler, year: Any, today: date, tasks=None, week_start: int = 0) -> YearSummary:
    """The year holding ``year`` (a year number, a day or a Period), read from the store and the plans (not the
    calendars: a year of them is a lot of reading for counts)."""
    from thirties_core.horizons import HorizonPlans
    if isinstance(year, int):
        year = date(year, 1, 1)
    year = year if isinstance(year, Period) else period_of("year", year)
    rows = HorizonPlans(scheduler.state_db).rows()
    reach = _reach(rows, year, week_start)
    src = _source(scheduler, rows, reach.start, reach.end, tasks, week_start)
    return year_summary(year, src, today)


__all__ = ["Entry", "Effort", "DaySummary", "WeekRow", "MonthSummary", "MonthCard", "YearSummary", "PeriodSource",
           "month_grid", "month_summary", "year_summary", "diurnal_day_of", "calendar_spans", "load_month",
           "load_year"]
