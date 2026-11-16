"""No reply keyboard under the Telegram chat. An earlier version kept a "What's next" button there; Telegram keeps a
keyboard until the bot sends a message that removes it. So nothing the bot sends carries a keyboard any more, and
once per chat the bot's first start sends where the day stands (what /now says) with remove_keyboard, stored once in
the app's chat. The command menu is untouched. A fake bot stands in for Telegram's servers; items are invented."""

import json
import unittest
import urllib.error
from unittest.mock import patch

from tests.fixtures import at
from tests.test_one_conversation import CHAT, OneConversationCase
from thirties_core.astronomy import set_debug_time
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.telegram_bridge import (KEYBOARD_REMOVED_KEY, MENU_COMMANDS, TelegramClient,
                                           ThirtiesTelegramBridge, plain_text)


class FakeBot:
    """Telegram's servers, keeping every request exactly as the bot made it."""

    def __init__(self):
        self.calls = []
        self.down = False

    def request(self, method, payload=None, timeout=30.0):
        self.calls.append((method, payload))
        if method == "sendMessage":
            if self.down:
                raise urllib.error.URLError("no network")
            return {"ok": True, "result": {"message_id": len(self.sent())}}
        return {"ok": True, "result": True}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def removals(self):
        return [p for p in self.sent() if "reply_markup" in p]


class KeyboardCase(OneConversationCase):

    def setUp(self):
        super().setUp()
        self.sched.config.assistant.after_change = "next"
        self.bot = FakeBot()
        self.client._request = self.bot.request
        self.sched.state_db.upsert_span(EventSpan("gar", "Water the garden", at("09:30"), at("10:00"),
                                                  SpanCategory.TASK))
        self.svc._manager = None

    def first_start(self, bridge=None):
        """What the bridge does when it starts polling, then stops at once."""
        bridge = bridge or self.svc.bridge
        bridge.client.get_updates = lambda *a, **k: setattr(bridge, "running", False) or []
        bridge.poll_forever()

    def restart(self):
        """The app restarted: a new bridge over the same saved state."""
        with patch.object(TelegramClient, "get_me", return_value={"username": "bot"}):
            self.svc.bridge = ThirtiesTelegramBridge(service=self.svc)
        self.client = self.svc.bridge.client
        self.client._request = self.bot.request
        self.client.RETRY_WAITS = ()
        self.client.SAME_TEXT_MINUTES = 0
        return self.svc.bridge


class TestNoKeyboardOnAnyMessage(KeyboardCase):

    def test_nothing_the_bot_sends_carries_a_keyboard(self):
        self.svc.check_ins(at("09:15"))
        self.svc.starts(at("09:30"))
        for text in ("/now", "What's next", "/day", "/help", "/status", "/sync", "/unknown"):
            self.svc.bridge.handle_message(CHAT, text)
        self.svc.bridge.handle_message(CHAT, "how is my day looking?")
        self.svc.bridge.broadcast("A plain notice")
        self.svc.send("A notice from the service", "notice")
        self.svc.bridge.handle_message(9999, "hello")  # a chat that isn't the paired phone
        self.assertGreaterEqual(len(self.bot.sent()), 8)
        self.assertEqual(self.bot.removals(), [])
        self.assertFalse(any("keyboard" in json.dumps(p) for p in self.bot.sent()))

    def test_a_queued_message_sent_late_carries_no_keyboard_either(self):
        self.bot.down = True
        self.svc.send("🔔 9:30: Water the garden", "start")
        self.assertEqual(len(self.client.undelivered), 1)
        self.bot.down = False
        self.client.flush_undelivered()
        self.assertEqual(self.client.undelivered, [])
        self.assertEqual(self.bot.removals(), [])
        self.assertEqual(len(self.bot.sent()), 2)


