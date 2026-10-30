import json
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from thirties_core.config import TelegramConfig, ThirtiesConfig
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory
from thirties_core.scheduler import DeterministicScheduler, StateDatabase
from thirties_core.telegram_bridge import TelegramClient, ThirtiesTelegramBridge


class TestTelegramClient(unittest.TestCase):
    def setUp(self):
        self.client = TelegramClient("123456:FAKE_TOKEN")

    def test_chunk_text_short(self):
        text = "Hello world"
        chunks = self.client._chunk_text(text, max_len=100)
        self.assertEqual(chunks, ["Hello world"])

    def test_chunk_text_long(self):
        lines = [f"Line {i}" for i in range(200)]
        text = "\n".join(lines)
        chunks = self.client._chunk_text(text, max_len=100)
        self.assertTrue(len(chunks) > 1)
        rejoined = "".join(chunks)
        self.assertEqual(rejoined, text)

    @patch("urllib.request.urlopen")
    def test_get_me_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"id": 999, "username": "thirties_test_bot", "first_name": "Thirties Bot"}
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        me = self.client.get_me()
        self.assertEqual(me["username"], "thirties_test_bot")

    @patch("urllib.request.urlopen")
    def test_send_message_markdown_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "result": {}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        ok = self.client.send_message(12345, "*Bold Text*")
        self.assertTrue(ok)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_send_message_fallback_on_markdown_error(self, mock_urlopen):
        err_response = urllib.error.HTTPError(
            url="https://api.telegram.org",
            code=400,
            msg="Bad Request: can't parse entities",
            hdrs={},
            fp=None,
        )
        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({"ok": True, "result": {}}).encode("utf-8")
        
        mock_urlopen.side_effect = [err_response, MagicMock(__enter__=MagicMock(return_value=success_resp))]

        ok = self.client.send_message(12345, "Unbalanced _markdown", parse_mode="Markdown")
        self.assertTrue(ok)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_a_dropped_send_is_retried_then_kept_for_later(self, mock_urlopen, _sleep):
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps({"ok": True, "result": {}}).encode("utf-8")
        sent = MagicMock(__enter__=MagicMock(return_value=ok_resp))
        mock_urlopen.side_effect = [ConnectionResetError(), sent]
        self.assertTrue(self.client.send_message(12345, "Lunch"))  # a second try gets through
        mock_urlopen.side_effect = [ConnectionResetError()] * 3
        self.assertFalse(self.client.send_message(12345, "We could start charging the watch"))
        self.assertEqual(len(self.client.undelivered), 1)
        mock_urlopen.side_effect = [sent]
        self.client.flush_undelivered()
        self.assertEqual(self.client.undelivered, [])
        body = json.loads(mock_urlopen.call_args[0][0].data)
        self.assertTrue(body["text"].startswith("(Sent late") and body["text"].endswith("charging the watch"))


class TestThirtiesTelegramBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.sqlite"
        self.state_db = StateDatabase(self.db_path)

        self.config = ThirtiesConfig(
            telegram=TelegramConfig(
                enabled=True,
                bot_token="FAKE_TOKEN",
                allowed_chat_ids=[1001],
                notify_block_transitions=True,
            )
        )

        from tests.fixtures import at
        from thirties_core.astronomy import set_debug_time
        from thirties_core.calendar_engine import MockCalendarEngine
        set_debug_time(at("09:00"))  # a fixed workday, never the real date or calendar
        self.addCleanup(set_debug_time, None)
        self.scheduler = DeterministicScheduler(
            config=self.config,
            state_db=self.state_db,
            calendar_engine=MockCalendarEngine([]),
        )

        self.mock_joplin = MagicMock()
        self.mock_joplin.fetch_tasks.return_value = []
        self.mock_joplin.fetch_active_notes.return_value = []
        self.mock_sync = MagicMock()
        self.mock_sync.pull.return_value = (True, "1 pulled")
        self.mock_sync.push.return_value = (True, "1 pushed")
        self.mock_inference = MockInferenceEngine()

        with patch.object(TelegramClient, "get_me", return_value={"username": "thirties_bot"}):
            self.bridge = ThirtiesTelegramBridge(
                config=self.config,
                scheduler=self.scheduler,
                joplin_engine=self.mock_joplin,
                inference_engine=self.mock_inference,
                sync_engine=self.mock_sync,
            )
        self.bridge.client = MagicMock()
        self.mock_sync.reset_mock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unauthorized_chat_rejected(self):
        self.bridge.handle_message(chat_id=9999, text="Hello")
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        self.assertIn("Unauthorized", args[1])

    def test_start_command_authorized(self):
        self.bridge.handle_message(chat_id=1001, text="/start")
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        self.assertIn("Thirties Mobile Planning Assistant", args[1])

    def test_now_command(self):
        plan = self.bridge.conv_manager.day_plan
        first = plan.blocks[0]
        self.scheduler.state_db.upsert_span(EventSpan("dw", "Deep Work Code Review", first.start_dt,
                                                      first.end_dt + timedelta(minutes=30), SpanCategory.ACTIVITY))
        with patch.object(self.bridge, "_get_current_time", return_value=first.start_dt + timedelta(minutes=5)):
            self.bridge.handle_message(chat_id=1001, text="/now")
        self.bridge.client.send_message.assert_called_once()
        msg = self.bridge.client.send_message.call_args[0][1]
        self.assertIn("Deep Work Code Review", msg)
        self.assertIn("Left in this thirty*: 25 minutes", msg)
        self.assertIn("Until sleep", msg)

    def test_day_command(self):
        self.bridge.handle_message(chat_id=1001, text="/day")
        self.bridge.client.send_message.assert_called_once()
        msg = self.bridge.client.send_message.call_args[0][1]
        self.assertIn("Schedule for", msg)
        self.assertIn("8:30 AM – 4:00 PM: Work", msg)
        self.assertNotIn("Block", msg)

    def test_new_command_starts_fresh_conversation(self):
        self.bridge.conv_manager.add_assistant_message("earlier")
        first = self.bridge.conv_manager.conversation.id
        self.bridge.handle_message(chat_id=1001, text="/new")
        self.assertEqual(self.bridge.conv_manager.history(), [])
        self.assertNotEqual(self.bridge.conv_manager.conversation and self.bridge.conv_manager.conversation.id, first)

    def test_sync_command(self):
        self.bridge.handle_message(chat_id=1001, text="/sync")
        self.mock_sync.pull.assert_called_once()
        self.mock_sync.push.assert_called_once()
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        self.assertIn("Sync Completed", args[1])

    def test_status_command(self):
        self.bridge.handle_message(chat_id=1001, text="/status")
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        msg = args[1]
        self.assertIn("Thirties System Status", msg)
        self.assertIn("Inference Backend", msg)

    def test_natural_conversation_routes_to_gemini(self):
        self.bridge.handle_message(chat_id=1001, text="Help me plan my afternoon")
        self.bridge.client.send_chat_action.assert_called_with(1001, "typing")
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        self.assertIn("ready to help you plan", args[1])

    def test_unpaired_bot_points_to_the_app(self):
        open_config = ThirtiesConfig(telegram=TelegramConfig(enabled=True, bot_token="FAKE_TOKEN", allowed_chat_ids=[]))
        bridge = ThirtiesTelegramBridge(config=open_config, scheduler=self.scheduler, joplin_engine=self.mock_joplin,
                                        inference_engine=self.mock_inference, sync_engine=self.mock_sync)
        bridge.client = MagicMock()
        bridge.handle_message(chat_id=777, text="what's on my schedule?")
        self.assertIn("Preferences → Phone", bridge.client.send_message.call_args[0][1])
        self.assertEqual(bridge.active_chats, set())

    def test_pairing_by_code_ignores_other_messages(self):
        from thirties_core.telegram_bridge import pair_by_code
        client = MagicMock()
        client.get_updates.side_effect = [
            [{"update_id": 1, "message": {"text": "hello", "chat": {"id": 5, "first_name": "Eve"}}}],
            [{"update_id": 2, "message": {"text": "/start 4821", "chat": {"id": 9, "first_name": "Matt"}}}],
            [],
        ]
        self.assertEqual(pair_by_code(client, "4821", timeout=5), {"id": 9, "name": "Matt"})
        self.assertEqual(client.send_message.call_args[0][0], 9)


if __name__ == "__main__":
    unittest.main()
