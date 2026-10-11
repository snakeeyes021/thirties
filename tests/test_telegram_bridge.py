import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from thirties_core.config import TelegramConfig, ThirtiesConfig
from thirties_core.conversation import MockInferenceEngine
from thirties_core.models import BlockKind, DayPlan, ThirtyBlock
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

        self.scheduler = DeterministicScheduler(
            config=self.config,
            state_db=self.state_db,
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
        b1 = self.bridge.conv_manager.day_plan.get_logical_block(1)
        if b1:
            b1.label = "Deep Work Code Review"
            with patch.object(self.bridge, "_get_current_time", return_value=b1.start_dt + timedelta(minutes=5)):
                self.bridge.handle_message(chat_id=1001, text="/now")
                self.bridge.client.send_message.assert_called_once()
                args, _ = self.bridge.client.send_message.call_args
                msg = args[1]
                self.assertIn("Current Block", msg)
                self.assertIn("Deep Work Code Review", msg)
                self.assertIn("Time Remaining", msg)

    def test_day_command(self):
        self.bridge.handle_message(chat_id=1001, text="/day")
        self.bridge.client.send_message.assert_called_once()
        args, kwargs = self.bridge.client.send_message.call_args
        msg = args[1]
        self.assertIn("Schedule for", msg)
        self.assertIn("Block", msg)

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

    def test_auto_bind_first_chat_if_allowed_chat_ids_empty(self):
        open_config = ThirtiesConfig(
            telegram=TelegramConfig(
                enabled=True,
                bot_token="FAKE_TOKEN",
                allowed_chat_ids=[],
            )
        )
        with patch.object(TelegramClient, "get_me", return_value={"username": "thirties_bot"}):
            bridge = ThirtiesTelegramBridge(
                config=open_config,
                scheduler=self.scheduler,
                joplin_engine=self.mock_joplin,
                inference_engine=self.mock_inference,
                sync_engine=self.mock_sync,
            )
        bridge.client = MagicMock()
        bridge.handle_message(chat_id=777, text="/start")
        self.assertIn(777, bridge.active_chats)
        bridge.client.send_message.assert_called_once()
        args, _ = bridge.client.send_message.call_args
        self.assertIn("Security Note", args[1])
        self.assertIn("777", args[1])


if __name__ == "__main__":
    unittest.main()