class TestTheOldKeyboardIsTakenAwayOnce(KeyboardCase):

    def test_the_first_start_sends_whats_next_with_remove_keyboard(self):
        expected = self.svc.bridge._cmd_now(CHAT)[0]
        self.first_start()
        self.assertEqual(len(self.bot.sent()), 1)
        sent = self.bot.sent()[0]
        self.assertEqual(sent["chat_id"], CHAT)
        self.assertEqual(sent["reply_markup"], {"remove_keyboard": True})
        self.assertEqual(plain_text(expected), self.phone_text(sent))
        self.assertIn("Water the garden", sent["text"])

    @staticmethod
    def phone_text(payload):
        import html
        import re
        return html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))

    def test_it_is_stored_once_in_the_app_chat_as_the_phone_got_it(self):
        self.first_start()
        self.assertEqual(len(self.said()), 1)
        self.assertEqual(self.said()[0], self.phone_text(self.bot.sent()[0]))
        self.assertEqual(self.desktop, [])  # nothing pops up on the desktop for it

    def test_it_says_nothing_about_the_change(self):
        self.first_start()
        text = self.phone_text(self.bot.sent()[0]).lower()
        for word in ("keyboard", "button", "removed", "update", "version", "changed"):
            self.assertNotIn(word, text)

    def test_never_again_across_restarts(self):
        self.first_start()
        for _ in range(3):
            bridge = self.restart()
            self.first_start(bridge)
        self.assertEqual(len(self.bot.removals()), 1)
        self.assertEqual(len(self.bot.sent()), 1)
        self.assertEqual(len(self.said()), 1)

    def test_it_is_kept_in_the_synced_state_per_bot(self):
        self.first_start()
        saved = self.sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123")
        self.assertEqual(json.loads(saved), [str(CHAT)])

    def test_the_command_menu_is_registered_as_before(self):
        self.first_start()
        self.restart()
        menus = [p for m, p in self.bot.calls if m == "setMyCommands"]
        self.assertEqual(menus, [{"commands": MENU_COMMANDS}])
        self.assertEqual([c["command"] for c in MENU_COMMANDS], ["now", "day", "new", "sync", "status", "help"])
        self.assertEqual(MENU_COMMANDS[0], {"command": "now", "description": "What's next"})

    def test_a_message_that_doesnt_get_through_is_tried_again_next_start(self):
        self.bot.down = True
        self.first_start()
        self.assertEqual(self.bot.sent()[0]["reply_markup"], {"remove_keyboard": True})
        self.assertIsNone(self.sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123"))
        self.assertEqual(self.client.undelivered[0][5], {"remove_keyboard": True})  # the queued copy removes it too
        self.bot.down = False
        self.client.undelivered.clear()
        self.first_start(self.restart())
        self.assertEqual(self.bot.sent()[-1]["reply_markup"], {"remove_keyboard": True})
        self.assertEqual(json.loads(self.sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123")), [str(CHAT)])

    def test_a_queued_removal_still_removes_the_keyboard_when_sent_late(self):
        self.bot.down = True
        self.first_start()
        self.bot.down = False
        self.client.flush_undelivered()
        self.assertEqual(self.bot.sent()[-1]["reply_markup"], {"remove_keyboard": True})
        self.assertTrue(self.bot.sent()[-1]["text"].startswith("(Sent late"))

    def test_while_messages_are_held_nothing_goes_out_and_nothing_is_remembered(self):
        with patch.object(type(self.svc), "_held", return_value=True):
            self.first_start()
        self.assertEqual(self.bot.sent(), [])
        self.assertIsNone(self.sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123"))
        self.first_start(self.restart())
        self.assertEqual(len(self.bot.removals()), 1)


class TestWithoutTelegram(unittest.TestCase):

    def test_with_telegram_off_nothing_happens_and_nothing_is_remembered(self):
        from unittest.mock import MagicMock
        from tests.fixtures import make_scheduler
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        set_debug_time(at("09:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()  # no bot token, no paired phone
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        svc = BackgroundService(sched.config, lambda *a: None, scheduler=sched, joplin_engine=joplin,
                                inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.run_in_threads = False
        with patch("thirties_core.background.threading.Thread"):  # started, but nothing really runs
            svc.start()
        self.assertIsNone(svc.bridge)
        self.assertIsNone(sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123"))

    def test_no_paired_chat_means_nothing_to_remove_it_from(self):
        case = KeyboardCase()
        case.setUp()
        try:
            case.svc.bridge.allowed_chat_ids = set()
            case.first_start()
            self.assertEqual(case.bot.sent(), [])
            self.assertIsNone(case.sched.state_db.get_sync_meta(f"{KEYBOARD_REMOVED_KEY}:123"))
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
