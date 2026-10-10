"""Unit tests for thirties_core.config."""

import tempfile
import unittest
from pathlib import Path

from thirties_core.config import ThirtiesConfig, load_config


class TestConfig(unittest.TestCase):

    def test_default_config(self) -> None:
        cfg = load_config(Path("/nonexistent/path/config.toml"))
        self.assertEqual(cfg.general.latitude, 38.8799)
        self.assertEqual(cfg.general.longitude, -77.1067)
        self.assertEqual(cfg.general.timezone, "America/New_York")
        self.assertEqual(cfg.general.work_start_thirty, 17)
        self.assertEqual(cfg.general.work_end_thirty, 31)
        self.assertIn("1. Tasks", cfg.joplin.allowed_notebooks)
        self.assertIn("Archive", cfg.joplin.excluded_notebooks)
        self.assertEqual(cfg.inference.backend, "gemini")
        self.assertEqual(cfg.inference.gemini_model, "gemini-3.8-flash-low")
        self.assertTrue(cfg.sync.enabled)
        self.assertEqual(cfg.sync.notebook_name, "Archive")
        self.assertEqual(cfg.sync.sync_subfolder, "Thirties_System")
        self.assertEqual(cfg.sync.sync_note_title, "Thirties_State_Sync")

    def test_load_custom_toml(self) -> None:
        toml_content = """
[general]
latitude = 40.7128
longitude = -74.0060
timezone = "America/New_York"
work_start_thirty = 18
work_end_thirty = 32

[joplin]
db_path = "/tmp/test-joplin.sqlite"
api_token = "secret123"
api_port = 41185
allowed_notebooks = ["Work", "Personal"]

[inference]
backend = "gemini"
gemini_model = "gemini-3.8-flash-high"
gemini_api_key = "AIzaSyTest"
temperature = 0.5

[sync]
enabled = true
notebook_name = "Archive"
sync_subfolder = "My_Thirties"
sync_note_title = "Sync_Note"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(toml_content)
            temp_path = Path(f.name)

        try:
            cfg = load_config(temp_path)
            self.assertEqual(cfg.general.latitude, 40.7128)
            self.assertEqual(cfg.general.work_start_thirty, 18)
            self.assertEqual(cfg.joplin.db_path, "/tmp/test-joplin.sqlite")
            self.assertEqual(cfg.joplin.api_token, "secret123")
            self.assertEqual(cfg.joplin.api_port, 41185)
            self.assertEqual(cfg.joplin.allowed_notebooks, ["Work", "Personal"])
            self.assertEqual(cfg.inference.backend, "gemini")
            self.assertEqual(cfg.inference.gemini_model, "gemini-3.8-flash-high")
            self.assertEqual(cfg.inference.gemini_api_key, "AIzaSyTest")
            self.assertEqual(cfg.inference.temperature, 0.5)
            self.assertTrue(cfg.sync.enabled)
            self.assertEqual(cfg.sync.sync_subfolder, "My_Thirties")
            self.assertEqual(cfg.sync.sync_note_title, "Sync_Note")
        finally:
            temp_path.unlink()

    def test_resolve_joplin_db_path_explicit(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".sqlite", delete=False) as f:
            db_path = Path(f.name)

        try:
            cfg = ThirtiesConfig()
            cfg.joplin.db_path = str(db_path)
            resolved = cfg.resolve_joplin_db_path()
            self.assertEqual(resolved, db_path)
        finally:
            db_path.unlink()


if __name__ == "__main__":
    unittest.main()
