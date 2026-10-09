"""Unit tests for thirties_core.inference."""

import unittest
from unittest.mock import MagicMock, patch

from thirties_core.config import ThirtiesConfig
from thirties_core.inference import LiteRTInferenceEngine, MockInferenceEngine


class TestInference(unittest.TestCase):

    def test_mock_inference_engine(self) -> None:
        engine = MockInferenceEngine()
        resp = engine.chat([{"role": "user", "content": "allocate block 32 to Dorico"}])
        self.assertIn("Dorico", resp["content"])
        self.assertEqual(len(resp["tool_calls"]), 1)
        self.assertEqual(resp["tool_calls"][0]["arguments"]["block_index"], 32)

    def test_litert_availability_detection(self) -> None:
        engine = LiteRTInferenceEngine()
        # On this system with the local Gemma model and host runner, is_available() should be True
        self.assertTrue(engine.is_available())

    def test_litert_intent_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        # Mock the raw generation output
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="---THIRTIES_RESPONSE_START---\nSure, I scheduled that for you.\n---THIRTIES_RESPONSE_END---",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "Please allocate block 24 to Writing Session"}])
                self.assertEqual(res["content"], "Sure, I scheduled that for you.")
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "allocate_thirty_block")
                self.assertEqual(res["tool_calls"][0]["arguments"]["block_index"], 24)
                self.assertEqual(res["tool_calls"][0]["arguments"]["custom_label"], "Writing Session")


    def test_litert_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="---THIRTIES_RESPONSE_START---\nALLOCATE_BLOCK: 38 | Dorico Compose\nI scheduled Dorico in Dark Block 38.\n---THIRTIES_RESPONSE_END---",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "Let's throw some composing in an open dark block"}])
                self.assertEqual(res["content"], "I scheduled Dorico in Dark Block 38.")
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "allocate_thirty_block")
                self.assertEqual(res["tool_calls"][0]["arguments"]["block_index"], 38)
                self.assertEqual(res["tool_calls"][0]["arguments"]["custom_label"], "Dorico Compose")

    def test_multi_block_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
ALLOCATE_BLOCKS: 28-31 | Composing Session
Allocated 2 hours of composing.
---THIRTIES_RESPONSE_END---""",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "Let's go block 28, I'll probably want to go for at least two hours"}])
                self.assertEqual(res["content"], "Allocated 2 hours of composing.")
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "allocate_thirty_block")
                self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 28)
                self.assertEqual(res["tool_calls"][0]["arguments"]["end_block"], 31)
                self.assertEqual(res["tool_calls"][0]["arguments"]["custom_label"], "Composing Session")

    def test_clear_work_blocks_directive_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
CLEAR_WORK_BLOCKS
I have deallocated all your work blocks for today.
---THIRTIES_RESPONSE_END---""",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "I don't actually have work today. Can you deallocate all my work blocks from work?"}])
                self.assertEqual(res["content"], "I have deallocated all your work blocks for today.")
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "clear_blocks")
                self.assertTrue(res["tool_calls"][0]["arguments"]["clear_all_work"])

    def test_duration_user_intent_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                # Model returns generic conversational reply without directives
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
Sounds like a solid plan!
---THIRTIES_RESPONSE_END---""",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "Let's go block 28, I'll probably want to go for at least two hours"}])
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "allocate_thirty_block")
                self.assertEqual(res["tool_calls"][0]["arguments"]["start_block"], 28)
                self.assertEqual(res["tool_calls"][0]["arguments"]["end_block"], 31)

    def test_day_off_user_intent_parsing(self) -> None:
        engine = LiteRTInferenceEngine()
        with patch.object(engine, "_find_host_runner", return_value={"python": "mock_py", "script": "mock_sc"}):
            with patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = MagicMock(
                    stdout="""---THIRTIES_RESPONSE_START---
Enjoy your day off!
---THIRTIES_RESPONSE_END---""",
                    stderr="",
                    returncode=0,
                )
                res = engine.chat([{"role": "user", "content": "I don't actually have work today. Can you deallocate all my work blocks from work?"}])
                self.assertEqual(len(res["tool_calls"]), 1)
                self.assertEqual(res["tool_calls"][0]["name"], "clear_blocks")
                self.assertTrue(res["tool_calls"][0]["arguments"]["clear_all_work"])


if __name__ == "__main__":
    unittest.main()
