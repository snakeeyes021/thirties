"""What a month or a year holds (thirties_core/period_summary.py): the month's grid of days with their items and
pins, the year's months with their efforts and budgets. Invented items throughout."""

import unittest
from datetime import date, datetime, time, timedelta

from tests.fixtures import DAY, TZ, at, event, make_scheduler
from thirties_core.horizons import HorizonPlans, period_of, years_period
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.period_summary import (PeriodSource, diurnal_day_of, load_month, load_year, month_grid,
                                          month_summary, year_summary)

OCT = period_of("month", DAY)           # October 2026; DAY is Thursday Oct 15
WEEK = period_of("week", DAY)           # Mon Oct 12 - Sun Oct 18


def on(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=TZ)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.sched = make_scheduler()
        self.db = self.sched.state_db
        self.plans = HorizonPlans(self.db)
        self.n = 0

    def span(self, title, day, start="18:00", minutes=60, status=SpanStatus.PLANNED,
             category=SpanCategory.ACTIVITY, **kw):
        self.n += 1
        s = on(day, start)
        span = EventSpan(f"s{self.n}", title, s, s + timedelta(minutes=minutes), category, status=status, **kw)
        self.db.upsert_span(span)
        return span

    def pin(self, item_id, title, level, period_start, slot="", sessions=0, minutes=0, list_title="Home"):
        p = period_of(level, period_start) if level != "years" else years_period(period_start.year, period_start.year + 2)
        self.plans.put(item_id, title, "n_home", list_title, p, slot)
        if sessions:
            self.plans.set_effort(f"{p.key}|{item_id}", sessions, minutes)

    def source(self, first=date(2026, 1, 1), last=date(2026, 12, 31), on_lists=None, done=()):
        start, end = datetime.combine(first, time(0), tzinfo=TZ), datetime.combine(last, time(0), tzinfo=TZ)
        return PeriodSource(self.db.get_spans_between(start, end + timedelta(days=2)), self.plans.rows(),
                            diurnal_day_of(self.sched.config), lambda d: set(done), on_lists)


class MonthGridTest(unittest.TestCase):
    def test_whole_weeks_from_the_first_days_week_to_the_last_days(self):
        weeks = month_grid(OCT)  # Oct 1 2026 is a Thursday
        self.assertEqual(weeks[0][0], date(2026, 9, 28))
        self.assertEqual(weeks[-1][-1], date(2026, 11, 1))
        self.assertEqual(len(weeks), 5)
        self.assertTrue(all(len(w) == 7 for w in weeks))

    def test_the_users_first_day_of_the_week(self):
        weeks = month_grid(OCT, week_start=6)  # Sunday
        self.assertEqual(weeks[0][0], date(2026, 9, 27))
        self.assertEqual(weeks[0][0].weekday(), 6)


