"""Unit tests for thirties_core.inference."""

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from thirties_core.config import ThirtiesConfig
from thirties_core.inference import (
    AntigravityInferenceEngine,
    GeminiInferenceEngine,
    LiteRTInferenceEngine,
    MockInferenceEngine,
    get_inference_engine,
    parse_model_directives,
    split_thinking_level,
)


def _api_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


class TestDirectiveParsing(unittest.TestCase):

    def test_call_lines_are_extracted_in_order_and_hidden(self) -> None:
        text, calls = parse_model_directives(
            'Sure, moving it.\n'
            'CALL update_event {"event": "abc", "shift": "+1 hour"}\n'
            '```\nCALL add_event {"title": "Laundry", "start": "7:30 PM", "duration": "1 hour"}\n```\n'
            'Done.'
        )
        self.assertEqual(text, "Sure, moving it.\n\nDone.")
        self.assertEqual([c["name"] for c in calls], ["update_event", "add_event"])
        self.assertEqual(calls[1]["arguments"], {"title": "Laundry", "start": "7:30 PM", "duration": "1 hour"})

    def test_call_without_arguments_and_plain_replies(self) -> None:
        self.assertEqual(parse_model_directives("CALL list_schedule")[1][0]["arguments"], {})
        self.assertEqual(parse_model_directives("You're free after 4."), ("You're free after 4.", []))
        # Mentioning a tool in prose is not a call
        self.assertEqual(parse_model_directives("I could CALL you later")[1], [])

    def test_malformed_arguments_are_reported_not_dropped(self) -> None:
        _, calls = parse_model_directives("CALL add_event {title: Gym}")
        self.assertIn("_parse_error", calls[0]["arguments"])


