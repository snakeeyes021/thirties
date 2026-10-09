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


if __name__ == "__main__":
    unittest.main()
