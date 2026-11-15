"""Guard: a test run writes only to temporary folders, never to the repo tree or the user's home (tests/hygiene.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from tests import hygiene
from tests.fixtures import temp_dir


def _inside(path, folder) -> bool:
    return Path(path).resolve().is_relative_to(Path(folder).resolve())


class TestTheRunHasItsOwnHome(unittest.TestCase):

    def setUp(self) -> None:
        if not hygiene.under_test_runner():
            self.skipTest("only under a test runner")
        self.home = os.environ.get("THIRTIES_TEST_HOME")
        self.assertTrue(self.home, "tests/__init__.py didn't give the run a temporary home")

    def test_home_and_the_xdg_folders_are_temporary(self) -> None:
        self.assertTrue(_inside(self.home, tempfile.gettempdir()), self.home)
        self.assertEqual(Path.home(), Path(self.home))
        for var in hygiene.XDG:
            self.assertTrue(_inside(os.environ[var], tempfile.gettempdir()), f"{var}={os.environ[var]}")

    def test_the_apps_own_files_land_in_it(self) -> None:
        # Paths fixed when thirties_core is imported: a test module that imported it before the tests package would
        # leave them pointing at the real home.
        from thirties_core.config import ThirtiesConfig, get_default_config_path
        from thirties_core.inference import AGY_LEDGER, agy_workspace_dir
        from thirties_core.joplin_peer import peer_home
        for path in (AGY_LEDGER, agy_workspace_dir(), get_default_config_path(),
                     ThirtiesConfig().resolve_state_db_path(), peer_home()):
            self.assertTrue(_inside(path, self.home), path)


class TestTheRepoGuard(unittest.TestCase):

    def test_it_notices_a_new_file_or_folder_but_not_bytecode(self) -> None:
        root = Path(temp_dir("thirties_guard_"))
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("")
        before = hygiene.snapshot(root)
        (root / "tests" / "__pycache__").mkdir()
        (root / "tests" / "__pycache__" / "test_x.cpython-313.pyc").write_text("")
        (root / "tests" / ".goutputstream-ABC").write_text("")
        self.assertEqual(hygiene.new_paths(before, hygiene.snapshot(root)), [])
        (root / "docs" / "eval_reports" / "sessions").mkdir(parents=True)
        (root / "docs" / "eval_reports" / "sessions" / "s.json").write_text("{}")
        (root / "state.sqlite").write_text("")
        self.assertEqual(hygiene.new_paths(before, hygiene.snapshot(root)), ["docs", "state.sqlite"])


if __name__ == "__main__":
    unittest.main()
