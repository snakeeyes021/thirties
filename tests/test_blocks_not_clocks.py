"""A schedule is shown in blocks (AGENTS.md §7, "Natural units"): every view the user reads lists the day by its
half-hour thirties, and never shows a minute time Thirties worked out itself.

Under the hood items keep their minutes: a routine's steps packed one after another in a thirty, a done item's
estimated time, the length of something Thirties placed. None of those may reach the user. A time off the half hours
is shown only when the user or a calendar gave it: a calendar event at 2:45, a done item the user said ran 1:10 to
1:40, a 4:45 pickup named in the chat.

Every user-facing text is rendered from the invented day below: the schedule (from now on, and in full, for
this date or a later one), the full-day record, what comes next, the show_schedule reply, the
Telegram /day and /now, the start bells and check-ins, and the Agenda's and Timeline's labels. Each must show only
block edges, apart from the times given.
"""

from __future__ import annotations

import re
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler
from tests.test_conversation import reply
from tests.test_telegram_bridge import BridgeCase
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ConversationManager
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory, SpanStatus

_ANY_TIME = re.compile(r"(?<![\d:])(1[0-2]|0?[1-9]):([0-5]\d)(?![\d:])")
# The times given on the invented day: the calendar event's, the done item the user timed, and the errand the user
# asked for at a time of their own (as (hour, minute) on a 12-hour clock).
GIVEN = {(2, 45), (3, 20), (1, 10), (1, 40), (4, 45)}


def invented_day(db) -> None:
    """One day with every kind of minute time there is under the hood."""
    spans = []
    # a morning routine's part: four steps packed into one thirty, and one running into the next
    t = at("07:30")
    for title, minutes in (("Wake stretch", 4), ("Kettle on", 7), ("Floss", 7), ("Tidy desk", 8), ("Plan the day", 8)):
        spans.append(EventSpan(f"r-{title}", title, t, t + timedelta(minutes=minutes), SpanCategory.ACTIVITY,
                               source="routine"))
        t += timedelta(minutes=minutes)
    # done with no times said: estimated, all at the same odd minutes
    for n, title in enumerate(("Fold towels", "Wipe counters", "Sort the post")):
        spans.append(EventSpan(f"d{n}", title, at("10:46"), at("10:52"), SpanCategory.ACTIVITY,
                               status=SpanStatus.DONE, actual_start=at("10:46"), actual_end=at("10:52"),
                               time_source="estimated"))
    # done, timed by the user ("I did it 1:10 to 1:40")
    spans.append(EventSpan("bike", "Bike tune-up", at("13:10"), at("13:40"), SpanCategory.ACTIVITY,
                           status=SpanStatus.DONE, actual_start=at("13:10"), actual_end=at("13:40"),
                           time_source="reported"))
    # placed by Thirties, with lengths that end off the half hours
    spans.append(EventSpan("porch", "Sweep the porch", at("15:30"), at("15:37"), SpanCategory.TASK))
    spans.append(EventSpan("basil", "Repot the basil", at("17:00"), at("17:15"), SpanCategory.TASK))
    spans.append(EventSpan("seed", "Order seeds", at("17:15"), at("17:22"), SpanCategory.TASK))
    # an item that runs past its blocks with an odd end
    spans.append(EventSpan("quilt", "Quilt binding", at("18:00"), at("20:10"), SpanCategory.ACTIVITY))
    # asked for by the user at a time of their own
    spans.append(EventSpan("clean", "Pick up the dry cleaning", at("16:45"), at("17:00"), SpanCategory.TASK))
    # a calendar event at an odd time
    spans.append(EventSpan("ortho", "Orthodontist", at("14:45"), at("15:20"), SpanCategory.CALENDAR,
                           source="calendar", is_firm=True))
    for s in spans:
        db.upsert_span(s)


def odd_times(text: str) -> list:
    """Every time in the text off the half hours that nobody gave."""
    return [m.group(0) for m in _ANY_TIME.finditer(text)
            if int(m.group(2)) not in (0, 30) and (int(m.group(1)), int(m.group(2))) not in GIVEN]


class GuardCase(unittest.TestCase):

    def assertBlocks(self, text: str, where: str) -> None:
        self.assertEqual(odd_times(text), [], f"{where}:\n{text}")