class TestTransports(unittest.TestCase):

    def test_mock_engine_replays_queue_then_plain_text(self) -> None:
        engine = MockInferenceEngine([{"role": "assistant", "content": "first", "tool_calls": []}])
        self.assertEqual(engine.chat([{"role": "user", "content": "hi"}])["content"], "first")
        second = engine.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(second["tool_calls"], [])
        self.assertEqual(len(engine.call_history), 2)

    def test_split_thinking_level(self) -> None:
        self.assertEqual(split_thinking_level("gemini-3.8-flash-low"), ("gemini-3.8-flash", "low"))
        self.assertEqual(split_thinking_level("gemini-3.8-flash"), ("gemini-3.8-flash", None))

    def test_gemini_api_payload_and_parsing(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "fake"
        cfg.inference.gemini_model = "gemini-3.8-flash-low"
        engine = GeminiInferenceEngine(cfg)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "free?"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "list_schedule"}]},
            {"role": "tool", "name": "list_schedule", "content": "Free: 4:00 PM – 11:00 PM"},
        ]
        with patch("urllib.request.urlopen", return_value=_api_response('Sure.\nCALL add_event {"title": "Walk", "start": "5:00 PM", "duration": "30 min"}')) as urlopen:
            res = engine.chat(messages)
        self.assertEqual(res["content"], "Sure.")
        self.assertEqual(res["tool_calls"][0]["name"], "add_event")

        req = urlopen.call_args[0][0]
        self.assertIn("models/gemini-3.8-flash:generateContent", req.full_url)
        self.assertNotIn("fake", req.full_url)  # key travels in a header, not the URL
        payload = json.loads(req.data)
        self.assertEqual(payload["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
        # Empty assistant text is skipped; the tool result reaches the model
        self.assertEqual([c["role"] for c in payload["contents"]], ["user"])
        texts = [p["text"] for p in payload["contents"][0]["parts"]]
        self.assertTrue(any("[Result of list_schedule]" in t for t in texts))

    def test_gemini_api_conversation_may_open_with_the_assistant(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "fake"
        engine = GeminiInferenceEngine(cfg)
        messages = [{"role": "system", "content": "sys"},
                    {"role": "assistant", "content": "Are you attending?"}, {"role": "user", "content": "yes"}]
        with patch("urllib.request.urlopen", return_value=_api_response("Noted.")) as urlopen:
            engine.chat(messages)
        payload = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual([c["role"] for c in payload["contents"]], ["user", "model", "user"])

    def test_gemini_api_retries_rate_limits(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "fake"
        engine = GeminiInferenceEngine(cfg)
        busy = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, BytesIO(b"{}"))
        with patch("urllib.request.urlopen", side_effect=[busy, _api_response("ok")]), patch("time.sleep") as sleep:
            res = engine.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(res["content"], "ok")
        sleep.assert_called_once()

    def test_gemini_api_retries_timeouts(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "fake"
        engine = GeminiInferenceEngine(cfg)
        with patch("urllib.request.urlopen", side_effect=[TimeoutError("read timed out"), _api_response("ok")]), \
                patch("time.sleep"):
            self.assertEqual(engine.chat([{"role": "user", "content": "hi"}])["content"], "ok")

    def test_gemini_api_gives_up_on_client_errors(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "fake"
        engine = GeminiInferenceEngine(cfg)
        bad = urllib.error.HTTPError("u", 400, "Bad Request", {}, BytesIO(b'{"error": "nope"}'))
        with patch("urllib.request.urlopen", side_effect=[bad]), patch("time.sleep") as sleep:
            res = engine.chat([{"role": "user", "content": "hi"}])
        self.assertIn("HTTP 400", res["content"])
        sleep.assert_not_called()

    def setUp(self) -> None:
        ledger = patch("thirties_core.inference.AGY_LEDGER", Path(tempfile.mkdtemp()) / "agy_calls.log")
        self.ledger = ledger.start()
        self.addCleanup(ledger.stop)

    def test_every_agy_call_is_logged_with_its_outcome(self) -> None:
        from thirties_core.inference import agy_ledger
        engine = AntigravityInferenceEngine(ThirtiesConfig())
        quota = MagicMock(returncode=3, stdout="", stderr="error: Individual quota reached. Resets in 4h7m.")
        huge = MagicMock(returncode=0, stderr="",
                         stdout='{"event":"init"}\n{"event":"result","result":{"status":"SUCCESS","response":"ok"}}\n')
        ok = MagicMock(returncode=0, stdout=json.dumps({"response": "hi"}), stderr="")
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", side_effect=[ok, quota]):
            engine.chat([{"role": "user", "content": "a"}])
            engine.chat([{"role": "user", "content": "b"}])
        self.assertEqual([o for _, _, o in agy_ledger(60)], ["ok", "quota"])

    def test_gemini_sources_in_the_users_order_with_fallback(self) -> None:
        from thirties_core.inference import GeminiSourcesEngine
        cfg = ThirtiesConfig()
        cfg.inference.gemini_api_key = "k"
        cfg.inference.sources = ["api", "agy"]
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            engine = GeminiSourcesEngine(cfg)
        self.assertEqual([n for n, _ in engine.engines], ["api", "agy"])
        api, agy = engine.engines[0][1], engine.engines[1][1]
        with patch.object(api, "chat", return_value={"content": "Encountered an error communicating with Gemini API: 429",
                                                    "tool_calls": []}), \
                patch.object(agy, "chat", return_value={"content": "hello", "tool_calls": []}):
            self.assertEqual(engine.chat([{"role": "user", "content": "hi"}])["content"], "hello")
        cfg.inference.sources = ["agy"]  # the user unticked the API: it's never used
        with patch("thirties_core.inference.find_agy_binary", return_value=None):
            self.assertFalse(GeminiSourcesEngine(cfg).is_available())

    def test_antigravity_chat_includes_tool_results(self) -> None:
        engine = AntigravityInferenceEngine(ThirtiesConfig())
        payload = {"response": 'CALL remove_event {"event": "Work"}\nEnjoy the day off.'}
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")) as run:
            res = engine.chat([
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "day off"},
                {"role": "tool", "name": "list_schedule", "content": "Scheduled: Work"},
            ])
        self.assertEqual(res["content"], "Enjoy the day off.")
        self.assertEqual(res["tool_calls"][0]["arguments"], {"event": "Work"})
        cmd = run.call_args[0][0]
        self.assertNotIn("--print", cmd)  # the prompt goes on stdin, never as an argument
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(sent["event"], "user")
        self.assertIn("(Result of list_schedule, for the assistant only: Scheduled: Work)", sent["message"]["content"])
        self.assertIn("--sandbox", cmd)
        self.assertTrue(str(run.call_args.kwargs["cwd"]).endswith("thirties/agy-workspace"))

    def test_antigravity_asked_again_when_it_tries_a_tool_instead_of_answering(self) -> None:
        engine = AntigravityInferenceEngine(ThirtiesConfig())
        empty = MagicMock(returncode=0, stdout=json.dumps({"event": "result", "result": {"response": ""}}),
                          stderr='no output produced — a tool required the "command" permission … auto-denied.')
        answer = MagicMock(returncode=0, stdout=json.dumps({"event": "result", "result": {"response": "Sure."}}),
                           stderr="")
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", side_effect=[empty, answer]) as run:
            res = engine.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(res["content"], "Sure.")
        first, second = (json.loads(c.kwargs["input"])["message"]["content"] for c in run.call_args_list)
        self.assertTrue(first.startswith("(You are answering as a text model: you have no tools here"))
        self.assertTrue(second.endswith("Answer now, in text only.)"))

    def test_antigravity_complete_sends_the_prompt_as_written(self) -> None:
        engine = AntigravityInferenceEngine(ThirtiesConfig())
        answer = MagicMock(returncode=0, stdout=json.dumps({"event": "result", "result": {"response": "hey"}}), stderr="")
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", return_value=answer) as run:
            self.assertEqual(engine.complete("You are role-playing a user. Your next message:"), "hey")
        sent = json.loads(run.call_args.kwargs["input"])["message"]["content"]
        self.assertTrue(sent.endswith("You are role-playing a user. Your next message:"))
        self.assertNotIn("Write the assistant's next message", sent)

    def test_antigravity_timeout(self) -> None:
        engine = AntigravityInferenceEngine(ThirtiesConfig())
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=10)):
            res = engine.chat([{"role": "user", "content": "hello"}])
        self.assertIn("timed out", res["content"])
        self.assertEqual(res["tool_calls"], [])

    def test_litert_availability_detection(self) -> None:
        engine = LiteRTInferenceEngine()
        if not engine.is_available():
            self.skipTest("No LiteRT-LM runtime or Gemma model installed on this machine.")
        self.assertTrue(engine.is_available())

    def test_litert_chat_parses_calls(self) -> None:
        engine = LiteRTInferenceEngine()
        session = MagicMock()
        session.send_message.return_value = 'CALL add_event {"title": "Writing", "start": "8:00 PM", "duration": "1 hour"}\nScheduled.'
        with patch.dict(sys.modules, {"litert_lm": MagicMock()}), \
                patch.object(engine, "_ensure_initialized"), patch.object(engine, "_conv_session", session):
            res = engine.chat([{"role": "user", "content": "writing at 8"}])
        self.assertEqual(res["content"], "Scheduled.")
        self.assertEqual(res["tool_calls"][0]["name"], "add_event")

    def test_get_inference_engine_factory(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.backend = "gemini"
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            engine = get_inference_engine(cfg)
            self.assertIsInstance(engine.engines[0][1], AntigravityInferenceEngine)
        with patch("thirties_core.inference.find_agy_binary", return_value=None):
            cfg.inference.gemini_api_key = "test_key"
            self.assertIsInstance(get_inference_engine(cfg).engines[0][1], GeminiInferenceEngine)

        cfg_api = ThirtiesConfig()
        cfg_api.inference.backend = "gemini_api"
        cfg_api.inference.gemini_api_key = "test_key"
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            self.assertIsInstance(get_inference_engine(cfg_api), GeminiInferenceEngine)

        cfg_litert = ThirtiesConfig()
        cfg_litert.inference.backend = "litert"
        with patch.object(LiteRTInferenceEngine, "is_available", return_value=True):
            self.assertIsInstance(get_inference_engine(cfg_litert), LiteRTInferenceEngine)

        cfg_mock = ThirtiesConfig()
        cfg_mock.inference.backend = "mock"
        self.assertIsInstance(get_inference_engine(cfg_mock), MockInferenceEngine)


if __name__ == "__main__":
    unittest.main()
