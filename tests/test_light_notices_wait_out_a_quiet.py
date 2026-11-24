"""At light and up, the heads-up before a calendar event and the "have you started" question wait out a quiet the
user asked for: each counts as sent only once it went out, so it can still go once the quiet is over.

Invented: an afternoon at a map maker's desk, with a talk at 4:20 PM (tests/test_bell_every_thirty.py)."""

import unittest
from datetime import timedelta

from tests.fixtures import at
from tests.test_bell_every_thirty import Day
from thirties_core import holds
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory


class LightNotices(Day):

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.message_level = "light"
        self.add(EventSpan("ev", "Inlet tide talk", at("16:20"), at("16:50"), SpanCategory.CALENDAR, is_firm=True))

    def every(self, start, end, duty):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            duty(t)
            t += timedelta(minutes=5)

    def kinds(self):
        return [(kind, body.split("\n")[0]) for kind, body in self.out]

    def test_a_heads_up_held_by_a_quiet_goes_out_once_it_ends(self):
        holds.start(self.sched.state_db, at("15:50"), at("16:12"), said="quiet please")
        self.every("16:05", "16:20", self.svc.heads_up)
        self.assertEqual([k for k, _ in self.kinds()], ["heads_up"])
        self.assertTrue(self.kinds()[0][1].startswith("⏰ Inlet tide talk at 4:20 PM, in the 4:00 PM block."), self.out)

    def test_a_heads_up_held_until_the_event_starts_never_goes(self):
        holds.start(self.sched.state_db, at("15:50"), at("16:30"), said="quiet please")
        self.every("16:05", "16:40", self.svc.heads_up)
        self.assertEqual(self.out, [])

    def test_a_have_you_started_question_held_by_a_quiet_goes_out_once_it_ends(self):
        from thirties_core.contextual import ContextualFlags
        ContextualFlags(self.sched.state_db).set("co")  # "ask me if I've started plotting the coastline"
        holds.start(self.sched.state_db, at("15:10"), at("15:25"), said="quiet please")
        self.every("15:20", "15:30", self.svc.contextual)
        self.assertEqual([k for k, _ in self.kinds()], ["contextual"])
        self.assertTrue(self.kinds()[0][1].startswith("👀") and "Plot the coastline" in self.kinds()[0][1], self.out)


if __name__ == "__main__":
    unittest.main()
