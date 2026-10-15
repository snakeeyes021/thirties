"""Unit tests for thirties_core.inference."""

import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from thirties_core.config import ThirtiesConfig
from thirties_core.inference import (
    AntigravityInferenceEngine,
    GeminiInferenceEngine,
    LiteRTInferenceEngine,
    MockInferenceEngine,
    get_inference_engine,
    parse_model_directives,
)


class TestInference(unittest.TestCase):

    def test_mock_inference_engine(self) -> None:
        engine = MockInferenceEngine()
        resp = engine.chat([{"role": "user", "content": "allocate block 32 to Dorico"}])
        self.assertIn("Dorico", resp["content"])
        self.assertEqual(len(resp["tool_calls"]), 1)
        self.assertEqual(resp["tool_calls"][0]["name"], "modify_blocks")
        self.assertEqual(resp["tool_calls"][0]["arguments"]["start_block"], 32)

    def test_litert_availability_detection(self) -> None:
        engine = LiteRTInferenceEngine()
        if not engine.is_available():
            self.skipTest("No LiteRT-LM runtime or Gemma model installed on this machine.")
        self.assertTrue(engine.is_available())

    def test_modify_blocks_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        mock_session = MagicMock()
        mock_session.send_message.return_value = """MODIFY_BLOCKS: 24 | label=Writing Session
Sure, I scheduled that for you."""
        with patch.dict(sys.modules, {"litert_lm": MagicMock()}), \
                patch.object(engine, "_ensure_initialized"), patch.object(engine, "_conv_session", mock_session):
            res = engine.chat([{"role": "user", "content": "Please allocate block 24 to Writing Session"}])
            self.assertEqual(res["content"], "Sure, I scheduled that for you.")
            self.assertEqual(len(res["tool_calls"]), 1)
            self.assertEqual(res["tool_calls"][0]["name"], "modify_blocks")
            self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 24)
            self.assertEqual(res["tool_calls"][0]["arguments"]["label"], "Writing Session")

    def test_multi_block_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        mock_session = MagicMock()
        mock_session.send_message.return_value = """MODIFY_BLOCKS: 28-31 | label=Composing Session
Allocated 2 hours of composing."""
        with patch.dict(sys.modules, {"litert_lm": MagicMock()}), \
                patch.object(engine, "_ensure_initialized"), patch.object(engine, "_conv_session", mock_session):
            res = engine.chat([{"role": "user", "content": "I want to compose from 28 to 31"}])
            self.assertEqual(res["content"], "Allocated 2 hours of composing.")
            self.assertEqual(len(res["tool_calls"]), 1)
            self.assertEqual(res["tool_calls"][0]["name"], "modify_blocks")
            self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 28)
            self.assertEqual(res["tool_calls"][0]["arguments"]["end_block"], 31)

    def test_modify_blocks_clock_time_parsing(self) -> None:
        raw = "MODIFY_BLOCKS: 07:00 AM - 03:00 PM | kind=WORK is_locked=true"
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "modify_blocks")
        self.assertEqual(calls[0]["arguments"]["start_time"], "07:00 AM")
        self.assertEqual(calls[0]["arguments"]["end_time"], "03:00 PM")
        self.assertEqual(calls[0]["arguments"]["kind"], "WORK")
        self.assertTrue(calls[0]["arguments"]["is_locked"])

    def test_modify_blocks_natural_clock_time_parsing(self) -> None:
        raw = "MODIFY_BLOCKS: 7am - 3pm | kind=WORK"
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["start_time"], "7am")
        self.assertEqual(calls[0]["arguments"]["end_time"], "3pm")
        self.assertEqual(calls[0]["arguments"]["kind"], "WORK")

    def test_clear_blocks_kinds_directive_parsing(self) -> None:
        raw = "CLEAR_BLOCKS: ALL\nCLEAR_BLOCKS: TASKS\nCLEAR_BLOCKS: EVENTS"
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["name"], "clear_blocks")
        self.assertEqual(calls[0]["arguments"]["clear_kind"], "ALL")
        self.assertEqual(calls[1]["name"], "clear_blocks")
        self.assertEqual(calls[1]["arguments"]["clear_kind"], "TASKS")
        self.assertEqual(calls[2]["name"], "clear_blocks")
        self.assertEqual(calls[2]["arguments"]["clear_kind"], "EVENTS")

    def test_clear_and_inspect_clock_time_directive_parsing(self) -> None:
        raw = """CLEAR_BLOCKS: 02:00 PM - 03:00 PM
INSPECT_BLOCKS: 10:00 AM - 12:00 PM"""
        clean, calls = parse_model_directives(raw)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["name"], "clear_blocks")
        self.assertEqual(calls[0]["arguments"]["start_time"], "02:00 PM")
        self.assertEqual(calls[0]["arguments"]["end_time"], "03:00 PM")
        self.assertEqual(calls[1]["name"], "inspect_blocks")
        self.assertEqual(calls[1]["arguments"]["start_time"], "10:00 AM")
        self.assertEqual(calls[1]["arguments"]["end_time"], "12:00 PM")

    def test_antigravity_inference_engine_chat(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_model = "gemini-3.8-flash-medium"
        engine = AntigravityInferenceEngine(cfg)

        mock_payload = {
            "conversation_id": "test-123",
            "status": "SUCCESS",
            "response": "MODIFY_BLOCKS: 20 | label=Morning Review\nI have scheduled your morning review.",
        }

        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps(mock_payload),
                    stderr="",
                )

                messages = [
                    {"role": "system", "content": "You are a scheduler."},
                    {"role": "user", "content": "Schedule morning review in block 20."},
                ]
                res = engine.chat(messages)

                self.assertEqual(res["content"], "I have scheduled your morning review.")
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "modify_blocks")
                self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 20)
                self.assertEqual(res["tool_calls"][0]["arguments"]["label"], "Morning Review")

    def test_antigravity_inference_engine_timeout(self) -> None:
        cfg = ThirtiesConfig()
        engine = AntigravityInferenceEngine(cfg)

        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=10)):
                res = engine.chat([{"role": "user", "content": "hello"}])
                self.assertIn("timed out", res["content"])
                self.assertEqual(res["tool_calls"], [])

    def test_gemini_inference_engine_chat(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "AIzaSyFakeKey"
        engine = GeminiInferenceEngine(cfg)
        self.assertTrue(engine.is_available())

        api_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "MODIFY_BLOCKS: 15-18 | kind=WORK\nLocked in your work hours."}
                        ]
                    }
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = engine.chat([{"role": "user", "content": "Work is 15-18"}])
            self.assertEqual(res["content"], "Locked in your work hours.")
            self.assertEqual(len(res["tool_calls"]), 1)
            self.assertEqual(res["tool_calls"][0]["name"], "modify_blocks")
            self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 15)
            self.assertEqual(res["tool_calls"][0]["arguments"]["end_block"], 18)
            self.assertEqual(res["tool_calls"][0]["arguments"]["kind"], "WORK")

    def test_get_inference_engine_factory(self) -> None:
        # 1. Antigravity backend available
        cfg = ThirtiesConfig()
        cfg.inference.backend = "gemini"
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            engine = get_inference_engine(cfg)
            self.assertIsInstance(engine, AntigravityInferenceEngine)

        # 2. Gemini direct API when agy missing
        with patch("thirties_core.inference.find_agy_binary", return_value=None):
            cfg.inference.gemini_api_key = "test_key"
            engine = get_inference_engine(cfg)
            self.assertIsInstance(engine, GeminiInferenceEngine)

        # 3. LiteRT backend
        cfg_litert = ThirtiesConfig()
        cfg_litert.inference.backend = "litert"
        with patch.object(LiteRTInferenceEngine, "is_available", return_value=True):
            engine_litert = get_inference_engine(cfg_litert)
        self.assertIsInstance(engine_litert, LiteRTInferenceEngine)

        # 4. Mock backend
        cfg_mock = ThirtiesConfig()
        cfg_mock.inference.backend = "mock"
        engine_mock = get_inference_engine(cfg_mock)
        self.assertIsInstance(engine_mock, MockInferenceEngine)


if __name__ == "__main__":
    unittest.main()
