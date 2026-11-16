"""The whole day as a record, by blocks: one plain line per thirty something starts in, with its block and the end
of the last block it reaches, things in that thirty on one line, no bullets, and every time with its AM/PM (in the
schedule, what's next and the notices alike)."""

from __future__ import annotations

import re
import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

_TIME = re.compile(r"\b\d{1,2}:\d{2}\b(?!\s?(?:AM|PM))")


def bare_times(text: str) -> list:
    """Clock times said without their AM/PM."""
    return _TIME.findall(text)


class RecordCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("19:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        for sid, title, start, end, status in (
                ("fern", "Mist the ferns", "09:00", "09:15", SpanStatus.DONE),
                ("kite", "Sketch a kite", "09:15", "09:30", SpanStatus.PLANNED),
                ("shelf", "Shelf build", "15:00", "18:30", SpanStatus.DONE),  # a long item
                ("pod", "Radio play", "15:00", "16:00", SpanStatus.PLANNED),  # at the same time
                ("soup", "Supper", "19:00", "19:45", SpanStatus.PLANNED),
                ("read", "Read", "21:00", "21:30", SpanStatus.PLANNED)):
            self.sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY,
                                                      status=status))
        plan, pending = self.sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        self.manager = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine,
                                           active_notes=[])

    def turn(self, *replies: str, said: str = "ok") -> str:
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.manager.send_user_message(said)


class TestWholeDayLines(RecordCase):

    def test_one_line_per_item_with_its_start_and_end(self) -> None:
        from thirties_core.messages import whole_day_lines
        lines = whole_day_lines(self.manager.day_plan)
        self.assertIn("9:00 AM – 9:30 AM Mist the ferns (done), Sketch a kite", lines)  # one thirty, one line
        self.assertEqual([line for line in lines if "Shelf build" in line],
                         ["3:00 PM – 6:30 PM Shelf build (done) + Radio play (until 4:00 PM)"])  # once, with its pair
        self.assertIn("9:00 PM – 9:30 PM Read", lines)

    def test_no_bullets_no_empty_time_lines_and_every_time_with_am_pm(self) -> None:
        from thirties_core.messages import render_schedule
        text = render_schedule(self.manager.day_plan, at("19:10"), whole_day=True)
        for line in text.splitlines():
            self.assertFalse(line.startswith(("- ", " ", "\t")), line)
            self.assertNotRegex(line, r"^\d{1,2}:\d{2} [AP]M –\s*$")  # a time with nothing after it
        self.assertEqual(bare_times(text), [])

    def test_the_reply_keeps_the_record_as_laid_out(self) -> None:
        out = self.turn('CALL show_schedule {"whole_day": true}', "Here's your whole day.",
                        said="show me the whole day")
        self.assertIn("3:00 PM – 6:30 PM Shelf build (done) + Radio play (until 4:00 PM)", out.splitlines())
        self.assertIn("9:00 AM – 9:30 AM Mist the ferns (done), Sketch a kite", out.splitlines())  # done stays
        self.assertEqual(bare_times(out), [])
        self.assertFalse(any(line.startswith("- ") for line in out.splitlines()))

    def test_another_days_whole_day_is_laid_out_the_same_way(self) -> None:
        from thirties_core.messages import render_schedule
        text = render_schedule(self.manager.day_plan, None, whole_day=True)
        self.assertIn("3:00 PM – 6:30 PM Shelf build (done) + Radio play (until 4:00 PM)", text.splitlines())


class TestEveryTimeWithAmPm(RecordCase):

    def test_a_schedule_reply_keeps_am_pm_on_every_time(self) -> None:
        out = self.turn("CALL show_schedule {}", "Here's the rest of today.", said="what's left today?")
        self.assertIn("9:00 PM – Read", out.splitlines())
        self.assertEqual(bare_times(out), [])

    def test_the_models_own_times_keep_their_am_pm(self) -> None:
        out = self.turn("Supper is at 7:00 PM and reading at 9:00 PM.", said="when's supper?")
        self.assertIn("7:00 PM", out)
        self.assertIn("9:00 PM", out)

    def test_whats_next_and_its_free_lines(self) -> None:
        from thirties_core.whats_next import render
        text = render(self.manager.day_plan, at("19:10"), mode="rest")
        self.assertIn("Free 9:30 PM – 11:00 PM", text.splitlines())
        self.assertEqual(bare_times(text), [])


if __name__ == "__main__":
    unittest.main()
