"""An item under way is shown in its block; when it really began is recorded, read by the model, never shown.

The user is shown blocks (AGENTS.md §7, "Natural units"). Someone says they're at something partway into its
block: the item is in that block on every view (the status line, Now, the whole day, the phone's message, a reply),
and the minute it began is tracking data. The model reads that minute, marked "(recorded; show the block, not the
minute)", and a reply that copies it anyway says the block.

The user can correct when it began ("it kicked off at 10:44"): the recorded start changes, and if it falls in
another block the item moves to that block. Without such words its start stays.

"line", "row", "entry", "slot", "listing", "block", "schedule" beside an item's words name the item, but a
complaint about its row still isn't a done.

Fixtures are invented: a potter who paddles a canoe on days off.
"""

import re
import unittest

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_today import TodayTestCase, item
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.joplin_writeback import current_turn
from thirties_core.models import SpanStatus
from thirties_core.plan_log import model_call
from thirties_core.plan_tools import NOT_DONE

TEAPOT, SEAT = "Throw the teapot", "Varnish the canoe seat"
MARK = "(recorded; show the block, not the minute)"
_ANY_TIME = re.compile(r"(?<![\d:])(1[0-2]|0?[1-9]):([0-5]\d)(?![\d:])")


class UnderWayCase(TodayTestCase):

    def setUp(self):
        super().setUp()
        self.api.notes["n_errands"]["body"] = f"- [ ] {TEAPOT}\n- [ ] {SEAT}\n"
        self.services.tasks[:] = [item("t_teapot", TEAPOT), item("t_seat", SEAT)]
        for t in self.services.tasks:
            t.list_role = "list"
        for title, start, length, tid in ((TEAPOT, "10:30 AM", "30 min", "t_teapot"),
                                          (SEAT, "11:30 AM", "40 min", "t_seat")):
            out = self.t.execute("add_event", {"title": title, "start": start, "duration": length, "task_id": tid})
            self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("10:50"))
        self.started = self.say("on the wheel with the teapot", "doing_now", {"item": TEAPOT})
        self.assertFalse(self.started.startswith(NOT_DONE), self.started)

    def say(self, message, tool, args, prior=None):
        """The model's own call of ``tool`` for the user's ``message``."""
        self.t.user_message, self.t.prior_exchange = message, prior
        token = current_turn.set(f"[app] {message}")
        try:
            with model_call():
                return self.t.execute(tool, args)
        finally:
            current_turn.reset(token)
            self.t.user_message, self.t.prior_exchange = None, None

    def span(self, title):
        return next(s for s in self.t.day_plan.spans if s.title == title)

    def lines_with(self, text, title=TEAPOT):
        return [line for line in text.splitlines() if title in line]

    def assertBlocksOnly(self, text, *minutes):
        """No minute it began, no "since", and only block edges on the item's lines."""
        for minute in minutes:
            self.assertNotIn(minute, text)
        self.assertNotIn("since", text)
        self.assertNotIn("recorded", text)
        for line in self.lines_with(text):
            odd = [m.group(0) for m in _ANY_TIME.finditer(line) if m.group(2) not in ("00", "30")]
            self.assertEqual(odd, [], line)


