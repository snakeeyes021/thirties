"""One conversation, wherever it was said: every message the assistant sends is stored once, in the conversation of
the day it's about, and the phone gets the same words (Telegram's formatting is the only difference). Each send
path is checked end to end: a real bridge and client, with only Telegram's servers faked."""

import html
import re
import unittest
import urllib.error
from datetime import timedelta
from unittest.mock import MagicMock, patch

from tests.fixtures import DAY, at, make_scheduler, mark_up
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.config import TelegramConfig
from thirties_core.inference import MockInferenceEngine
from thirties_core.memory import ConversationStore, ReminderStore, is_internal
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.telegram_bridge import EDIT_NOTICE, TelegramClient, ThirtiesTelegramBridge, plain_text

CHAT = 7
LATE = "(Sent late: the first try didn't get through.)\n\n"


class Phone:
    """Telegram's servers: what reached the phone, as the phone shows it (no formatting), and whether it's up."""

    def __init__(self):
        self.got = []
        self.down = False

    def request(self, method, payload=None, timeout=30.0):
        if method != "sendMessage":
            return {"ok": True, "result": True}
        if self.down:
            raise urllib.error.URLError("no network")
        self.got.append(html.unescape(re.sub(r"<[^>]+>", "", payload["text"])))
        return {"ok": True, "result": {"message_id": len(self.got)}}

    def read(self):
        return [t[len(LATE):] if t.startswith(LATE) else t for t in self.got]


