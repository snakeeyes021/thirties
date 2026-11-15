"""Every model call runs on the user's own config, never on the built-in defaults, and the agy ledger tells the
truth (11-15: default-model "ok" rows every few minutes, with no agy run behind them)."""

import json
import os
import pwd
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import temp_dir
from thirties_core import config as config_mod
from thirties_core import inference
from thirties_core.config import ThirtiesConfig, get_default_config_path, load_config
from thirties_core.inference import (AntigravityInferenceEngine, GeminiInferenceEngine, LiteRTInferenceEngine,
                                     agy_outcome, agy_ledger, get_inference_engine)

INVENTED = "gemini-invented-model-low"
CONFIG = f'[inference]\ngemini_model = "{INVENTED}"\n\n[general]\nplace_name = "Lakeview"\n'


def agy_answer(cmd, input=None, **kwargs):
    body = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Sure."}})
    return subprocess.CompletedProcess(cmd, 0, stdout=body + "\n", stderr="")


class EnginesNeedAConfigTest(unittest.TestCase):
    def test_no_engine_makes_up_the_defaults(self):
        for build in (AntigravityInferenceEngine, GeminiInferenceEngine, LiteRTInferenceEngine, get_inference_engine):
            with self.subTest(build.__name__), self.assertRaises(ValueError):
                build(None)
        with patch.dict(os.environ, {inference.TEST_BRAIN_ENV: "1"}), self.assertRaises(ValueError):
            inference.ClaudeCliInferenceEngine(None)  # the test brain too

    def test_the_engine_runs_the_model_from_the_config_it_was_given(self):
        cfg = ThirtiesConfig()
        cfg.inference.gemini_model = INVENTED
        seen = {}

        def run(cmd, input=None, **kwargs):
            seen["model"] = cmd[cmd.index("--model") + 1]
            return agy_answer(cmd, input)

        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("thirties_core.inference.subprocess.run", side_effect=run):
            AntigravityInferenceEngine(cfg).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(seen["model"], INVENTED)
        self.assertEqual(agy_ledger(60)[-1][1:], (INVENTED, "ok"))


class AppConfigTest(unittest.TestCase):
    """The app (AppCore) reads the user's config.toml, and everything it builds runs on it."""

    def setUp(self):
        self.home = Path(temp_dir("thirties_home_"))
        env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                                      "XDG_DATA_HOME": str(self.home / ".local/share")})
        env.start()
        self.addCleanup(env.stop)

    def write_config(self, text, where=None):
        path = where or self.home / ".config" / "thirties" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def core(self):
        from thirties_core.app_core import AppCore
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            return AppCore(use_keyring=False)

    def test_the_apps_engine_and_service_use_the_users_model(self):
        path = self.write_config(CONFIG)
        core = self.core()
        self.assertEqual(core.config.loaded_from, path)
        seen = []

        def run(cmd, input=None, **kwargs):
            seen.append(cmd[cmd.index("--model") + 1])
            return agy_answer(cmd, input)

        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("thirties_core.inference.subprocess.run", side_effect=run):
            core.inference_engine.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(seen, [INVENTED])
        with patch("thirties_core.background.BackgroundService.start"):
            core.start_service()
        self.assertIs(core.service.inference_engine, core.inference_engine)
        self.assertIs(core.service.config, core.config)

    def test_an_unreadable_config_is_said_plainly_and_nothing_runs_unseen(self):
        self.write_config('[inference]\ngemini_model = "unterminated\n')
        with self.assertLogs("thirties_core.config", "ERROR") as logs:
            core = self.core()
        self.assertIn("Couldn't read the config file", logs.output[0])
        self.assertTrue(core.config.load_error)
        with self.assertLogs("thirties_core.app_core", "ERROR") as logs, \
                patch("thirties_core.background.BackgroundService.start") as start:
            core.start_service()
        self.assertIsNone(core.service)
        start.assert_not_called()
        self.assertIn("not started", logs.output[0])

    def test_reload_keeps_where_the_config_came_from(self):
        path = self.write_config(CONFIG)
        core = self.core()
        self.write_config('[inference]\ngemini_model = "broken\n')
        with self.assertLogs("thirties_core.config", "ERROR"), \
                patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"):
            core.reload()
        self.assertTrue(core.config.load_error)
        self.assertEqual(core.config.loaded_from, path)

    def test_no_config_file_is_the_defaults_and_not_an_error(self):
        cfg = load_config()
        self.assertIsNone(cfg.loaded_from)
        self.assertIsNone(cfg.load_error)
        self.assertEqual(cfg.inference.gemini_model, ThirtiesConfig().inference.gemini_model)