class MonthTest(Fixture):
    def test_a_days_items_by_short_title_in_time_order_with_done_marked(self):
        self.span("Call the plumber", DAY, "09:00", status=SpanStatus.DONE)
        self.span("**Shelf brackets: measured** (see the sketch, https://example.org/plan). Next: buy screws",
                  DAY, "08:00")
        self.span("Skipped walk", DAY, "11:00", status=SpanStatus.SKIPPED)
        m = month_summary(DAY, self.source(), DAY)
        d = m.day(DAY)
        self.assertEqual([e.title for e in d.items], ["Shelf brackets: measured", "Call the plumber", "Skipped walk"])
        self.assertEqual([e.state for e in d.items], ["planned", "done", "skipped"])
        self.assertTrue(d.is_today)
        self.assertEqual((d.done, d.planned), (1, 1))
        self.assertTrue(m.day(DAY - timedelta(days=1)).is_past)

    def test_work_sleep_and_other_peoples_events_are_not_items(self):
        self.span("Work", DAY, "09:00", 480, category=SpanCategory.WORK)
        self.span("Sleep", DAY, "23:00", 480, category=SpanCategory.SLEEP)
        self.span("Choir (partner)", DAY, "19:00", category=SpanCategory.CALENDAR, source="calendar",
                  shown_only=True)
        self.span("Party invite", DAY, "20:00", category=SpanCategory.CALENDAR, source="calendar", tentative=True)
        self.span("Dentist", DAY, "14:00", category=SpanCategory.CALENDAR, source="calendar")
        d = month_summary(DAY, self.source(), DAY).day(DAY)
        self.assertEqual([(e.title, e.look) for e in d.items], [("Dentist", "calendar")])
        self.assertEqual((d.done, d.planned), (0, 0))  # shown, not counted: the year counts the same way

    def test_after_midnight_is_the_night_of_the_day_before(self):
        self.span("Late film", DAY + timedelta(days=1), "01:00")
        m = month_summary(DAY, self.source(), DAY)
        self.assertEqual([e.title for e in m.day(DAY).items], ["Late film"])
        self.assertEqual(m.day(DAY + timedelta(days=1)).items, [])

    def test_the_grid_fills_the_days_either_side_of_the_month(self):
        self.span("Harvest fair", date(2026, 9, 29), "10:00")
        m = month_summary(DAY, self.source(), DAY)
        first = m.weeks[0].days[1]
        self.assertEqual((first.day, first.in_month), (date(2026, 9, 29), False))
        self.assertEqual([e.title for e in first.items], ["Harvest fair"])
        self.assertEqual(len(m.days), 31)  # only October's own count

    def test_a_pin_to_a_day_shows_until_its_on_the_days_schedule(self):
        friday = DAY + timedelta(days=1)
        self.pin("t_door", "Fix the door", "week", DAY, slot=friday.isoformat())
        self.pin("t_hall", "Paint the hall", "week", DAY, slot=DAY.isoformat())
        self.pin("t_tax", "File the taxes", "week", DAY)  # this week, no day
        self.span("Paint the hall", DAY, "16:00", task_id="t_hall")
        m = month_summary(DAY, self.source(), DAY)
        self.assertEqual([e.title for e in m.day(friday).pinned], ["Fix the door"])
        self.assertEqual(m.day(DAY).pinned, [])  # it's on Thursday's schedule: shown as the item
        week = next(w for w in m.weeks if w.start == WEEK.start)
        self.assertEqual([(e.title, e.status) for e in week.pinned], [("File the taxes", "open")])

    def test_the_month_plans_efforts_with_their_budgets(self):
        self.pin("t_shed", "Shed clear-out", "month", DAY, sessions=4, minutes=90)
        self.pin("t_frame", "Hang the frames", "month", DAY)
        self.span("Shed clear-out", date(2026, 10, 3), status=SpanStatus.DONE)
        self.span("Shed clear-out", date(2026, 10, 10), status=SpanStatus.DONE)
        self.span("Shed clear-out", date(2026, 10, 12), status=SpanStatus.SKIPPED)
        self.span("Shed clear-out", date(2026, 10, 20))
        self.span("Shed clear-out", date(2026, 11, 2), status=SpanStatus.DONE)  # next month's: not this budget's
        m = month_summary(DAY, self.source(), DAY)
        shed, frames = m.efforts
        self.assertEqual((shed.title, shed.sessions, shed.minutes, shed.done, shed.planned, shed.left),
                         ("Shed clear-out", 4, 90, 2, 1, 1))
        self.assertEqual(shed.progress(), "2 of 4 sessions done, 1 planned")
        self.assertEqual(shed.status, "scheduled")
        self.assertEqual((frames.title, frames.budgeted, frames.progress(), frames.status),
                         ("Hang the frames", False, "", "open"))

    def test_done_and_gone(self):
        self.pin("t_frame", "Hang the frames", "month", DAY)
        self.pin("t_old", "Return the drill", "month", DAY)
        m = month_summary(DAY, self.source(on_lists={"t_frame"}, done={"t_frame"}), DAY)
        self.assertEqual([(e.title, e.status) for e in m.efforts],
                         [("Hang the frames", "done"), ("Return the drill", "gone")])


