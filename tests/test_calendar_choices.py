"""Calendar choices (O1): "not mine" per recurring series, and mine / show only / ignore per calendar."""

import unittest
from datetime import timedelta

from tests.fixtures import DAY, at, make_scheduler, occupant
from thirties_core.calendar_engine import CalendarEvent, events_to_spans, series_key, series_of
from thirties_core.plan_tools import PlanTools


def partners(eid: str, start: str, end: str, title: str = "Evening pottery", day_offset: int = 0,
          calendar: str = "Jamie") -> CalendarEvent:
    """An occurrence of a recurring event on someone else's shared calendar (no RSVP of the user's)."""
    begin = at(start, day_offset)
    return CalendarEvent(id=f"{eid}@google.com@{int(begin.timestamp())}", summary=title, start_dt=begin,
                         end_dt=at(end, day_offset), calendar_id="uid-jamie", calendar_name=calendar,
                         is_primary=False, is_ambiguous=True, response_status="accepted", series_id=f"{eid}@google.com")


class SeriesTest(unittest.TestCase):
    def test_series_of_an_occurrence_id(self):
        self.assertEqual(series_of("abc@google.com@1760000000"), "abc@google.com")
        self.assertEqual(series_of("abc@1760000000"), "abc")
        self.assertEqual(series_of("plain"), "plain")
        self.assertEqual(series_of("x@google.com"), "x@google.com")  # no occurrence suffix: its own series
        self.assertEqual(series_key(partners("pottery", "19:00", "21:00")), "series:pottery@google.com")

    def test_not_mine_covers_every_occurrence(self):
        today, next_week = partners("pottery", "19:00", "21:00"), partners("pottery", "19:00", "21:00", day_offset=7)
        spans, pending = events_to_spans([today, next_week], {series_key(today): "not_mine"})
        self.assertEqual((spans, pending), ([], []))

    def test_an_occurrence_decision_wins_over_its_series(self):
        today = partners("pottery", "19:00", "21:00")
        spans, pending = events_to_spans([today], {series_key(today): "not_mine", today.id: "attend"})
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].is_firm)
        self.assertEqual(pending, [])

    def test_resolving_someone_elses_event_decides_the_series(self):
        sched = make_scheduler([partners("pottery", "19:00", "21:00"), partners("pottery", "19:00", "21:00", day_offset=7)])
        plan, pending = sched.build_day_plan(DAY)
        self.assertEqual(len(pending), 1)
        tools = PlanTools(plan, pending, sched)
        result = tools.execute("resolve_calendar_event", {"event": "Evening pottery", "attend": False})
        self.assertIn("every time it comes up", result)
        self.assertEqual(tools.ambiguous_events, [])
        later, later_pending = sched.build_day_plan(DAY + timedelta(days=7))
        self.assertEqual(later_pending, [])  # next week's rehearsal isn't asked about again
        self.assertNotIn("Evening pottery", [s.title for s in later.spans])

    def test_resolving_ones_own_invite_is_for_that_time_only(self):
        mine = [CalendarEvent(id=f"standup@{int(at('10:00', d).timestamp())}", summary="Standup",
                              start_dt=at("10:00", d), end_dt=at("10:30", d), is_ambiguous=True,
                              response_status="needsAction", series_id="standup") for d in (0, 7)]
        sched = make_scheduler(mine)
        plan, pending = sched.build_day_plan(DAY)
        tools = PlanTools(plan, pending, sched)
        tools.execute("resolve_calendar_event", {"event": "Standup", "attend": False})
        _, later_pending = sched.build_day_plan(DAY + timedelta(days=7))
        self.assertEqual(len(later_pending), 1)  # declining one standup isn't declining them all
        tools.execute("resolve_calendar_event", {"event": later_pending[0].id, "attend": True, "series": True,
                                                 "day": "Oct 22"})
        third = CalendarEvent(id=f"standup@{int(at('10:00', 14).timestamp())}", summary="Standup",
                              start_dt=at("10:00", 14), end_dt=at("10:30", 14), is_ambiguous=True,
                              response_status="needsAction", series_id="standup")
        spans, pending3 = events_to_spans([third], sched.state_db.get_event_decisions())
        self.assertEqual(pending3, [])
        self.assertTrue(spans[0].is_firm)