class FlatpakConfigPathTest(unittest.TestCase):
    """Inside the Flatpak, XDG_CONFIG_HOME is the app's own folder; the host's ~/.config/thirties is the same file
    as for the app started on the host."""

    def setUp(self):
        self.home = Path(temp_dir("thirties_home_"))
        self.app_config = self.home / ".var" / "app" / "tech.redfoxlabs.Thirties" / "config"
        self.app_config.mkdir(parents=True)
        self.host_file = self.home / ".config" / "thirties" / "config.toml"
        self.host_file.parent.mkdir(parents=True)
        self.host_file.write_text(CONFIG)
        env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.app_config)})
        env.start()
        self.addCleanup(env.stop)

    def test_in_the_flatpak_the_hosts_config_is_found(self):
        with patch.object(config_mod, "_in_flatpak", return_value=True):
            self.assertEqual(get_default_config_path(), self.host_file)
            cfg = load_config()
        self.assertEqual(cfg.inference.gemini_model, INVENTED)
        self.assertEqual(cfg.general.place_name, "Lakeview")
        self.assertEqual(cfg.loaded_from, self.host_file)

    def test_the_apps_own_folder_comes_first_when_it_has_the_file(self):
        own = self.app_config / "thirties" / "config.toml"
        own.parent.mkdir(parents=True)
        own.write_text('[general]\nplace_name = "Riverside"\n')
        with patch.object(config_mod, "_in_flatpak", return_value=True):
            self.assertEqual(load_config().general.place_name, "Riverside")

    def test_on_the_host_only_its_own_config_home_counts(self):
        with patch.object(config_mod, "_in_flatpak", return_value=False):
            self.assertEqual(get_default_config_path(), self.app_config / "thirties" / "config.toml")
            self.assertIsNone(load_config().loaded_from)


class LedgerOutcomeTest(unittest.TestCase):
    def setUp(self):
        ledger = patch("thirties_core.inference.AGY_LEDGER", Path(temp_dir("thirties_tmp_")) / "agy_calls.log")
        ledger.start()
        self.addCleanup(ledger.stop)

    @staticmethod
    def done(returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess(["agy"], returncode, stdout=stdout, stderr=stderr)

    def test_ok_only_when_agy_answered(self):
        result = lambda status, **extra: json.dumps({"event": "result", "result": {"status": status, **extra}})
        self.assertEqual(agy_outcome(self.done(0, result("SUCCESS", response="hi"))), "ok")
        self.assertEqual(agy_outcome(self.done(0, json.dumps({"response": "hi"}))), "ok")
        self.assertEqual(agy_outcome(self.done(
            0, result("ERROR", error="RESOURCE_EXHAUSTED: try again in 3h."))), "quota")
        self.assertEqual(agy_outcome(self.done(0, result("ERROR", error="model not found"))), "error")
        self.assertEqual(agy_outcome(self.done(0, "", "error: too many requests")), "quota")
        self.assertEqual(agy_outcome(self.done(0, "not json at all")), "error")
        self.assertEqual(agy_outcome(self.done(3, "", "error: usage limit reached.")), "quota")
        self.assertEqual(agy_outcome(self.done(1, "", "boom")), "error")

    def test_a_quota_result_with_exit_0_is_logged_as_quota(self):
        body = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED"}})
        cfg = ThirtiesConfig()
        cfg.inference.gemini_model = INVENTED
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("thirties_core.inference.subprocess.run",
                      return_value=subprocess.CompletedProcess(["agy"], 0, stdout=body + "\n", stderr="")):
            reply = AntigravityInferenceEngine(cfg).chat([{"role": "user", "content": "hi"}])
        self.assertTrue(reply["content"].startswith("Error contacting model"))
        self.assertEqual([row[1:] for row in agy_ledger(60)], [(INVENTED, "quota")])


class TestRunLedgerTest(unittest.TestCase):
    def test_unit_tests_never_write_the_users_own_ledger(self):
        users = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "thirties" / "agy_calls.log"
        self.assertNotEqual(Path(inference.AGY_LEDGER).resolve(), users.resolve())
        with patch("thirties_core.inference.find_agy_binary", return_value="/mock/bin/agy"), \
                patch("thirties_core.inference.subprocess.run", side_effect=agy_answer):
            AntigravityInferenceEngine(ThirtiesConfig()).chat([{"role": "user", "content": "hi"}])
        self.assertTrue(Path(inference.AGY_LEDGER).is_file())  # the run's own ledger got the fake call,
        last = Path(inference.AGY_LEDGER).read_text().splitlines()[-1].split("\t")
        self.assertEqual(len(last), 4)                          # with who made it
        self.assertTrue(last[3])


if __name__ == "__main__":
    unittest.main()
