"""The test-only brain on Claude (inference.ClaudeCliInferenceEngine, backend "claude_cli"): the same CALL-line
prompt and parser as the agy engine, with the claude command underneath; refused outside a test or simulation.
A fake `claude` binary stands in for the real one; invented messages only."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thirties_core import privacy
from thirties_core.config import ThirtiesConfig
from thirties_core.inference import (CLAUDE_CLI_FLAGS, TEST_BRAIN_ENV, ClaudeCliInferenceEngine,
                                     MockInferenceEngine, TestBrainRefused, get_inference_engine)
from thirties_core.privacy import Shield
from thirties_core.turns import transport_failure

FAKE = """#!/bin/bash
printf '%s\\n' "$@" > "$FAKE_DIR/argv"
pwd > "$FAKE_DIR/cwd"
ls -A > "$FAKE_DIR/ls"
cat > "$FAKE_DIR/stdin"
cat "$FAKE_DIR/reply"
echo "$FAKE_ERR" >&2
exit "${FAKE_EXIT:-0}"
"""


class ClaudeCliCase(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.binary = self.dir / "claude"
        self.binary.write_text(FAKE)
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.reply('CALL add_event {"title": "Repot the fern", "start": "5:30 PM"}\nThe fern is at 5:30.')
        env = patch.dict(os.environ, {TEST_BRAIN_ENV: "1", "FAKE_DIR": str(self.dir), "FAKE_EXIT": "0",
                                      "FAKE_ERR": "", "XDG_CACHE_HOME": str(self.dir / "cache")})
        env.start()
        self.addCleanup(env.stop)

    def reply(self, text: str) -> None:
        (self.dir / "reply").write_text(text)

    def engine(self) -> ClaudeCliInferenceEngine:
        return ClaudeCliInferenceEngine(ThirtiesConfig(), binary=str(self.binary))

    def sent(self, name: str) -> str:
        return (self.dir / name).read_text()


class TestTheClaudeBrain(ClaudeCliCase):

    def test_it_asks_like_the_agy_engine_and_reads_the_calls_the_same_way(self) -> None:
        out = self.engine().chat([{"role": "system", "content": "Plan the day."},
                                  {"role": "user", "content": "fern at 5:30"}])
        self.assertEqual(out["content"], "The fern is at 5:30.")
        self.assertEqual(out["tool_calls"][0]["name"], "add_event")
        self.assertEqual(out["tool_calls"][0]["arguments"]["start"], "5:30 PM")
        prompt = self.sent("stdin")  # on stdin, never on the command line
        self.assertIn("SYSTEM INSTRUCTIONS:\nPlan the day.", prompt)
        self.assertIn("User: fern at 5:30", prompt)
        self.assertTrue(prompt.rstrip().endswith("Assistant:"))
        self.assertNotIn("fern at 5:30", self.sent("argv"))

    def test_it_runs_as_a_bare_text_model_in_an_empty_directory(self) -> None:
        self.engine().chat([{"role": "user", "content": "hello"}])
        argv = self.sent("argv").splitlines()
        self.assertEqual(argv[:3], ["-p", "--model", "claude-sonnet-5-5"])
        self.assertEqual(argv[3:], CLAUDE_CLI_FLAGS)
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # no tools
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")  # no settings
        self.assertEqual(self.sent("ls").strip(), "")  # nothing there to read
        self.assertEqual(self.sent("cwd").strip(), str(self.dir / "cache" / "thirties" / "claude-workspace"))

    def test_the_privacy_shield_filters_what_leaves_and_restores_the_reply(self) -> None:
        self.reply('CALL add_item {"title": "Pick up [medication]"}\nAdded [medication].')
        with patch.object(privacy, "SHIELD", Shield()):
            out = self.engine().chat([{"role": "user", "content": "add pick up escitalopram, card 4111 1111 1111 1111"}])
        self.assertNotIn("escitalopram", self.sent("stdin"))
        self.assertNotIn("4111 1111 1111 1111", self.sent("stdin"))
        self.assertEqual(out["content"], "Added escitalopram.")
        self.assertEqual(out["tool_calls"][0]["arguments"]["title"], "Pick up escitalopram")

    def test_a_failure_reads_as_one_so_the_turn_retries_it(self) -> None:
        os.environ.update({"FAKE_EXIT": "1", "FAKE_ERR": "the usage limit is reached"})
        self.reply("")
        out = self.engine().chat([{"role": "user", "content": "hello"}])
        self.assertTrue(out["content"].startswith("Error contacting model via Claude CLI"))
        self.assertIn("usage limit", out["content"])
        self.assertEqual(out["tool_calls"], [])
        self.assertTrue(transport_failure(out))  # never shown to the user as a reply

    def test_a_timeout_reads_as_one_too(self) -> None:
        import subprocess
        with patch("thirties_core.inference.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
            out = self.engine().chat([{"role": "user", "content": "hello"}])
        self.assertTrue(transport_failure(out))

    def test_complete_for_a_person_or_a_judge(self) -> None:
        self.reply("A short, plain answer.")
        self.assertEqual(self.engine().complete("Say something."), "A short, plain answer.")
        self.assertIn("Say something.", self.sent("stdin"))


class TestOnlyInTests(ClaudeCliCase):

    def test_refused_without_the_test_switch(self) -> None:
        with patch.dict(os.environ, {TEST_BRAIN_ENV: ""}):
            with self.assertRaises(TestBrainRefused):
                self.engine()

    def test_a_config_asking_for_it_gets_the_usual_brain_instead(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.backend = "claude_cli"
        with patch.dict(os.environ, {TEST_BRAIN_ENV: ""}):
            engine = get_inference_engine(cfg)
        self.assertNotIsInstance(engine, ClaudeCliInferenceEngine)
        self.assertEqual(cfg.inference.backend, "claude_cli")  # the caller's config isn't changed

    def test_with_the_switch_the_factory_gives_it(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.backend = "claude_cli"
        self.assertIsInstance(get_inference_engine(cfg), ClaudeCliInferenceEngine)

    def test_never_offered_to_choose(self) -> None:
        from thirties_core.brains import BRAINS
        from thirties_core.settings import SETTINGS
        self.assertFalse(any("claude" in (k + v).lower() for k, v in BRAINS.items()))
        self.assertNotIn("inference.backend", SETTINGS)  # no setting, so no row in Preferences

    def test_mock_is_still_the_tests_default(self) -> None:
        cfg = ThirtiesConfig()
        cfg.inference.backend = "mock"
        self.assertIsInstance(get_inference_engine(cfg), MockInferenceEngine)


if __name__ == "__main__":
    unittest.main()
