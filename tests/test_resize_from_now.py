"""An item that hasn't been started, whose planned block has begun, takes a new length from the current block.

The model is asked to stretch such an item. Its old start is in the past and nothing says it began, so a stretch from
that start ran it over the next item and left its start behind. Now the length counts from the current block, what
follows is pushed back, and the result says plainly what moved. An item under way keeps the start it began at, a
start the model passes wins, and something that can't move is reported and left alone.

Fixtures are invented: a bike co-op's repair bench.
"""

import unittest

from tests.fixtures import at
from tests.test_today import TodayTestCase
from thirties_core.astronomy import set_debug_time
from thirties_core.joplin_writeback import current_turn
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

WHEEL, TUBES, GEARS, FORK = "True the front wheel", "Count the spare tubes", "Sort the loose gear cables", \
    "Fit the loaner fork"


class ResizeCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        for title, start, length in ((WHEEL, "10:30 AM", "30 min"), (TUBES, "11:30 AM", "30 min"),
                                     (GEARS, "12:00 PM", "30 min")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("11:24"))  # the wheel's block is long past; nothing says it was started

    def say(self, message, tool, args):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message = message
        token = current_turn.set(f"[app] {message}")
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            current_turn.reset(token)
            self.t.user_message = None

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)


class TestNotStartedResizesFromNow(ResizeCase):

    def test_the_length_counts_from_the_current_block_and_what_follows_moves(self):
        out = self.say(f"{WHEEL} needs four blocks", "update_event", {"event": WHEEL, "duration": "120 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        wheel = self.span(WHEEL)
        self.assertEqual((wheel.start_dt, wheel.end_dt), (at("11:00"), at("13:00")))
        self.assertGreaterEqual(self.span(TUBES).start_dt, wheel.end_dt)
        self.assertGreaterEqual(self.span(GEARS).start_dt, self.span(TUBES).end_dt)
        self.assertIn("hadn't been started", out)
        self.assertIn("11:00 AM – 1:00 PM", out)
        self.assertIn(f"{TUBES} moved to 1:00 PM", out)
        self.assertIn(f"{GEARS} moved to 1:30 PM", out)

    def test_nothing_overlaps_afterwards(self):
        self.say(f"{WHEEL} needs four blocks", "update_event", {"event": WHEEL, "duration": "120 min"})
        spans = sorted((s for s in self.t.day_plan.spans if s.title in (WHEEL, TUBES, GEARS)),
                       key=lambda s: s.start_dt)
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a.end_dt, b.start_dt, (a.title, b.title))

    def test_a_block_that_has_not_begun_keeps_its_start(self):
        out = self.say(f"{GEARS} needs an hour", "update_event", {"event": GEARS, "duration": "60 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(GEARS).start_dt, at("12:00"))
        self.assertNotIn("hadn't been started", out)

    def test_a_start_the_model_gives_wins(self):
        out = self.say(f"{WHEEL} needs four blocks, from 10:30", "update_event",
                       {"event": WHEEL, "start": "10:30 AM", "duration": "120 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(WHEEL).start_dt, at("10:30"))
        self.assertNotIn("hadn't been started", out)

    def test_the_app_own_controls_keep_the_times_they_give(self):
        out = self.t.execute("update_event", {"event": WHEEL, "duration": "120 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(WHEEL).start_dt, at("10:30"))

    def test_items_the_user_said_can_share_time_are_not_pushed(self):
        out = self.say(f"{WHEEL} needs four blocks, alongside the rest", "update_event",
                       {"event": WHEEL, "duration": "120 min", "alongside": True})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(TUBES).start_dt, at("11:30"))


class TestUnderWayKeepsItsStart(ResizeCase):

    def test_an_item_under_way_keeps_the_start_it_began_at(self):
        set_debug_time(at("10:50"))
        out = self.say("on the front wheel truing now", "doing_now", {"item": WHEEL})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("11:20"))
        out = self.say(f"{WHEEL} needs four blocks", "update_event", {"event": WHEEL, "duration": "120 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        wheel = self.span(WHEEL)
        self.assertEqual((wheel.start_dt, wheel.end_dt), (at("10:30"), at("12:30")))
        self.assertNotIn("hadn't been started", out)


class TestWhatCannotMove(ResizeCase):

    def test_a_fixed_follower_is_reported_and_stays(self):
        self.t.execute("add_event", {"title": FORK, "start": "12:00 PM", "duration": "30 min"})
        self.t.execute("update_event", {"event": FORK, "fixed": True})
        before = [(s.title, s.start_dt, s.end_dt) for s in self.t.day_plan.spans]
        out = self.say(f"{WHEEL} needs four blocks", "update_event", {"event": WHEEL, "duration": "120 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn(FORK, out)
        self.assertIn("can't move", out)
        self.assertEqual(before, [(s.title, s.start_dt, s.end_dt) for s in self.t.day_plan.spans])

    def test_a_fixed_item_behind_what_slides_stops_the_slide(self):
        self.t.execute("update_event", {"event": GEARS, "fixed": True})
        before = [(s.title, s.start_dt, s.end_dt) for s in self.t.day_plan.spans]
        out = self.say(f"{WHEEL} needs two blocks", "update_event", {"event": WHEEL, "duration": "60 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)  # the tubes would slide onto the cables, which can't move
        self.assertIn(GEARS, out)
        self.assertEqual(before, [(s.title, s.start_dt, s.end_dt) for s in self.t.day_plan.spans])


class TestTheFactIsGiven(ResizeCase):

    def test_the_schedule_says_the_block_has_begun_and_nothing_says_it_started(self):
        text = self.t.schedule_text()
        line = next(x for x in text.splitlines() if WHEEL in x)
        self.assertIn("planned from 10:30 AM, which has begun; nothing says it started", line)
        self.assertIn(f"next: {TUBES} from 11:30 AM", line)

    def test_an_item_under_way_is_not_flagged(self):
        set_debug_time(at("10:50"))
        self.say("on the front wheel truing now", "doing_now", {"item": WHEEL})
        set_debug_time(at("11:00"))
        line = next(x for x in self.t.schedule_text().splitlines() if WHEEL in x)
        self.assertNotIn("nothing says it started", line)
        self.assertIn("under way", line)

    def test_a_block_ahead_is_not_flagged(self):
        line = next(x for x in self.t.schedule_text().splitlines() if GEARS in x)
        self.assertNotIn("has begun", line)

    def test_a_change_that_leaves_it_begun_says_so(self):
        out = self.say(f"rename {WHEEL}", "update_event", {"event": WHEEL, "title": "True the rear wheel"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertIn("which has begun; nothing says it started", out)


if __name__ == "__main__":
    unittest.main()
