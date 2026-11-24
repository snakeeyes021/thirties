"""Saying a calendar event is happening now changes nothing: it's already on the plan.

doing_now, given a calendar event, used to lose the event and make a fresh entry named by its raw id, and that
entry then pushed others aside. The event already sits on the plan, so doing_now leaves the plan alone and says
so plainly. A raw calendar id is never a title, by whichever path it comes.
Invented fixtures: a marionette maker who also flies a falcon.
"""

from tests.fixtures import at
from tests.test_if_time import Base
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE
from thirties_core.titles import is_raw_id

MEWS = "Falconry at the mews"
LIMBS = "Gesso the marionette limbs"


class CalendarNowBase(Base):
    def setUp(self):
        super().setUp()
        out = self.t.execute("add_event", {"title": LIMBS, "start": "11:10 AM", "duration": "40 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.t.day_plan.spans.append(EventSpan("cal:mews-1", MEWS, at("10:10"), at("10:50"), SpanCategory.CALENDAR,
                                               source="calendar", source_event_id="mews-1", is_firm=True))
        self.t.day_plan.spans.sort(key=lambda s: s.start_dt)
        set_debug_time(at("10:20"))

    def layout(self):
        return sorted((s.id, s.title, s.start_dt, s.end_dt, s.status) for s in self.t.day_plan.spans)

    def assert_no_raw_titles(self):
        titles = [s.title for s in self.t.day_plan.spans]
        self.assertFalse([t for t in titles if is_raw_id(t) or t.startswith("cal:")], titles)


class TestDoingNowOnACalendarEvent(CalendarNowBase):
    def test_by_its_id_nothing_is_added_or_moved(self):
        before = self.layout()
        out = self.say("at the mews with the falcon now", "doing_now", {"item": "cal:mews-1"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn(MEWS, out)
        self.assertIn("calendar event", out)
        self.assertNotIn("cal:", out)
        self.assertEqual(self.layout(), before)
        self.assert_no_raw_titles()

    def test_by_its_words_nothing_is_added_or_moved(self):
        before = self.layout()
        out = self.say("at the mews with the falcon now", "doing_now", {"item": MEWS})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.layout(), before)
        self.assertEqual(len([s for s in self.t.day_plan.spans if s.title == MEWS]), 1)


class TestNoRawCalendarIdAsATitle(CalendarNowBase):
    def test_a_calendar_id_reads_as_a_raw_id(self):
        self.assertTrue(is_raw_id("cal:mews-1"))
        self.assertFalse(is_raw_id(MEWS))

    def test_doing_now_on_a_calendar_id_not_on_the_plan_adds_nothing(self):
        before = self.layout()
        out = self.say("at the mews now", "doing_now", {"item": "cal:mews-9"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.layout(), before)
        self.assert_no_raw_titles()

    def test_adding_or_recording_under_a_calendar_id_is_refused(self):
        before = self.layout()
        for args in ({"title": "cal:mews-9", "start": "1:10 PM", "duration": "20 min"},
                     {"title": "cal:mews-9", "start": "9:10 AM", "duration": "20 min", "happened": True}):
            out = self.say("put the mews on", "add_event", args)
            self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.layout(), before)
        self.assert_no_raw_titles()


class TestDoingNowOnAnItemStillMovesIt(CalendarNowBase):
    def test_a_planned_item_starts_now(self):
        out = self.say("gessoing the marionette limbs now", "doing_now", {"item": LIMBS})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("not done yet", out)
        limbs = self.span(LIMBS)
        self.assertEqual((limbs.status, limbs.actual_start), (SpanStatus.PLANNED, at("10:20")))
        self.assertLessEqual(limbs.start_dt, at("10:20"))
        self.assertEqual(limbs.end_dt - limbs.start_dt, at("11:50") - at("11:10"))
        self.assert_no_raw_titles()