class YearTest(Fixture):
    def test_twelve_months_each_with_its_efforts_and_counts(self):
        self.pin("t_shed", "Shed clear-out", "month", DAY, sessions=3)
        self.pin("t_bulbs", "Plant the bulbs", "month", date(2026, 11, 1))
        self.pin("t_door", "Fix the door", "week", DAY, slot=DAY.isoformat())
        self.span("Shed clear-out", date(2026, 10, 3), status=SpanStatus.DONE)
        self.span("Call the plumber", date(2026, 10, 5), status=SpanStatus.PARTIAL)
        self.span("Book the car in", date(2026, 10, 22))
        y = year_summary(2026, self.source(), DAY)
        self.assertEqual([m.month.start.month for m in y.months], list(range(1, 13)))
        october, november = y.months[9], y.months[10]
        self.assertTrue(october.is_current)
        self.assertTrue(y.months[8].is_past)
        self.assertEqual([(e.title, e.progress()) for e in october.efforts],
                         [("Shed clear-out", "1 of 3 sessions done")])
        self.assertEqual([e.title for e in october.weeks], ["Fix the door"])
        self.assertEqual((october.done, october.planned), (2, 1))
        self.assertEqual([e.title for e in november.efforts], ["Plant the bulbs"])
        self.assertEqual(y.done, 2)

    def test_the_year_plan_as_a_whole_and_by_quarter_with_a_quarters_budget(self):
        self.pin("t_lang", "Learn the guitar chords", "year", DAY)
        self.pin("t_shed", "Shed clear-out", "year", DAY, slot=date(2026, 10, 1).isoformat(), sessions=6)
        self.pin("t_move", "Find a new flat", "years", DAY)
        self.span("Shed clear-out", date(2026, 9, 28), status=SpanStatus.DONE)   # Q3: not the Q4 budget's
        self.span("Shed clear-out", date(2026, 10, 3), status=SpanStatus.DONE)
        self.span("Shed clear-out", date(2026, 12, 5))
        y = year_summary(DAY, self.source(), DAY)
        self.assertEqual([e.title for e in y.efforts], ["Find a new flat", "Learn the guitar chords"])
        q4, shed = y.quarters[3][0], y.quarters[3][1]
        self.assertEqual(q4.start, date(2026, 10, 1))
        self.assertEqual([(e.title, e.where, e.progress()) for e in shed],
                         [("Shed clear-out", q4, "1 of 6 sessions done, 1 planned")])
        self.assertEqual([q[1] for q in y.quarters[:3]], [[], [], []])


class LoadingTest(Fixture):
    def test_loading_a_month_reads_the_calendars_and_sets_no_day_up(self):
        self.sched = make_scheduler([event("dentist", "14:00", "15:00", "Dentist")])
        self.db, self.plans = self.sched.state_db, HorizonPlans(self.sched.state_db)
        self.span("Call the plumber", DAY, "09:00", status=SpanStatus.DONE)
        self.pin("t_shed", "Shed clear-out", "year", DAY, slot=date(2026, 10, 1).isoformat(), sessions=6)
        self.span("Shed clear-out", date(2026, 12, 5))  # in the quarter's budget, past the month's grid
        m = load_month(self.sched, DAY, DAY)
        self.assertEqual([(e.title, e.done) for e in m.day(DAY).items],
                         [("Call the plumber", True), ("Dentist", False)])
        self.assertFalse(self.db.get_day_meta(date(2026, 10, 20)).routines_applied)  # no Work or Sleep set up
        self.assertEqual(self.db.get_spans_between(at("00:00", 3), at("23:00", 3)), [])
        y = load_year(self.sched, 2026, DAY)
        self.assertEqual(y.quarters[3][1][0].progress(), "0 of 6 sessions done, 1 planned")


if __name__ == "__main__":
    unittest.main()