class TestTheUserSeesTheBlock(UnderWayCase):

    def test_the_status_line(self):
        from thirties_core.messages import render_schedule, status_line
        set_debug_time(at("10:55"))
        status = status_line(self.t.day_plan, at("10:55"))
        self.assertTrue(status.startswith("You're in the 10:30 AM block: Throw the teapot"), status)
        self.assertBlocksOnly(status, "10:50")
        self.assertBlocksOnly(render_schedule(self.t.day_plan, at("10:55")), "10:50")

    def test_the_now_row_and_the_whole_day(self):
        from thirties_core.messages import whole_day_lines
        set_debug_time(at("10:55"))
        lines = whole_day_lines(self.t.day_plan, now=at("10:55"))
        now_row = [line for line in lines if line.startswith("Now:")]
        self.assertEqual(len(now_row), 1, lines)
        self.assertIn(TEAPOT, now_row[0])
        self.assertBlocksOnly("\n".join(lines), "10:50")
        record = self.lines_with("\n".join(whole_day_lines(self.t.day_plan)))
        self.assertTrue(record and all(line.startswith("10:30 AM") for line in record), record)

    def test_the_phone_message(self):
        from thirties_core.whats_next import summary
        set_debug_time(at("10:55"))
        text = summary(self.t, at("10:55"), mode="rest")
        self.assertIn(TEAPOT, text)
        self.assertBlocksOnly(text, "10:50")

    def test_still_going_after_its_block(self):
        from thirties_core.messages import whole_day_lines
        from thirties_core.whats_next import summary
        set_debug_time(at("11:40"))
        rows = self.lines_with("\n".join(whole_day_lines(self.t.day_plan, now=at("11:40"))))
        self.assertTrue(rows)
        self.assertTrue(all(row.startswith("10:30 AM") for row in rows), rows)
        text = summary(self.t, at("11:40"), mode="rest")  # not marked yet: its title, never a time
        self.assertIn("Not marked yet:\nThrow the teapot", text)
        self.assertBlocksOnly(text, "10:50")


class TestTheModelReadsTheRecord(UnderWayCase):

    def test_the_doing_now_result(self):
        self.assertIn(f"Throw the teapot started at 10:50 AM {MARK}", self.started)
        self.assertIn("on the plan 10:30 AM – 11:00 AM", self.started)

    def test_the_schedule_the_model_reads_and_the_item(self):
        lines = self.lines_with(self.t.schedule_text())
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("- 10:30 AM – 11:00 AM: Throw the teapot"), lines)
        self.assertIn(f"under way, began 10:50 AM {MARK}", lines[0])
        self.assertIn(f"began 10:50 AM {MARK}", self.t.describe(self.span(TEAPOT)))

    def test_an_item_with_no_start_said_has_none_recorded(self):
        set_debug_time(at("11:35"))
        out = self.say("still brushing varnish on that seat", "still_doing", {"event": SEAT})
        self.assertFalse(out.startswith(NOT_DONE), out)
        lines = self.lines_with(self.t.schedule_text(), SEAT)
        self.assertTrue(lines)
        self.assertFalse(any("recorded" in line or "began" in line for line in lines), lines)

    def test_a_reply_that_copies_the_minute_says_the_block(self):
        from thirties_core.clock import real_times, to_thirties
        from thirties_core.internal_text import strip_internal
        from thirties_core.under_way import unsaid
        spans = self.t.day_plan.spans
        copied = f"The teapot began at 10:50 AM {MARK}; the canoe seat is in the 11:30 AM block."
        for said in ("on the wheel with the teapot", "the teapot kicked off at 10:50"):  # said by the user too
            keep, now = unsaid(real_times(said, spans=spans), real_times(now=at("10:52")), spans, said)
            out = to_thirties(strip_internal(copied), keep, now=now)
            self.assertEqual(out, "The teapot began in the 10:30 AM block; the canoe seat is in the 11:30 AM block.")
        keep, now = unsaid(real_times(spans=spans), real_times(now=at("10:52")), spans, "what time is it?")
        self.assertEqual(to_thirties("It's 10:50 AM.", keep, now=now), "It's 10:50 AM.")  # the time, when asked


