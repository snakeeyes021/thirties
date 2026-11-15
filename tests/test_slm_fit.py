"""Fitting the on-device model to the device (brains/slm/fit.py): the check before loading, the marker around model
use, telling out-of-memory from a crash, the offer of the smaller model, and the safe swap. No model is loaded."""

import json
import os
import signal
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import make_scheduler
from thirties_core.brains.slm import fit, model_download as md

GB = 1024 ** 3


class MemoryTest(unittest.TestCase):
    def test_preflight_says_when_it_plainly_wont_fit(self):
        with tempfile.TemporaryDirectory() as d:
            model = Path(d) / "m.litertlm"
            with open(model, "wb") as f:
                f.truncate(3 * GB)  # sparse: takes no space
            self.assertIsNotNone(fit.preflight(model, info={"MemTotal": 8 * GB, "MemAvailable": 2 * GB}))
            self.assertIsNone(fit.preflight(model, info={"MemTotal": 16 * GB, "MemAvailable": 10 * GB}))

    def test_small_device(self):
        self.assertTrue(fit.small_device({"MemTotal": 6 * GB}))
        self.assertFalse(fit.small_device({"MemTotal": 16 * GB}))
        self.assertFalse(fit.small_device({}))  # unknown: no offer

    def test_meminfo_and_pressure_parse(self):
        with tempfile.TemporaryDirectory() as d:
            mi, pr = Path(d) / "meminfo", Path(d) / "pressure"
            mi.write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
            pr.write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=1.00 avg60=0 avg300=0 total=1\n")
            self.assertEqual(fit.meminfo(mi), {"MemTotal": 16000000 * 1024, "MemAvailable": 4000000 * 1024})
            self.assertEqual(fit.pressure(pr), 12.5)


class ExitTest(unittest.TestCase):
    def test_how_a_runner_ended(self):
        self.assertEqual(fit.classify_exit(0), "ok")
        self.assertEqual(fit.classify_exit(1, "RuntimeError: std::bad_alloc"), "memory")
        self.assertEqual(fit.classify_exit(-signal.SIGKILL, exhausted=True), "memory")
        self.assertEqual(fit.classify_exit(137, exhausted=False), "unexplained")
        self.assertEqual(fit.classify_exit(-signal.SIGSEGV, "Segmentation fault"), "crash")
        self.assertEqual(fit.classify_exit(1, "Traceback ... KeyError: 'x'"), "crash")


class MarkerAndOfferTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": self.dir.name})
        env.start()
        self.addCleanup(env.stop)
        self.marker = fit.Marker(Path(self.dir.name) / "marker.json")
        self.record = fit.FitRecord(make_scheduler().state_db)

    def test_the_marker_is_cleared_after_use(self):
        with fit.Using("m", "generate", self.marker):
            self.assertTrue(self.marker.path.exists())
        self.assertFalse(self.marker.path.exists())

    def test_a_marker_from_a_run_that_died_counts_once(self):
        self.marker.path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "model": "m", "phase": "generate"}))
        with patch.object(fit, "nearly_exhausted", return_value=False):
            self.assertEqual(self.record.check_startup(self.marker), "unexplained")
            self.assertIsNone(self.record.check_startup(self.marker))  # cleared: not counted again

    def test_our_own_marker_isnt_a_death(self):
        self.marker.begin("m", "load")
        self.assertIsNone(self.marker.left_behind())

    def test_offer_after_one_memory_failure_or_two_deaths_and_at_most_weekly(self):
        self.assertFalse(self.record.should_offer())
        self.record.record("unexplained")
        self.assertFalse(self.record.should_offer())
        self.record.record("unexplained")
        self.assertTrue(self.record.should_offer())
        now = datetime.now(timezone.utc)
        self.record.declined(now)
        self.assertFalse(self.record.should_offer(now + timedelta(days=3)))
        self.assertTrue(self.record.should_offer(now + timedelta(days=8)))
        self.assertFalse(self.record.should_offer(current_model=fit.SMALL_MODEL))  # already on it

    def test_a_crash_never_leads_to_the_offer(self):
        self.record.record("crash")
        self.record.record("crash")
        self.assertFalse(self.record.should_offer())

    def test_events_noted_without_a_database_are_recorded_at_the_check(self):
        fit.note_event("memory", "needs 4 GB; 2 GB available")
        self.assertEqual(self.record.check_startup(self.marker), "memory")
        self.assertTrue(self.record.should_offer())


class SwapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name)
        for name in (md.GEMMA_E4B.filename, md.GEMMA_E4B_CPU.filename):
            (self.folder / name).write_bytes(b"big")
        self.settings = {}

    def fake_download(self, spec, progress=None, cancel=None, dest_dir=None):
        path = Path(dest_dir) / spec.filename
        path.write_bytes(b"small")
        return path

    def test_the_big_model_goes_only_after_the_small_one_passes_its_test(self):
        out = fit.swap(True, self.settings.__setitem__, gpu=True, download=self.fake_download, test=lambda p: False,
                       dest_dir=self.folder)
        self.assertIn("didn't pass its test", out)
        self.assertTrue((self.folder / md.GEMMA_E4B.filename).exists())
        self.assertEqual(self.settings, {})
        out = fit.swap(True, self.settings.__setitem__, gpu=True, download=self.fake_download, test=lambda p: True,
                       dest_dir=self.folder)
        self.assertIn("Now using Gemma 4 E2B", out)
        self.assertFalse((self.folder / md.GEMMA_E4B.filename).exists())
        self.assertTrue((self.folder / md.GEMMA_E2B.filename).exists())
        self.assertEqual(self.settings, {"assistant.slm_model": fit.SMALL_MODEL})

    def test_downloads_come_from_a_fixed_revision(self):
        for spec in (md.GEMMA_E4B, md.GEMMA_E4B_CPU, md.GEMMA_E2B, md.GEMMA_E2B_CPU):
            self.assertNotIn("/resolve/main/", spec.url)
            self.assertTrue(spec.url.endswith(spec.filename))
            self.assertEqual(len(spec.sha256), 64)


if __name__ == "__main__":
    unittest.main()