class OneConversationCase(unittest.TestCase):

    def setUp(self):
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        self.sched = make_scheduler()
        from tests.fixtures import morning_sent
        morning_sent(self.sched.state_db)  # ordinary turns: the morning went out already
        self.sched.config.assistant.message_level = "heavy"
        self.sched.config.telegram = TelegramConfig(enabled=True, bot_token="123:FAKE", allowed_chat_ids=[CHAT])
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        self.engine = MockInferenceEngine()
        self.desktop = []
        self.svc = BackgroundService(self.sched.config, lambda t, b, k: self.desktop.append(b),
                                     scheduler=self.sched, joplin_engine=joplin, inference_engine=self.engine,
                                     sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.run_in_threads = False
        self.svc.sort_lists_enabled = False
        self.phone = Phone()
        with patch.object(TelegramClient, "get_me", return_value={"username": "bot"}):
            self.svc.bridge = ThirtiesTelegramBridge(service=self.svc)
        self.client = self.svc.bridge.client
        self.client._request = self.phone.request
        self.client.RETRY_WAITS = ()
        self.client.SAME_TEXT_MINUTES = 0
        mark_up(self.sched.state_db, at("08:30"))  # the user is up: nothing waits for the morning
        self.sched.state_db.upsert_span(EventSpan("tx", "Taxes", at("17:00"), at("18:00"), SpanCategory.TASK))

    def stored(self, day=DAY):
        """The day's conversation as the app shows it: (role, plain words, not delivered?)."""
        conv = ConversationStore(self.sched.state_db).latest_for_day(day)
        return [(m["role"], plain_text(m["content"]), bool(m.get("undelivered")))
                for m in (conv.messages if conv else []) if m.get("role") in ("user", "assistant")
                and not is_internal(m)]

    def said(self, day=DAY):
        return [text for role, text, _ in self.stored(day) if role == "assistant"]

    def assertSymmetric(self, day=DAY):
        """The conversation's assistant messages are exactly what the phone got, word for word, in order."""
        self.assertEqual(self.said(day), self.phone.read())

    @staticmethod
    def clock(hhmm):
        """Telegram's queue keeps time by the real clock: here, the test day's."""
        return patch("thirties_core.telegram_bridge.time.time", return_value=at(hhmm).timestamp())

    def update(self, text, message_id=100, edited=False):
        msg = {"message_id": message_id, "chat": {"id": CHAT}, "text": text}
        return {"update_id": message_id, "edited_message" if edited else "message": msg}


class TestEachSendPath(OneConversationCase):

    def test_a_check_in_with_its_title_in_bold_on_the_phone(self):
        self.svc.check_ins(at("18:15"))
        self.assertEqual(len(self.phone.got), 1)
        self.assertIn("Taxes", self.phone.got[0])
        self.assertSymmetric()
        self.assertEqual(self.desktop, self.said())

    def test_a_start_notice_with_its_if_theres_time_line(self):
        set_debug_time(at("17:00"))
        self.svc._manager = None
        self.svc.starts(at("17:00"))
        self.assertEqual(len(self.phone.got), 1)
        self.assertSymmetric()

    def test_a_reminder(self):
        ReminderStore(self.sched.state_db).add(at("10:00"), "Water the ferns")
        set_debug_time(at("10:00"))
        self.svc.reminders(at("10:00"))
        self.assertEqual(self.phone.read(), ["⏰ Reminder: Water the ferns"])
        self.assertSymmetric()

    def test_the_morning_in_the_conversation_of_the_day_its_about(self):
        # before sunrise it's still yesterday's day: the morning belongs to the day it opens
        self.svc.today = lambda: DAY - timedelta(days=1)
        self.svc._manager = None
        self.svc.send("☀️ Morning! On the plate today:\nTaxes", "morning", day=DAY)
        self.assertEqual(self.said(DAY - timedelta(days=1)), [])
        self.assertEqual(self.said(DAY), ["☀️ Morning! On the plate today:\nTaxes"])
        self.assertSymmetric(DAY)

    def test_a_reply_on_telegram(self):
        self.svc.bridge.handle_updates([self.update("bins are out")])
        self.assertEqual([r for r, _, _ in self.stored()], ["user", "assistant"])
        self.assertEqual(self.stored()[0][1], "bins are out")
        self.assertSymmetric()

    def test_a_lost_turn_typed_in_the_app_reaches_the_phone_once(self):
        from tests.test_conversation import reply
        from thirties_core import turns
        waits, background = turns.RETRY_WAITS, turns.MIRROR_IN_BACKGROUND
        turns.RETRY_WAITS, turns.MIRROR_IN_BACKGROUND = (0.0, 0.0), False
        self.addCleanup(setattr, turns, "RETRY_WAITS", waits)
        self.addCleanup(setattr, turns, "MIRROR_IN_BACKGROUND", background)
        turns.add_mirror(self.svc._mirror)
        self.addCleanup(turns.remove_mirror, self.svc._mirror)
        timeout = {"role": "assistant", "content": "The Antigravity assistant timed out waiting.", "tool_calls": []}
        self.engine.responses = [timeout] * 3
        self.svc.manager().send_user_message("kiln is off", via="app")
        self.phone.got.clear()  # the lost turn's own note
        self.engine.responses = [reply("Logged: kiln off.")]
        self.svc._fixed_now = True
        self.svc.answer_lost_turns(at("09:03"))
        self.assertEqual(self.phone.read(), ["🖥 You, on the desktop:\nkiln is off", "Logged: kiln off."])
        self.assertEqual(self.said()[-1], "Logged: kiln off.")
        self.assertEqual(self.said().count("Logged: kiln off."), 1)

    def test_a_message_a_turn_already_stored_goes_out_as_stored(self):
        self.svc.manager().add_assistant_message("Taxes at 5:00 PM, then Oil the bike chain")
        self.svc.send("Taxes at 5:00 PM, then Oil the bike chain", "reply", to_conversation=False)
        self.assertSymmetric()


class TestCommandsAndYeses(OneConversationCase):

    def test_commands_are_kept_with_their_replies(self):
        for i, cmd in enumerate(("/now", "/day", "/sync")):
            self.svc.bridge.handle_updates([self.update(cmd, 200 + i)])
        stored = self.stored()
        self.assertEqual([t for r, t, _ in stored if r == "user"], ["/now", "/day", "/sync"])
        self.assertSymmetric()

    def test_status_is_kept_as_one_compact_line(self):
        self.svc.bridge.handle_updates([self.update("/status", 211)])
        self.assertEqual([t for r, t, _ in self.stored() if r == "user"], ["/status"])
        status_line, = self.said()
        self.assertTrue(status_line.startswith("Status: "), status_line)
        self.assertEqual(len(self.phone.got), 1)
        self.assertNotIn("\n", status_line)

    def test_help_names_each_menu_command_on_its_own_line(self):
        from thirties_core.telegram_bridge import MENU_COMMANDS
        self.svc.bridge.handle_updates([self.update("/help", 210)])
        self.assertEqual([t for r, t, _ in self.stored() if r == "user"], ["/help"])
        help_text, = self.said()
        lines = help_text.split("\n")
        for entry in MENU_COMMANDS:
            found = [ln for ln in lines if ln.startswith("/" + entry["command"] + ": ")]
            self.assertEqual(len(found), 1, (entry["command"], help_text))
            self.assertGreater(len(found[0].split(": ", 1)[1].strip()), 8)
        self.assertFalse(any(ln.lstrip().startswith(("-", "•", "*")) for ln in lines), help_text)
        self.assertNotIn("on your phone", help_text)
        self.assertSymmetric()  # the phone and the chat got the same words

    def test_a_new_conversation_is_kept_at_the_end_of_the_one_it_closes(self):
        self.svc.bridge.handle_updates([self.update("bins are out", 220)])
        first = ConversationStore(self.sched.state_db).latest_for_day(DAY).id
        self.svc.bridge.handle_updates([self.update("/new", 221)])
        store = ConversationStore(self.sched.state_db)
        closed = store.get(first)
        self.assertEqual(closed.messages[-2]["content"], "/new")
        self.assertEqual([plain_text(m["content"]) for m in closed.messages if m["role"] == "assistant"],
                         self.phone.read())
        self.assertNotEqual(store.latest_for_day(DAY).id, first)

    def test_a_thumbs_up_yes_is_kept_as_the_users_line(self):
        self.svc.send("☀️ Good morning! Taxes at 5. Sound good?", "rollover")
        mid = self.client.sent_ids[-1][1]
        self.svc.bridge.handle_updates([{"update_id": 300, "message_reaction": {
            "chat": {"id": CHAT}, "message_id": mid, "new_reaction": [{"type": "emoji", "emoji": "👍"}]}}])
        self.assertEqual([t for r, t, _ in self.stored() if r == "user"], ["👍"])
        self.assertSymmetric()

    def test_an_edit_after_the_reply_is_kept_with_its_note(self):
        self.svc.bridge.handle_updates([self.update("bins are out", 400)])
        self.svc.bridge.handle_updates([self.update("recycling is out too", 400, edited=True)])
        stored = self.stored()
        self.assertEqual(stored[-2][:2], ("user", "(edited) recycling is out too"))
        self.assertEqual(stored[-1][1], EDIT_NOTICE)
        self.assertSymmetric()


class TestWhenThePhoneCantBeReached(OneConversationCase):

    def test_a_notice_dropped_as_stale_is_never_kept(self):
        ReminderStore(self.sched.state_db).add(at("10:00"), "Water the ferns")
        set_debug_time(at("10:00"))
        self.phone.down = True
        with self.clock("10:00"):
            self.svc.reminders(at("10:00"))
        self.assertEqual(self.said(), [])  # waiting for the phone
        self.phone.down = False
        with self.clock("12:30"):
            self.client.flush_undelivered()  # two and a half hours on: stale
        self.assertEqual(self.phone.got, [])
        self.assertEqual(self.said(), [])
        self.assertEqual(self.svc._waiting, {})

    def test_a_notice_that_gets_there_in_time_is_kept_then(self):
        ReminderStore(self.sched.state_db).add(at("10:00"), "Water the ferns")
        set_debug_time(at("10:00"))
        self.phone.down = True
        with self.clock("10:00"):
            self.svc.reminders(at("10:00"))
        self.phone.down = False
        with self.clock("10:20"):
            self.client.flush_undelivered()
        self.assertEqual(self.said(), ["⏰ Reminder: Water the ferns"])
        self.assertSymmetric()

    def test_a_message_that_didnt_get_there_is_kept_with_a_mark_that_comes_off_when_it_does(self):
        self.phone.down = True
        self.svc.send("📝 Added Oil the bike chain to Chores.", "today")
        self.assertEqual(self.stored(), [("assistant", "📝 Added Oil the bike chain to Chores.", True)])
        self.phone.down = False
        self.client.flush_undelivered()
        self.assertEqual(self.stored(), [("assistant", "📝 Added Oil the bike chain to Chores.", False)])
        self.assertSymmetric()

    def test_the_mark_stays_when_it_never_gets_there(self):
        self.phone.down = True
        with self.clock("09:00"):
            self.svc.send("📝 Added Oil the bike chain to Chores.", "today")
        self.phone.down = False
        with self.clock("13:00"):
            self.client.flush_undelivered()  # past the queue's few hours: dropped
        self.assertEqual(self.phone.got, [])
        self.assertEqual(self.stored(), [("assistant", "📝 Added Oil the bike chain to Chores.", True)])

    def test_a_telegram_reply_that_didnt_get_there_is_marked_until_it_does(self):
        self.phone.down = True
        self.svc.bridge.handle_updates([self.update("bins are out", 500)])
        self.assertTrue(self.stored()[-1][2])
        self.phone.down = False
        self.client.flush_undelivered()
        self.assertFalse(self.stored()[-1][2])
        self.assertSymmetric()

    def test_a_command_reply_that_didnt_get_there_is_marked_until_it_does(self):
        self.phone.down = True
        self.svc.bridge.handle_updates([self.update("/day", 510)])
        self.assertTrue(self.stored()[-1][2])
        self.phone.down = False
        self.client.flush_undelivered()
        self.assertFalse(self.stored()[-1][2])
        self.assertSymmetric()

    def test_the_morning_of_the_next_day_loses_its_mark_there(self):
        self.svc.today = lambda: DAY - timedelta(days=1)
        self.svc._manager = None
        self.phone.down = True
        self.svc.send("☀️ Morning! On the plate today:\nTaxes", "morning", day=DAY)
        self.assertEqual(self.stored(DAY), [("assistant", "☀️ Morning! On the plate today:\nTaxes", True)])
        self.phone.down = False
        self.client.flush_undelivered()
        self.assertEqual(self.stored(DAY), [("assistant", "☀️ Morning! On the plate today:\nTaxes", False)])


if __name__ == "__main__":
    unittest.main()
