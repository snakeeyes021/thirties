"""A calendar that can't be read never makes the day look free: it keeps what it showed at its last good read, or
it's named on the plan, in what the assistant reads, in the morning's facts and on the Agenda. Cancelled events and
events marked free take no time. Invented calendars and events."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler
from thirties_core.calendar_engine import CalendarEvent, EDSCalendarEngine, MockCalendarEngine, unread_text
from thirties_core.plan_tools import PlanTools


class FlakyCalendars(MockCalendarEngine):
    """A calendar source where some calendars (by id), or all of them, can fail to read."""

    def __init__(self, events):
        super().__init__(events)
        self.calendars = {"cal_classes": "Classes", "cal_choir": "Choir"}
        self.failing = {}  # calendar id -> name
        self.down = False
        self.unread, self.read = [], []

    def fetch_events_for_day(self, target_date, tz):
        if self.down:
            raise OSError("calendar service not running")
        self.unread = list(self.failing.items())
        self.read = [c for c in self.calendars if c not in self.failing]
        return [e for e in super().fetch_events_for_day(target_date, tz) if e.calendar_id not in self.failing]


def lesson(title="Pottery class", start="18:00", end="19:30", cal="cal_classes", name="Classes"):
    return CalendarEvent(id=f"{title}@1", summary=title, start_dt=at(start), end_dt=at(end), calendar_id=cal,
                         calendar_name=name)


class TestUnreadCalendars(unittest.TestCase):

    def setUp(self):
        self.sched = make_scheduler()
        self.cals = FlakyCalendars([lesson(), lesson("Choir practice", "20:00", "21:00", "cal_choir", "Choir")])
        self.sched.calendar_engine = self.cals

    def titles(self, plan):
        return sorted(s.title for s in plan.spans if s.source == "calendar")

    def test_a_calendar_that_fails_later_keeps_its_last_read(self):
        self.sched.build_day_plan(DAY)
        self.cals.failing = {"cal_classes": "Classes"}
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(self.titles(plan), ["Choir practice", "Pottery class"])
        self.assertEqual(plan.calendar_unread, [])

    def test_a_calendar_never_read_is_named(self):
        self.cals.failing = {"cal_classes": "Classes"}
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(self.titles(plan), ["Choir practice"])
        self.assertEqual(plan.calendar_unread, ["Classes"])

    def test_no_calendars_at_all_is_named_too(self):
        self.cals.down = True
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(plan.calendar_unread, [""])
        self.assertEqual(unread_text(plan.calendar_unread), "Couldn't read your calendars, so events may be missing.")

    def test_an_outage_after_a_good_read_keeps_the_day(self):
        self.sched.build_day_plan(DAY)
        self.cals.down = True
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(self.titles(plan), ["Choir practice", "Pottery class"])
        self.assertEqual(plan.calendar_unread, [])

    def test_a_good_read_replaces_the_last_one(self):
        """An event deleted from a calendar that reads fine is gone, not kept from before."""
        self.sched.build_day_plan(DAY)
        self.cals.clear()
        self.cals.add_event(lesson("Choir practice", "20:00", "21:00", "cal_choir", "Choir"))
        self.sched.build_day_plan(DAY)
        self.cals.failing = {"cal_choir": "Choir"}
        plan, _ = self.sched.build_day_plan(DAY)
        self.assertEqual(self.titles(plan), ["Choir practice"])

    def test_the_assistant_is_told_the_free_time_may_not_be_free(self):
        self.cals.failing = {"cal_classes": "Classes", "cal_choir": "Choir"}
        plan, pending = self.sched.build_day_plan(DAY)
        text = PlanTools(plan, pending, self.sched).schedule_text()
        self.assertTrue(text.startswith("Couldn't read the calendars Classes and Choir, so their events may be "
                                        "missing. Free time below may not be free."), text)

    def test_the_morning_says_so(self):
        from thirties_core.conversation import ConversationManager
        from thirties_core.inference import MockInferenceEngine
        from thirties_core.morning import prepare
        self.cals.failing = {"cal_classes": "Classes"}
        plan, pending = self.sched.build_day_plan(DAY)
        manager = ConversationManager(plan, [], pending, self.sched, inference_engine=MockInferenceEngine(),
                                      active_notes=[])
        facts, _ = prepare(manager)
        self.assertIn("Couldn't read the calendar Classes, so its events may be missing.", facts)

    def test_nothing_is_said_when_every_calendar_reads(self):
        plan, pending = self.sched.build_day_plan(DAY)
        self.assertEqual(plan.calendar_unread, [])
        self.assertTrue(PlanTools(plan, pending, self.sched).schedule_text().startswith("Scheduled:"))

    def test_the_two_dates_of_a_day_are_read_once_each(self):
        seen = []
        fetch = self.cals.fetch_events_for_day
        self.cals.fetch_events_for_day = lambda d, tz: seen.append(d) or fetch(d, tz)
        self.sched.build_day_plan(DAY)
        self.assertEqual(seen, [DAY, DAY + timedelta(days=1)])


class TestEventsThatTakeNoTime(unittest.TestCase):
    """Cancelled events and events marked free, on real iCal components (no calendar server needed)."""

    def setUp(self):
        try:
            import gi
            gi.require_version("ICalGLib", "3.0")
            from gi.repository import ICalGLib
        except Exception:
            self.skipTest("ICalGLib not available")
        self.ical = ICalGLib

    def comp(self, extra=""):
        return self.ical.Component.new_from_string(
            "BEGIN:VEVENT\nUID:k1\nSUMMARY:Kayak rental\nDTSTART:20261015T150000Z\nDTEND:20261015T170000Z\n"
            + extra + "END:VEVENT\n")

    def test_cancelled_and_free_events_take_no_time(self):
        self.assertTrue(EDSCalendarEngine._occupies(self.comp()))
        self.assertTrue(EDSCalendarEngine._occupies(self.comp("TRANSP:OPAQUE\nSTATUS:CONFIRMED\n")))
        self.assertFalse(EDSCalendarEngine._occupies(self.comp("STATUS:CANCELLED\n")))
        self.assertFalse(EDSCalendarEngine._occupies(self.comp("TRANSP:TRANSPARENT\n")))


if __name__ == "__main__":
    unittest.main()
