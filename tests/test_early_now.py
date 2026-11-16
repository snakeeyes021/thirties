"""Something under way at this moment can always be recorded, even before the day's plan begins.

The day's plan in these tests starts with the 7:00 AM block (sunrise). Up at 6:35 and already out feeding the
hens, the user said so; doing_now refused it as not "now, today", so nothing was recorded. Being up and busy before
sunrise is ordinary: it's recorded on today in the block it's happening in, started now, the plan's first block
stays at 7:00, and the day's record lists it.
"""

import unittest

from tests.fixtures import DAY, at
from tests.test_routines import RoutineCase
from thirties_core.astronomy import set_debug_time
from thirties_core.day_edges import says_doing_now
from thirties_core.messages import whole_day_lines
from thirties_core.models import SpanStatus
from thirties_core.plan_tools import NOT_DONE


class WordingTest(unittest.TestCase):
    def test_words_that_say_its_happening_now(self):
        for said in ("i'm feeding the hens now", "out on a run right now, back soon", "currently sorting the mail",
                     "fixing the gate now", "we're packing the car at the moment"):
            self.assertTrue(says_doing_now(said), said)
        for said in ("run is at 6:15, now", "put the run at 6:30", "fed the hens at 6:30", "is the run still on?",
                     "", None, "now move the run to 8"):
            self.assertFalse(says_doing_now(said), said)


class EarlyCase(RoutineCase):
    def setUp(self):
        super().setUp()
        set_debug_time(at("06:20"))
        out = self.t.execute("add_event", {"title": "Feed the hens", "start": "7:00 AM", "duration": "30 minutes"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.hens = self.spans(self.t, "Feed the hens")[0]
        self.day_start = self.t.day_plan.day_start
        self.assertEqual(self.day_start, at("07:00"))

    def said(self, message, tool, args):
        self.t.user_message = message
        try:
            return self.t.execute(tool, args)
        finally:
            self.t.user_message = None

    def record(self, tools=None):
        return "\n".join(whole_day_lines((tools or self.t).day_plan))

    def assert_on_record(self, title, block, tools=None):
        tools = tools or self.t
        line = next((l for l in whole_day_lines(tools.day_plan) if title in l), None)
        self.assertIsNotNone(line, self.record(tools))
        self.assertTrue(line.startswith(block), line)
        self.assertEqual(tools.day_plan.day_start, at("07:00"))  # the plan's first block never moves


class TestDoingNowBeforeThePlan(EarlyCase):
    def test_at_6_35_an_item_planned_at_7_is_recorded_now(self):
        set_debug_time(at("06:35"))
        out = self.said("i'm feeding the hens now", "doing_now", {"item": self.hens.id})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("not done yet", out)
        self.assertEqual((self.hens.start_dt, self.hens.status, self.hens.actual_start),
                         (at("06:30"), SpanStatus.PLANNED, at("06:35")))
        self.assert_on_record("Feed the hens", "6:30 AM")
        self.assertIn("Feed the hens", self.t.execute("show_schedule", {}))
        later = self.tools_for(DAY)  # the day's plan built again keeps it
        self.assert_on_record("Feed the hens", "6:30 AM", later)
        self.assertEqual([s.actual_start for s in self.spans(later, "Feed the hens")], [at("06:35")])

    def test_at_5_50_something_new_is_recorded_in_its_block(self):
        set_debug_time(at("05:50"))
        out = self.said("out on a run right now", "doing_now", {"item": "Morning run", "until": "6:20 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        run = self.spans(self.t, "Morning run")[0]
        self.assertEqual((run.start_dt, run.end_dt, run.actual_start), (at("05:30"), at("06:20"), at("05:50")))
        self.assert_on_record("Morning run", "5:30 AM")
        self.assertEqual(self.hens.start_dt, at("07:00"))  # what's planned in the day stays
        self.assert_on_record("Morning run", "5:30 AM", self.tools_for(DAY))

    def test_marked_done_afterwards_it_stays_on_the_record(self):
        set_debug_time(at("06:35"))
        self.said("i'm feeding the hens now", "doing_now", {"item": self.hens.id})
        set_debug_time(at("06:58"))
        out = self.said("done with the hens", "log_outcome", {"event": self.hens.id, "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        later = self.tools_for(DAY)
        self.assertEqual([s.status for s in self.spans(later, "Feed the hens")], [SpanStatus.DONE])
        self.assert_on_record("Feed the hens", "6:30 AM", later)


class TestNeverRefusedForBeingEarly(EarlyCase):
    def test_adding_what_theyre_doing_right_now_at_an_early_hour(self):
        for clock, said, title, block in (("05:50", "5:50 AM", "Sweep the porch", "5:30 AM"),
                                          ("06:35", "6:35 AM", "Wipe the windows", "6:30 AM")):
            set_debug_time(at(clock))
            out = self.said(f"{title.lower()}, doing it right now", "add_event",
                            {"title": title, "start": said, "duration": "20 minutes"})
            self.assertFalse(out.startswith(NOT_DONE), out)
            span = self.spans(self.t, title)[0]
            self.assertEqual((span.start_dt, span.actual_start), (at(block.split()[0].zfill(5)), at(clock)))
            self.assert_on_record(title, block)

    def test_moving_an_item_to_now_at_an_early_hour(self):
        set_debug_time(at("05:50"))
        out = self.said("i'm feeding the hens now, they were loud", "update_event",
                        {"event": self.hens.id, "start": "5:50 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual((self.hens.start_dt, self.hens.actual_start), (at("05:30"), at("05:50")))
        self.assert_on_record("Feed the hens", "5:30 AM")

    def test_an_early_time_that_isnt_now_is_still_refused(self):
        set_debug_time(at("06:35"))
        out = self.said("feed the hens at 6:00", "update_event", {"event": self.hens.id, "start": "6:00 AM"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertEqual(self.hens.start_dt, at("07:00"))
        out = self.said("put the hens at 6:30 instead", "update_event", {"event": self.hens.id, "start": "6:30 AM"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # no word that it's happening now


if __name__ == "__main__":
    unittest.main()