class TestTheConversationAndMessages(GuardCase):

    def setUp(self) -> None:
        set_debug_time(at("12:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        invented_day(self.sched.state_db)
        plan, pending = self.sched.build_day_plan(DAY)
        self.engine = MockInferenceEngine()
        self.manager = ConversationManager(plan, [], pending, self.sched, inference_engine=self.engine,
                                           active_notes=[])

    def test_the_schedule_in_every_mode(self) -> None:
        from thirties_core.messages import render_schedule, schedule_rows, status_line, whole_day_lines
        plan = self.manager.day_plan
        for now in (at("07:40"), at("12:10"), at("17:05"), at("18:20"), None):
            for whole in (False, True):
                self.assertBlocks(render_schedule(plan, now, whole_day=whole), f"schedule at {now}, whole={whole}")
            if now is not None:
                self.assertBlocks(status_line(plan, now), f"status at {now}")
                self.assertBlocks("\n".join(schedule_rows(plan, now)), f"rows from {now}")
        lines = whole_day_lines(plan)
        self.assertBlocks("\n".join(lines), "the whole day")
        # the routine's steps together in their thirty, and the one that runs on in the same line
        self.assertIn("7:30 AM – 8:30 AM Wake stretch, Kettle on, Floss, Tidy desk, Plan the day", lines)
        # three estimated done items: one line in their block, no minute times
        self.assertIn("10:30 AM – 11:00 AM Fold towels + Wipe counters + Sort the post (all done)", lines)
        self.assertIn("1:10 PM – 1:40 PM Bike tune-up (done)", lines)  # the user's own times
        self.assertIn("2:45 PM – 3:20 PM Orthodontist", lines)  # the calendar's
        self.assertIn("6:00 PM – 8:30 PM Quilt binding", lines)  # a long item: its blocks
        rest = render_schedule(plan, at("12:10")).splitlines()
        self.assertIn("2:30 PM – Orthodontist at 2:45 PM (until 3:20 PM)", rest)  # its block, and its real time
        self.assertIn("5:00 PM – Repot the basil, then Order seeds (until 5:30 PM)", rest)  # packed: one block

    def test_whats_next_and_the_show_schedule_reply(self) -> None:
        from thirties_core.whats_next import render, summary
        plan = self.manager.day_plan
        for now in (at("07:40"), at("12:10"), at("16:50"), at("18:20")):
            for mode in ("next", "rest"):
                self.assertBlocks(render(plan, now, mode=mode), f"what's next at {now}, {mode}")
            self.assertBlocks(summary(self.manager.tools, now, mode="rest"), f"summary at {now}")
        for whole in ("true", "false"):
            self.engine.set_next_response(reply(f'CALL show_schedule {{"whole_day": {whole}}}'))
            self.engine.set_next_response(reply("Here's the day."))
            self.assertBlocks(self.manager.send_user_message("show me the day"), f"show_schedule whole={whole}")

    def test_a_reply_that_copies_minute_times_says_blocks(self) -> None:
        self.engine.set_next_response(reply("Floss is 7:41 AM – 7:48 AM, the towels were 10:46 AM – 10:52 AM, the "
                                            "basil ends at 5:15 PM, and the orthodontist is at 2:45 PM."))
        out = self.manager.send_user_message("what did the morning look like?")
        self.assertBlocks(out, "a reply")
        self.assertIn("2:45 PM", out)  # the calendar's time stays

    def test_another_day_shows_the_same_way(self) -> None:
        from thirties_core.messages import render_schedule
        self.assertBlocks(render_schedule(self.manager.day_plan, None, label="Thursday"), "another day")


class TestNoticesAndBells(GuardCase):

    def setUp(self) -> None:
        from thirties_core.background import BackgroundService
        set_debug_time(at("07:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.message_level = "heavy"  # every item
        invented_day(self.sched.state_db)
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.sent = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.sent.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()

    def test_bells_and_check_ins_say_blocks(self) -> None:
        t = at("07:30")
        while t < at("21:00"):
            set_debug_time(t)
            for tick in (t, t + timedelta(minutes=1), t + timedelta(minutes=16)):
                self.svc.starts(tick)
                self.svc.check_ins(tick)
            t += timedelta(minutes=30)
        self.assertTrue(self.sent)
        for kind, body in self.sent:
            self.assertBlocks(body, kind)


class TestTelegram(BridgeCase, GuardCase):

    def setUp(self) -> None:
        super().setUp()
        invented_day(self.scheduler.state_db)

    def test_day_and_now(self) -> None:
        for now in (at("07:40"), at("12:10"), at("17:05")):
            for command in ("/day", "/now"):
                self.bridge.client.send_message.reset_mock()
                set_debug_time(now)
                with patch.object(self.bridge, "_get_current_time", return_value=now):
                    self.bridge.handle_message(chat_id=1001, text=command)
                msg = self.bridge.client.send_message.call_args[0][1]
                self.assertBlocks(msg, f"{command} at {now}")


class TestTheAgendaAndTimeline(GuardCase):

    def test_card_labels(self) -> None:
        from thirties_core.day_layout import build_segments, card_rows, segment_context
        from thirties_core.timeline_layout import day_layout
        set_debug_time(at("12:10"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        invented_day(sched.state_db)
        plan, _ = sched.build_day_plan(DAY)
        for seg in build_segments(plan):
            self.assertBlocks(segment_context(seg, plan) or "", "a card's context line")
            for row in card_rows(plan, seg):
                self.assertBlocks(" ".join(str(v) for v in vars(row).values() if isinstance(v, str)), "a card row")
        layout = day_layout(plan, at("12:10"))
        for card in layout.cards:
            self.assertBlocks(card.times, f"the tooltip of {card.span.title}")
            self.assertBlocks(" ".join(card.notes), f"the notes of {card.span.title}")
        ortho = next(c for c in layout.cards if c.span.title == "Orthodontist")
        self.assertEqual(ortho.times, "2:45 PM – 3:20 PM")  # the calendar's own time


class TestItemPaths(unittest.TestCase):
    """An item's path names what it's about, never the heading that only says where it stands."""

    def test_a_state_heading_is_no_parent(self) -> None:
        from types import SimpleNamespace
        from thirties_core.item_path import label
        for heading in ("Active", "Probation", "Done", "Someday", "Running", "Moot", "On hold", "Current:"):
            item = SimpleNamespace(id="i1", title="Grout the tiles", name="Grout the tiles", section=heading,
                                   list_role="", path=[], parent_task_id=None)
            self.assertEqual(label(item, [item]), "Grout the tiles", heading)
        topic = SimpleNamespace(id="i2", title="Grout the tiles", name="Grout the tiles", section="Bathroom",
                                list_role="", path=[], parent_task_id=None)
        self.assertEqual(label(topic, [topic]), "Bathroom › Grout the tiles")
        child = SimpleNamespace(id="i3", title="Buy grout", name="Buy grout", section="Active", list_role="",
                                path=["Grout the tiles"], parent_task_id=None)
        self.assertEqual(label(child, [child]), "Grout the tiles › Buy grout")  # a parent line still counts


if __name__ == "__main__":
    unittest.main()