class CalendarModesTest(unittest.TestCase):
    def test_ignore_drops_the_calendar(self):
        spans, pending = events_to_spans([partners("pottery", "19:00", "21:00")], modes={"jamie": "ignore"})
        self.assertEqual((spans, pending), ([], []))

    def test_show_only_takes_no_time_and_is_never_asked_about(self):
        sched = make_scheduler([partners("show", "19:00", "21:00", title="School play")])
        sched.config.calendar.modes = {"jamie": "show"}
        plan, pending = sched.build_day_plan(DAY)
        self.assertEqual(pending, [])
        shown = next(s for s in plan.spans_in_view() if s.title == "School play")
        self.assertTrue(shown.shown_only)
        self.assertFalse(shown.is_firm or shown.tentative)
        self.assertIsNone(occupant(plan, "19:00"))  # the time is still free
        tools = PlanTools(plan, pending, sched)
        self.assertNotIn("School play", tools.schedule_text())  # the assistant never brings it up
        tools.execute("add_event", {"title": "Dinner", "start": "7:00 PM", "duration": "1 hour"})
        self.assertEqual(occupant(tools.day_plan, "19:00"), "Dinner")

    def test_mine_means_no_question_unless_the_rsvp_asks(self):
        accepted = partners("studio", "19:00", "21:00", title="Open studio", calendar="Club")
        invited = partners("swap", "21:00", "22:00", title="Book swap", calendar="Club")
        invited.response_status = "needsAction"
        spans, pending = events_to_spans([accepted, invited], modes={"club": "mine"})
        self.assertEqual([e.summary for e in pending], ["Book swap"])
        self.assertTrue(next(s for s in spans if s.title == "Open studio").is_firm)

    def test_modes_are_a_synced_setting(self):
        from thirties_core.config import ThirtiesConfig
        from thirties_core.settings import SETTINGS, Settings
        self.assertEqual(SETTINGS["calendar.modes"].scope, "shared")
        sched = make_scheduler()
        settings = Settings(sched.state_db)
        settings.set("calendar.modes", {"jamie": "show"})
        self.assertEqual(settings.apply(ThirtiesConfig()).calendar.modes, {"jamie": "show"})


class CalendarModeToolTest(unittest.TestCase):
    """Choosing per calendar in chat: exact names only, the same synced setting as Preferences."""

    def setUp(self):
        holiday = CalendarEvent(id="hol@1", summary="Halloween eve", start_dt=at("09:00"), end_dt=at("10:00"),
                                calendar_name="Holidays in United States", is_primary=False, is_ambiguous=True)
        self.sched = make_scheduler([partners("pottery", "19:00", "21:00"), holiday])
        plan, pending = self.sched.build_day_plan(DAY)
        self.tools = PlanTools(plan, pending, self.sched)

    def test_show_only_by_exact_name_sets_the_synced_choice_and_redraws(self):
        self.assertEqual(len(self.tools.ambiguous_events), 2)
        out = self.tools.execute("set_calendar_mode", {"calendar": "jamie", "mode": "show-only"})
        self.assertIn("Jamie: show only", out)
        from thirties_core.settings import Settings
        self.assertEqual(Settings(self.sched.state_db).get("calendar.modes"), {"jamie": "show"})
        self.assertEqual(self.sched.config.calendar.modes, {"jamie": "show"})
        pottery = next(s for s in self.tools.day_plan.spans if s.title == "Evening pottery")
        self.assertTrue(pottery.shown_only)
        self.assertEqual([e.summary for e in self.tools.ambiguous_events], ["Halloween eve"])

    def test_ignore_then_ask_again(self):
        self.tools.execute("set_calendar_mode", {"calendar": "Holidays in United States", "mode": "ignore"})
        self.assertNotIn("Halloween eve", [s.title for s in self.tools.day_plan.spans])
        out = self.tools.execute("set_calendar_mode", {"calendar": "Holidays in United States", "mode": "ask"})
        self.assertIn("ask about its events", out)
        self.assertEqual(self.sched.config.calendar.modes, {})
        self.assertIn("Halloween eve", [e.summary for e in self.tools.ambiguous_events])

    def test_a_near_name_lists_the_calendars_and_changes_nothing(self):
        out = self.tools.execute("set_calendar_mode", {"calendar": "Holidays", "mode": "ignore"})
        self.assertTrue(out.startswith("NOT_DONE"), out)
        self.assertIn("Holidays in United States", out)
        self.assertEqual(self.sched.config.calendar.modes, {})

    def test_listing_and_a_bad_mode(self):
        self.assertIn("Jamie (ask about its events)", self.tools.execute("set_calendar_mode", {}))
        self.assertIn("isn't a choice", self.tools.execute("set_calendar_mode", {"calendar": "Jamie", "mode": "x"}))


if __name__ == "__main__":
    unittest.main()


class TestHolidays(unittest.TestCase):
    """10-30: a federal holiday on a work day almost always means no work that day."""

    def test_a_holiday_on_a_work_day_takes_work_off_once(self):
        from tests.fixtures import DAY, at, make_scheduler
        from thirties_core.calendar_engine import CalendarEvent
        from thirties_core.models import SpanCategory
        day_off = CalendarEvent(id="vets", summary="Veterans Day", start_dt=at("00:00"), end_dt=at("23:59"),
                                calendar_name="Holidays in United States")
        sched = make_scheduler([day_off])
        sched.config.calendar.modes = {"holidays in united states": "holidays"}
        plan, _ = sched.build_day_plan(DAY)
        self.assertFalse([s for s in plan.spans if s.category == SpanCategory.WORK])
        holiday = next(s for s in plan.spans if s.title == "Veterans Day")
        self.assertTrue(holiday.shown_only)
        self.assertEqual(sched.state_db.get_sync_meta(f"holiday:{DAY.isoformat()}"), "Veterans Day")
        from thirties_core.models import EventSpan
        sched.state_db.upsert_span(EventSpan("w", "Work", at("09:00"), at("13:00"), SpanCategory.WORK, source="user"))
        plan, _ = sched.build_day_plan(DAY)  # they said they're working: it stays
        self.assertTrue([s for s in plan.spans if s.category == SpanCategory.WORK])
