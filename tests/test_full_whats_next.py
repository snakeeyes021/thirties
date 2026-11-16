"""What's next lists the whole rest of today: every row from the current block to the end of the day, wherever the
assistant says what's coming up (a reply after a change, /now on the phone, a bell). Only asking for less trims it
to the next few rows. Scripted model replies; an invented evening. DAY is Thu Oct 15 (Work 8:30 AM - 4 PM, Sleep
from 11 PM)."""

import unittest
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler, morning_sent
from tests.test_conversation import reply
from tests.test_telegram_bridge import BridgeCase
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.clock import format_clock
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.settings import settings_for

# Thirteen invented things, one each half hour from 4:00 PM: at 4:10 the first is on now and twelve rows follow
TITLES = ["Sharpen the chisels", "Label the jam jars", "Oil the gate hinges", "Sketch the footbridge",
          "Defrost the freezer", "Adjust the bike gears", "Sort the seed packets", "Mend the box kite",
          "Read the atlas chapter", "Polish the brass lamp", "Practise sailing knots", "Write to the pen friend",
          "Water the bonsai"]
STARTS = [f"{16 + i // 2:02d}:{30 * (i % 2):02d}" for i in range(len(TITLES))]  # 16:00, 16:30, ... 22:00


def add_evening(db) -> None:
    for i, (title, start) in enumerate(zip(TITLES, STARTS)):
        h, m = (int(x) for x in start.split(":"))
        end = f"{h + (m + 30) // 60:02d}:{(m + 30) % 60:02d}"
        db.upsert_span(EventSpan(f"ev{i}", title, at(start), at(end), SpanCategory.ACTIVITY))


def rows_of(text: str, heading: str = "The rest of today:") -> list:
    """The rows under the heading, up to the next blank line."""
    lines = text.splitlines()
    i = lines.index(heading)
    out = []
    for line in lines[i + 1:]:
        if not line.strip():
            break
        out.append(line)
    return out


TWELVE = [f"{format_clock(at(start))} – {title}" for start, title in zip(STARTS[1:], TITLES[1:])]


class EveningCase(unittest.TestCase):

    def setUp(self) -> None:
        set_debug_time(at("16:10"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        self.sched.config.assistant.after_change = "rest"
        self.sched.config.assistant.message_level = "heavy"
        morning_sent(self.sched.state_db)
        add_evening(self.sched.state_db)
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.desktop = []
        self.engine = MockInferenceEngine()
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append((k, b)),
                                     scheduler=self.sched, joplin_engine=joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.bridge = MagicMock()
        self.svc.bridge.broadcast.return_value = True

    def turn(self, *replies: str, said: str = "ok") -> str:
        for r in replies:
            self.engine.set_next_response(reply(r))
        return self.svc.manager().send_user_message(said)


class TestEveryRowAfterAChange(EveningCase):

    def test_twelve_upcoming_rows_all_listed_after_a_change(self) -> None:
        out = self.turn('CALL log_outcome {"event": "ev0", "status": "done"}', "Nice, chisels done.",
                        said="sharpened the chisels")
        self.assertEqual(rows_of(out), TWELVE)  # every row to the end of the day, none cut
        self.assertNotIn("Next:", out)
        self.assertFalse(any("more" in line for line in out.splitlines()))  # no "+N more"
        self.assertFalse(any(line.startswith("- ") for line in out.splitlines()))  # plain lines

    def test_the_block_it_is_in_is_marked_now(self) -> None:
        out = self.turn('CALL add_event {"title": "Fold the tarp", "start": "10:30 PM", "duration": "30 minutes"}',
                        "Added the tarp.", said="fold the tarp at 10:30")
        self.assertIn("Now: Sharpen the chisels", out.splitlines())
        self.assertEqual(rows_of(out), TWELVE + [f"{format_clock(at('22:30'))} – Fold the tarp"])

    def test_a_setting_kept_by_earlier_versions_shows_it_whole_too(self) -> None:
        settings_for(self.sched.state_db).set("assistant.after_change", "next")  # their stored default
        settings_for(self.sched.state_db).apply(self.sched.config)
        out = self.turn('CALL log_outcome {"event": "ev0", "status": "done"}', "Noted.", said="chisels done")
        self.assertEqual(rows_of(out), TWELVE)


class TestEveryRowOnThePhone(BridgeCase):

    def test_now_lists_all_twelve(self) -> None:
        add_evening(self.scheduler.state_db)
        with patch.object(self.bridge, "_get_current_time", return_value=at("16:10")):
            self.bridge.handle_message(chat_id=1001, text="/now")
        msg = self.bridge.client.send_message.call_args[0][1]
        self.assertTrue(msg.startswith("Now: Sharpen the chisels\n\nThe rest of today:\n"), msg)
        self.assertEqual(rows_of(msg), TWELVE)

    def test_a_long_rest_of_today_is_split_between_rows_never_inside_one(self) -> None:
        from thirties_core.telegram_bridge import TelegramClient
        add_evening(self.scheduler.state_db)
        with patch.object(self.bridge, "_get_current_time", return_value=at("16:10")):
            self.bridge.handle_message(chat_id=1001, text="/now")
        msg = self.bridge.client.send_message.call_args[0][1]
        chunks = TelegramClient._chunk_text(msg, max_len=120)  # as if the day were too long for one message
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), msg)  # nothing dropped
        for chunk in chunks:
            for line in chunk.splitlines():
                self.assertIn(line, msg.splitlines())  # every row whole, in one message


class TestEveryRowInABell(EveningCase):

    def test_the_bell_lists_all_twelve(self) -> None:
        self.svc.starts(at("16:00"))
        bell = [b for k, b in self.desktop if k == "start"][-1]
        self.assertTrue(bell.startswith(f"🔔 {format_clock(at('16:00'))}: Sharpen the chisels\n\n"), bell)
        self.assertEqual(rows_of(bell), TWELVE)
        self.assertNotIn("Now:", bell)  # the bell says what's on now itself


class TestAskingForLess(EveningCase):
    """The user can still trim it to the next few rows, or bring the whole rest of today back, from chat."""

    def extras(self, said: str, show: str) -> str:
        tools = self.svc.manager().tools
        tools.user_message = said
        return tools.execute("set_reply_extras", {"part": "whats_next", "show": show})

    def test_trimmed_then_whole_again(self) -> None:
        out = self.extras("just the next few things after each change from now on", "few")
        self.assertFalse(out.startswith("NOT_DONE"), out)
        self.assertEqual(settings_for(self.sched.state_db).get("assistant.after_change"), "few")
        out = self.turn('CALL log_outcome {"event": "ev0", "status": "done"}', "Noted.", said="chisels done")
        self.assertEqual(rows_of(out, "Next:"), TWELVE[:3])
        self.extras("show me the whole rest of the day after each change again, from now on", "rest")
        self.assertEqual(settings_for(self.sched.state_db).get("assistant.after_change"), "rest")
        self.svc.manager().tools.user_message = None
        out = self.turn('CALL log_outcome {"event": "ev1", "status": "done"}', "Noted.", said="jars labelled")
        self.assertEqual([r for r in rows_of(out) if not r.startswith("Free")], TWELVE[1:])

    def test_a_complaint_trims_nothing(self) -> None:
        out = self.extras("why is the kite at 7:30, that's wrong", "few")
        self.assertTrue(out.startswith("NOT_DONE"), out)
        self.assertIsNone(settings_for(self.sched.state_db).get("assistant.after_change"))


if __name__ == "__main__":
    unittest.main()
