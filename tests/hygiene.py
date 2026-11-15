"""Test runs write only to temporary folders: never to the repo tree, never to the user's home.

tests/__init__.py calls ``isolate()`` before anything imports thirties_core, so every path the app builds from
``Path.home()`` or the XDG folders (its state database, config, model folder, the agy ledger and workspace) lands in a
temporary home that's removed when the run ends. ``guard_repo()`` notes what's in the repo tree and, when the run ends,
fails it if anything new appeared there (a report, a database, a session folder).

Both apply only under a test runner. The live evals in this package (run as their own modules) write their reports to
docs/eval_reports/ and need the user's real home for the agy sign-in.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Set

ROOT = Path(__file__).resolve().parent.parent

# Folders the run may leave behind or that other tools write while it runs (bytecode, build trees, editor saves).
SKIP_DIRS = {".git", "__pycache__", ".flatpak-builder", "_flatpak_build", "_build", "build", ".venv", "venv",
             ".mypy_cache", ".ruff_cache", ".pytest_cache"}
XDG = {"XDG_CONFIG_HOME": ".config", "XDG_DATA_HOME": ".local/share", "XDG_CACHE_HOME": ".cache",
       "XDG_STATE_HOME": ".local/state"}


def under_test_runner() -> bool:
    """True when unittest (python3 -m unittest ...) or pytest is running the tests."""
    main_package = getattr(sys.modules.get("__main__"), "__package__", None)
    return main_package == "unittest" or "pytest" in sys.modules


def _skipped(name: str) -> bool:
    return (name.endswith((".pyc", ".pyo", "~", ".swp", ".swx")) or name.startswith((".goutputstream", ".#"))
            or name in SKIP_DIRS)


def snapshot(root: Path = ROOT) -> Set[str]:
    """Every file and folder under ``root`` (relative paths), less bytecode, build trees and editor temp files."""
    found: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skipped(d)]
        rel = Path(dirpath).relative_to(root)
        for name in [*dirnames, *filenames]:
            if not _skipped(name):
                found.add(str(rel / name))
    return found


def new_paths(before: Iterable[str], after: Iterable[str]) -> list:
    """What appeared since ``before``, top-most only (a new folder, not each file in it)."""
    added = sorted(set(after) - set(before))
    return [p for p in added if not any(p.startswith(q + os.sep) for q in added if q != p)]


def isolate() -> Path:
    """Point HOME and the XDG folders at a fresh temporary home, removed at exit. Returns that home."""
    base = Path(tempfile.mkdtemp(prefix="thirties_home_"))
    atexit.register(shutil.rmtree, base, True)
    home = base / "home"
    os.environ["HOME"] = str(home)
    for var, sub in XDG.items():
        (home / sub).mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(home / sub)
    os.environ["THIRTIES_TEST_HOME"] = str(home)
    return home


def guard_repo(root: Path = ROOT) -> None:
    """At exit, fail the run (exit status 1) if it left anything new in the repo tree.

    Register this before any other exit handler: handlers run last-registered first, so this one runs after every
    temporary folder has been cleaned up, and its os._exit skips nothing that still had to run."""
    before = snapshot(root)

    def check() -> None:
        left = new_paths(before, snapshot(root))
        if left:
            sys.stdout.flush()
            sys.stderr.write("\nFAILED: the test run wrote into the repo tree (tests write only to temp dirs):\n"
                             + "".join(f"  {p}\n" for p in left))
            sys.stderr.flush()
            os._exit(1)

    atexit.register(check)