class TestTheReplyInAConversation(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("10:20"))
        self.addCleanup(set_debug_time, None)
        self.engine = MockInferenceEngine()
        plan, pending = make_scheduler().build_day_plan(DAY)
        self.manager = ConversationManager(plan, [], pending, make_scheduler(), inference_engine=self.engine,
                                           active_notes=[])
        out = self.manager.execute_tool("add_event", {"title": TEAPOT, "start": "10:30 AM", "duration": "30 min"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        set_debug_time(at("10:50"))

    def teapot(self):
        return next(s for s in self.manager.day_plan.spans if s.title == TEAPOT)

    def test_the_reply_says_the_block(self):
        self.engine.set_next_response(reply(f'CALL doing_now {{"item": "{TEAPOT}"}}'))
        self.engine.set_next_response(reply(f"On it: the teapot started at 10:50 AM {MARK}."))
        out = self.manager.send_user_message("on the wheel with the teapot")
        self.assertEqual(self.teapot().actual_start, at("10:50"))
        self.assertNotIn("10:50", out)
        self.assertNotIn("recorded", out)
        self.assertIn("10:30", out)

    def test_a_corrected_start_isnt_said_back(self):
        self.engine.set_next_response(reply(f'CALL doing_now {{"item": "{TEAPOT}"}}'))
        self.engine.set_next_response(reply("On it."))
        self.manager.send_user_message("on the wheel with the teapot")
        set_debug_time(at("10:56"))
        self.engine.set_next_response(reply(f'CALL update_event {{"event": "{TEAPOT}", "start": "10:44 AM"}}'))
        self.engine.set_next_response(reply("Fixed: the teapot began at 10:44 AM."))
        out = self.manager.send_user_message("the teapot kicked off at 10:44, please change it")
        self.assertEqual(self.teapot().actual_start, at("10:44"))
        self.assertNotIn("10:44", out)
        self.assertEqual(self.teapot().start_dt, at("10:30"))
        self.assertIn("began in the 10:30", out)


class TestTheStartTheUserCorrects(UnderWayCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("10:56"))

    def assertShownIn(self, block, *minutes):
        from thirties_core.messages import render_schedule, whole_day_lines
        from thirties_core.whats_next import summary
        record = self.lines_with("\n".join(whole_day_lines(self.t.day_plan)))
        self.assertTrue(record and all(line.startswith(block) for line in record), record)
        self.assertBlocksOnly(render_schedule(self.t.day_plan, self.t._now()), *minutes)
        self.assertBlocksOnly(summary(self.t, self.t._now(), mode="rest"), *minutes)
        self.assertIsNone(self.span(TEAPOT).given_start)

    def test_a_start_given_as_now(self):
        out = self.say("correction for the teapot: it got going a moment ago", "update_event",
                       {"event": TEAPOT, "start": "10:56 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        teapot = self.span(TEAPOT)
        self.assertEqual((teapot.actual_start, teapot.start_dt, teapot.end_dt), (at("10:56"), at("10:30"), at("11:00")))
        self.assertIn(f"began 10:56 AM {MARK}", out)
        self.assertIn(f"began 10:56 AM {MARK}", self.t.schedule_text())
        self.assertShownIn("10:30 AM", "10:56", "10:50")

    def test_a_start_given_as_a_clock_time(self):
        out = self.say("the teapot kicked off at 10:44, please change it", "update_event",
                       {"event": TEAPOT, "start": "10:44 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual((self.span(TEAPOT).actual_start, self.span(TEAPOT).start_dt), (at("10:44"), at("10:30")))
        self.assertShownIn("10:30 AM", "10:44", "10:50")

    def test_a_start_in_another_block_moves_it_there(self):
        set_debug_time(at("11:08"))
        out = self.say("the teapot actually got going at 11:02, not before", "update_event",
                       {"event": TEAPOT, "start": "11:02 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        teapot = self.span(TEAPOT)
        self.assertEqual((teapot.actual_start, teapot.start_dt, teapot.end_dt), (at("11:02"), at("11:00"), at("11:30")))
        self.assertIn("now in the 11:00 AM block", out)
        self.assertTrue(self.lines_with(self.t.schedule_text())[0].startswith("- 11:00 AM – 11:30 AM"))
        self.assertShownIn("11:00 AM", "11:02", "10:50")

    def test_an_earlier_start_in_another_block(self):
        out = self.say("the teapot started at 10:25 really, change the start", "update_event",
                       {"event": TEAPOT, "start": "10:25 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        teapot = self.span(TEAPOT)
        self.assertEqual((teapot.actual_start, teapot.start_dt, teapot.end_dt), (at("10:25"), at("10:00"), at("10:30")))
        self.assertShownIn("10:00 AM", "10:25", "10:50")

    def test_a_wrong_start_said_outright(self):
        out = self.say("the teapot start shown is wrong, I began right now", "update_event",
                       {"event": TEAPOT, "start": "10:56 AM"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(TEAPOT).actual_start, at("10:56"))

    def test_without_words_about_its_start_it_stays(self):
        out = self.say("the teapot will need another twenty minutes or so", "update_event",
                       {"event": TEAPOT, "start": "10:56 AM", "duration": "20 min"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertIn("under way", out)
        self.assertEqual(self.span(TEAPOT).actual_start, at("10:50"))

    def test_a_later_start_is_a_move_not_a_correction(self):
        out = self.say("the teapot started just now? push it to 2:10 instead", "update_event",
                       {"event": TEAPOT, "start": "2:10 PM"})
        teapot = self.span(TEAPOT)
        if out.startswith(NOT_DONE):
            self.assertEqual(teapot.actual_start, at("10:50"))
        else:  # moved where the user said: not under way any more
            self.assertEqual(teapot.start_dt, at("14:10"))
            self.assertIsNone(teapot.actual_start)


class TestStartWords(unittest.TestCase):

    def test_words_that_correct_a_start(self):
        from thirties_core.under_way import START_ASK_RE
        for said in ("the kiln load began at 9:25", "the sanding just began", "its start is wrong",
                     "the glaze run kicked off a minute ago", "not 10:30, a bit after", "change the start please",
                     "the wedging started right when you messaged"):
            self.assertTrue(START_ASK_RE.search(said), said)
        for said in ("the teapot needs another twenty minutes", "it's not 2 hours, more like 3",
                     "it's now 11:05 and the clay is drying", "how long will the varnish take?",
                     "add a stop at the boatyard"):
            self.assertFalse(START_ASK_RE.search(said), said)


class TestDisplayWordsNameTheItem(UnderWayCase):

    def setUp(self):
        super().setUp()
        set_debug_time(at("11:40"))

    def test_the_words_with_a_display_noun_fit_the_item(self):
        from thirties_core.done_fit import names_something_else
        for said in ("the teapot row is finished", "teapot entry wrapped up", "the teapot slot is complete",
                     "that teapot listing can go, it's finished", "teapot block is done", "the teapot line is done",
                     "the teapot on the schedule is done", "teapot rows are finished"):
            self.assertEqual(names_something_else(said, [TEAPOT], [SEAT]), [], said)

    def test_a_noun_of_its_own_still_names_something_else(self):
        from thirties_core.done_fit import names_something_else
        self.assertTrue(names_something_else("the teapot lid is finished", [TEAPOT], [SEAT]))

    def test_a_done_said_about_its_row_ticks_it(self):
        out = self.say("the teapot row can come off, it's finished", "log_outcome",
                       {"event": self.span(TEAPOT).id, "status": "done"})
        self.assertFalse(out.startswith(NOT_DONE), out)
        self.assertEqual(self.span(TEAPOT).status, SpanStatus.DONE)

    def test_a_complaint_about_its_row_is_not_a_done(self):
        out = self.say("the teapot row has the wrong start on it, it began later than that", "log_outcome",
                       {"event": self.span(TEAPOT).id, "status": "done"})
        self.assertTrue(out.startswith(NOT_DONE), out)
        self.assertNotIn("name something other", out)
        self.assertEqual(self.span(TEAPOT).status, SpanStatus.PLANNED)
        self.assertIn("- [ ] Throw the teapot", self.body("n_errands"))


if __name__ == "__main__":
    unittest.main()
