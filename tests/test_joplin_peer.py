"""The headless Joplin sync peer, with fakes only (no real Joplin, no network): which copy of Joplin is used, the
outbox draining through the peer, the sync schedule, reading the desktop app's sync settings, and failures
(the peer down, a sync conflict, a locked target, a sign-in or password needed)."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
import unittest.mock
from unittest.mock import MagicMock

from tests.fixtures import make_scheduler
from thirties_core import joplin_peer as jp
from thirties_core.config import ThirtiesConfig
from thirties_core.joplin_peer import (CliResult, JoplinPeer, JoplinRouter, Presence, WriteFence, classify,
                                       describe, node_flags, read_desktop_target, reading_db_path)
from thirties_core.joplin_writeback import JoplinWriter

# What the real CLI printed (3.7.1, a throwaway profile on a local folder target, 2026-10-30).
SYNC_OK = ("Synchronization target:  (2)\nStarting synchronization...\nCreated remote items: 1.\n"
           "Downloading resources...\nCreated remote items: 2. Fetched items: 2/2. Completed: 30/10/2026 21:53 (244ms)")
DROPBOX_AUTH = ("To allow Joplin to synchronise with Dropbox, please follow the steps below:\n"
                "Step 1: Open this URL in your browser to authorise the application:\n"
                "https://www.dropbox.com/oauth2/authorize?response_type=code&client_id=abc\n"
                "Step 2: Enter the code provided by Dropbox:\n"
                "Authentication was not completed (did not receive an authentication token).")
LOCKED = ("Synchronization target:  (6)\nStarting synchronization...\nCompleted: 30/10/2026 22:00 (130ms)\n"
          "Last error: Error: Cannot acquire sync lock because the following client has an exclusive lock on the "
          "sync target: (Desktop #0123)")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeAPI:
    """A Joplin Data API (the desktop app's or the peer's): notes with updated_time, ``up`` says it's running."""

    def __init__(self, notes=None, up=False, clock=None):
        self.notes = {k: dict(v) for k, v in (notes or {}).items()}
        self.up = up
        self.updates = []
        self.clock = clock or Clock()
        self.token = "t"

    def ping(self):
        return self.up

    def get_note(self, note_id, fields=""):
        return dict(self.notes[note_id]) if self.up and note_id in self.notes else None

    def update_note(self, note_id, data):
        if not self.up or note_id not in self.notes:
            return False
        self.notes[note_id].update(data)
        self.notes[note_id]["updated_time"] = int(self.clock() * 1000) + len(self.updates)
        self.updates.append((note_id, data))
        return True

    def create_note(self, title, body, parent_id=None):
        if not self.up:
            return None
        nid = f"new{len(self.notes)}"
        self.notes[nid] = {"id": nid, "title": title, "body": body, "updated_time": int(self.clock() * 1000)}
        return dict(self.notes[nid])

    def get_folders(self):
        return []

    def get_folder_notes(self, folder_id):
        return []

    def create_folder(self, title, parent_id=None):
        return {"id": "f1", "title": title} if self.up else None

    def search_notes(self, query, fields=""):
        return []


class FakeProc:
    def __init__(self, on_stop=None, lines=(), rc=0):
        self.returncode = None
        self.on_stop = on_stop
        self._lines = list(lines)
        self.rc = rc
        self.stdin = io.StringIO()
        self.stdout = self

    def readline(self):
        if self._lines:
            return self._lines.pop(0) + "\n"
        return ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.rc
        return self.returncode

    def terminate(self):
        self.returncode = -15
        if self.on_stop:
            self.on_stop()

    kill = terminate


class FakeRunner:
    """Node, npm and the Joplin CLI, scripted. ``syncs`` is the queue of what each ``sync`` prints."""

    def __init__(self, peer_api, node_version="v22.12.0"):
        self.peer_api = peer_api
        self.node_version = node_version
        self.calls = []
        self.imports = []
        self.syncs = []
        self.on_sync = None
        self.spawned = []
        self.killed = []
        self.server_fails = False

    def _joplin_args(self, cmd):
        i = cmd.index("--profile")
        return cmd[i + 2:]

    def run(self, cmd, timeout=60, input=None, env=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[-1] == "--version":
            if "/peer/node/" in cmd[0] and not Path(cmd[0]).exists():  # Thirties' own Node, not downloaded
                return CliResult(127, "not found")
            return CliResult(0, self.node_version) if self.node_version else CliResult(127, "not found")
        if cmd[:2] == ["sh", "-lc"]:
            if "command -v node" in cmd[2]:
                return CliResult(0, "/usr/bin/node") if self.node_version else CliResult(1, "")
            return CliResult(0, "/usr/lib/node_modules/npm/bin/npm-cli.js")
        if "install" in cmd and "--prefix" in cmd:
            prefix = Path(cmd[cmd.index("--prefix") + 1])
            pkg = prefix / "node_modules" / "joplin"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "package.json").write_text(json.dumps({"version": "3.7.1"}))
            (pkg / "main.js").write_text("")
            return CliResult(0, "added 684 packages in 19s")
        args = self._joplin_args(cmd)
        if args[:2] == ["config", "--import-file"]:
            path = Path(args[2])
            self.imports.append((json.loads(path.read_text()), oct(path.stat().st_mode & 0o777)))
            return CliResult(0, "")
        if args == ["sync"]:
            if self.on_sync:
                self.on_sync()
            return self.syncs.pop(0) if self.syncs else CliResult(0, SYNC_OK)
        if args[:2] == ["e2ee", "decrypt"]:
            return CliResult(0, "Completed decryption.")
        return CliResult(0, "")

    def spawn(self, cmd, log_path=None, pipes=False):
        cmd = list(cmd)
        self.spawned.append(self._joplin_args(cmd))
        if pipes:
            return self.sign_in_proc
        if self.server_fails:
            proc = FakeProc(rc=1)
            proc.returncode = 1
            return proc
        self.peer_api.up = True
        return FakeProc(on_stop=lambda: setattr(self.peer_api, "up", False))

    def kill(self, pid):
        self.killed.append(pid)
        self.peer_api.up = False


def make_desktop_profile(root, target=5, settings=None, db_settings=None, e2ee=False):
    """A sample desktop Joplin profile: settings.json (public settings) and the database's settings table."""
    root.mkdir(parents=True, exist_ok=True)
    data = {"$schema": "https://joplinapp.org/schema/settings.json", "sync.target": target,
            "sync.5.path": "https://cloud.example.com/remote.php/webdav/Joplin", "sync.5.username": "matt",
            "locale": "en_GB", "theme": 2}
    data.update(settings or {})
    (root / "settings.json").write_text(json.dumps(data, indent=2))
    conn = sqlite3.connect(root / "database.sqlite")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE notes (id TEXT, title TEXT, is_conflict INT)")
    rows = {"sync.5.password": "hunter2", "sync.7.auth": "{\"access_token\": \"secret\"}",
            "sync.5.context": "{}", "syncInfoCache": json.dumps({"e2ee": {"value": e2ee}})}
    rows.update(db_settings or {})
    conn.executemany("INSERT INTO settings VALUES (?, ?)", list(rows.items()))
    conn.commit()
    conn.close()
    return root / "database.sqlite"


class PeerCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="thirties_peer_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = unittest.mock.patch.object(Path, "home", return_value=self.tmp / "home")  # never the real Joplin
        home.start()
        self.addCleanup(home.stop)
        env = unittest.mock.patch.dict(os.environ, {"THIRTIES_JOPLIN_PEER_HOME": str(self.tmp / "peer-default")})
        env.start()
        self.addCleanup(env.stop)
        self.desktop_db = make_desktop_profile(self.tmp / "joplin-desktop")
        self.cfg = ThirtiesConfig()
        self.cfg.joplin.db_path = str(self.desktop_db)
        self.cfg.joplin.write_back = True
        self.cfg.joplin.api_token = "desk-token"
        self.cfg.joplin.peer_enabled = True
        self.cfg.joplin.peer_token = "peer-token"
        self.clock = Clock()
        self.desktop = FakeAPI(up=False, clock=self.clock)
        self.peer_api = FakeAPI(up=False, clock=self.clock)
        self.runner = FakeRunner(self.peer_api)
        self.peer = JoplinPeer(self.cfg, home=self.tmp / "peer", runner=self.runner, desktop=self.desktop,
                               peer_api=self.peer_api, clock=self.clock)
        self.ready, self.told = [], []
        self.peer.on_ready = lambda: self.ready.append(True)
        self.peer.notify = self.told.append

    def install(self):
        self.assertEqual(self.peer.install(), (True, "Installed Joplin 3.7.1."))

    def peer_db(self, conflicts=(), encrypted=0):
        self.peer.profile.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.peer.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT, title TEXT, is_conflict INT, "
                     "encryption_applied INT DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS folders (id TEXT, encryption_applied INT DEFAULT 0)")
        conn.executemany("INSERT INTO notes (id, title, is_conflict) VALUES (?, ?, 1)", list(conflicts))
        conn.executemany("INSERT INTO folders VALUES (?, 1)", [(f"e{i}",) for i in range(encrypted)])
        conn.commit()
        conn.close()


class TestNodeAndInstall(PeerCase):

    def test_which_nodes_can_run_the_cli(self):
        self.assertEqual(node_flags((22, 12, 0)), [])
        self.assertEqual(node_flags((24, 1, 0)), [])
        self.assertEqual(node_flags((20, 19, 1)), [])
        self.assertEqual(node_flags((20, 18, 0)), ["--experimental-require-module"])  # this computer's, verified
        self.assertEqual(node_flags((22, 3, 0)), ["--experimental-require-module"])
        self.assertIsNone(node_flags((18, 20, 0)))
        self.assertIsNone(node_flags((21, 7, 0)))

    def test_no_node_is_explained(self):
        self.runner.node_version = ""
        self.assertEqual(self.peer.install_state(), "no_node")
        ok, msg = self.peer.install()
        self.assertFalse(ok)
        self.assertIn("Node.js isn't installed", msg)

    def test_a_node_too_old_is_named(self):
        self.runner.node_version = "v18.19.0"
        node, why = self.peer.node()
        self.assertIsNone(node)
        self.assertIn("Node 18.19.0 is too old", why)

    def test_install_into_its_own_prefix_then_the_cli_runs_with_the_right_flags(self):
        self.runner.node_version = "v20.18.0"
        self.assertEqual(self.peer.install_state(), "not_installed")
        self.install()
        npm = self.runner.calls[-1]
        self.assertEqual(npm[:2], ["/usr/bin/node", "/usr/lib/node_modules/npm/bin/npm-cli.js"])
        self.assertIn(str(self.tmp / "peer" / "npm"), npm)
        self.assertEqual(self.peer.install_state(), "ready")
        self.peer.cli(["version"])
        cmd = self.runner.calls[-1]
        self.assertEqual(cmd[:3], ["/usr/bin/node", "--experimental-require-module", "--no-warnings"])
        self.assertEqual(cmd[-3:], ["--profile", str(self.peer.profile), "version"])

    def test_the_private_node_download_is_checked_against_its_checksum(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tar:
            data = b"#!/bin/sh\necho v22.21.0\n"
            info = tarfile.TarInfo("node-v22.21.0-linux-x64/bin/node")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        digest = hashlib.sha256(blob).hexdigest()

        class Resp(io.BytesIO):
            headers = {"Content-Length": str(len(blob))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fetch(url, timeout=0, good=True):
            if url.endswith("SHASUMS256.txt"):
                sums = f"{digest if good else '0' * 64}  node-v22.21.0-linux-x64.tar.xz\n" \
                       f"{'1' * 64}  node-v22.21.0-linux-arm64.tar.xz\n"
                return io.BytesIO(sums.encode())
            return Resp(blob)

        with unittest.mock.patch("platform.machine", return_value="x86_64"):
            ok, msg = self.peer.download_node(fetch=lambda u, timeout=0: fetch(u, timeout, good=False))
            self.assertFalse(ok)
            self.assertIn("checksum", msg)
            self.assertFalse((self.peer.home / "node").exists())
            progress = []
            ok, msg = self.peer.download_node(progress=lambda d, t: progress.append((d, t)), fetch=fetch)
        self.assertTrue(ok, msg)
        self.assertTrue((self.peer.home / "node" / "bin" / "node").is_file())
        self.assertEqual(progress[-1], (len(blob), len(blob)))


class TestDesktopSettings(PeerCase):

    def test_the_target_and_its_plain_settings_are_read_never_its_secrets(self):
        desk = read_desktop_target(self.desktop_db.parent)
        self.assertEqual((desk.target, desk.label), (5, "Nextcloud"))
        self.assertEqual(desk.values, {"path": "https://cloud.example.com/remote.php/webdav/Joplin", "username": "matt"})
        self.assertNotIn("hunter2", json.dumps(desk.__dict__))
        self.assertFalse(desk.e2ee)

    def test_older_joplin_kept_everything_in_the_database(self):
        root = self.tmp / "old"
        db = make_desktop_profile(root, settings={}, db_settings={"sync.target": "9", "sync.9.path":
                                                                   "https://joplin.example.com",
                                                                   "sync.9.username": "me@example.com"})
        (root / "settings.json").unlink()
        desk = read_desktop_target(db.parent)
        self.assertEqual(desk.target, 9)
        self.assertEqual(desk.values, {"path": "https://joplin.example.com", "username": "me@example.com"})

    def test_encryption_is_noticed(self):
        db = make_desktop_profile(self.tmp / "enc", e2ee=True)
        self.assertTrue(read_desktop_target(db.parent).e2ee)

    def test_no_desktop_app(self):
        self.assertEqual(read_desktop_target(None).target, 0)
        self.assertFalse(read_desktop_target(self.tmp / "nowhere").found)

    def test_what_the_user_must_still_give(self):
        self.assertEqual(self.peer.needs(), ["password"])
        self.cfg.joplin.peer_password = "hunter2"
        self.assertEqual(self.peer.needs(), [])
        self.cfg.joplin.peer_target = 7  # Dropbox: a sign-in of the peer's own
        self.assertEqual(self.peer.needs(), ["sign_in"])
        self.cfg.joplin.peer_target, self.cfg.joplin.db_path = 0, str(self.tmp / "none" / "database.sqlite")
        self.assertEqual(self.peer.needs(), ["target"])  # no desktop app to read, nothing chosen
        self.cfg.joplin.peer_target, self.cfg.joplin.peer_target_values = 6, {"path": "https://dav.example.com"}
        self.assertEqual(self.peer.needs(), ["username"])
        enc = make_desktop_profile(self.tmp / "enc", e2ee=True)
        self.cfg.joplin.db_path, self.cfg.joplin.peer_target, self.cfg.joplin.peer_target_values = str(enc), 0, {}
        self.assertEqual(self.peer.needs(), ["master_password"])

    def test_configure_goes_through_a_private_file_and_only_when_something_changed(self):
        self.install()
        self.assertFalse(self.peer.configure()[0])  # the password isn't there yet
        self.cfg.joplin.peer_password = "hunter2"
        self.assertTrue(self.peer.configure()[0])
        values, mode = self.runner.imports[-1]
        self.assertEqual(values, {"sync.target": 5, "sync.5.path": "https://cloud.example.com/remote.php/webdav/Joplin",
                                  "sync.5.username": "matt", "sync.5.password": "hunter2", "api.port": 41210,
                                  "api.token": "peer-token", "locale": "en_US"})
        self.assertEqual(mode, "0o600")
        self.assertFalse((self.peer.home / "import.json").exists())
        self.assertFalse(any("hunter2" in " ".join(c) for c in self.runner.calls))  # never on a command line
        self.assertNotIn("hunter2", (self.peer.home / "status.json").read_text())
        self.peer.configure()
        self.assertEqual(len(self.runner.imports), 1)  # nothing changed
        self.cfg.joplin.peer_password = "correct horse"
        self.peer.configure()
        self.assertEqual(len(self.runner.imports), 2)


class TestOutput(unittest.TestCase):

    def test_what_the_cli_said(self):
        self.assertTrue(classify(CliResult(0, SYNC_OK)).ok)
        self.assertEqual(classify(CliResult(0, DROPBOX_AUTH)).kind, "auth")
        self.assertEqual(classify(CliResult(0, LOCKED)).kind, "locked")
        self.assertEqual(classify(CliResult(0, "Synchronisation is already in progress.")).kind, "busy")
        self.assertEqual(classify(CliResult(0, "/!\\ Sync target must be upgraded! Run `sync --upgrade`")).kind,
                         "upgrade_target")
        self.assertEqual(classify(CliResult(0, SYNC_OK + "\n/!\\ Your password is needed to decrypt some of your "
                                                         "data. Type `:e2ee decrypt` to set it.")).kind, "password")
        out = classify(CliResult(1, "Error: getaddrinfo ENOTFOUND cloud.example.com"))
        self.assertEqual(out.kind, "error")
        self.assertIn("ENOTFOUND", out.message)


class TestSyncSchedule(PeerCase):

    def setUp(self):
        super().setUp()
        self.cfg.joplin.peer_password = "hunter2"
        self.install()
        self.peer_db()

    def test_first_round_syncs_then_serves_and_the_outbox_is_told(self):
        self.assertEqual(self.peer.tick(), "synced")
        self.assertTrue(self.peer_api.up)
        self.assertEqual(self.ready, [True])
        self.assertEqual(self.runner.spawned, [["server", "start", "--quiet"]])
        st = self.peer.status
        self.assertEqual((st["result"], st["last_ok_sync"], st["next_sync"]),
                         ("ok", self.clock.t, self.clock.t + jp.SYNC_EVERY))
        self.assertEqual(self.peer.tick(), "idle")

    def test_a_write_is_synced_soon_and_the_server_never_runs_during_a_sync(self):
        self.peer.tick()
        served_during_sync = []
        self.runner.on_sync = lambda: served_during_sync.append(self.peer_api.up)
        self.peer.note_write()
        self.clock.advance(5)
        self.assertEqual(self.peer.tick(), "idle")  # the rest of the turn's writes come first
        self.clock.advance(jp.SYNC_AFTER_WRITE)
        self.assertEqual(self.peer.tick(), "synced")
        self.assertEqual(served_during_sync, [False])  # one process on the profile at a time
        self.assertTrue(self.peer_api.up)
        self.assertFalse(self.peer.status["dirty"])

    def test_every_few_minutes_otherwise(self):
        self.peer.tick()
        self.clock.advance(jp.SYNC_EVERY - 1)
        self.assertEqual(self.peer.tick(), "idle")
        self.clock.advance(2)
        self.assertEqual(self.peer.tick(), "synced")

    def test_the_desktop_app_takes_over_after_what_thirties_wrote_is_pushed(self):
        self.peer.tick()
        self.peer.note_write()
        self.desktop.up = True
        self.clock.advance(6)
        pushes = []
        self.runner.on_sync = lambda: pushes.append(self.peer_api.up)
        self.assertEqual(self.peer.tick(), "handed over")
        self.assertEqual(pushes, [False])
        self.assertFalse(self.peer_api.up)  # stepped aside
        self.clock.advance(jp.SYNC_EVERY * 2)
        self.assertEqual(self.peer.tick(), "desktop")  # the app syncs itself meanwhile
        self.desktop.up = False
        self.clock.advance(6)
        self.assertEqual(self.peer.tick(), "synced")  # pulls what the app did, then serves again
        self.assertTrue(self.peer_api.up)

    def test_without_the_apps_token_the_app_is_just_another_device(self):
        self.cfg.joplin.api_token = ""
        self.desktop.up = True
        self.assertEqual(self.peer.tick(), "synced")
        self.assertTrue(self.peer_api.up)

    def test_switched_off_the_server_stops(self):
        self.peer.tick()
        self.cfg.joplin.peer_enabled = False
        self.assertEqual(self.peer.tick(), "off")
        self.assertFalse(self.peer_api.up)

    def test_a_sign_in_target_waits_for_the_user(self):
        self.cfg.joplin.peer_target = 7
        self.assertEqual(self.peer.tick(), "needs")
        self.assertNotIn(["sync"], [self.runner._joplin_args(c) for c in self.runner.calls if "--profile" in c])


class TestFailures(PeerCase):

    def setUp(self):
        super().setUp()
        self.cfg.joplin.peer_password = "hunter2"
        self.install()
        self.peer_db()
        self.peer.tick()  # synced once, serving

    def test_a_locked_target_is_retried_later_and_later(self):
        self.clock.advance(jp.SYNC_EVERY)
        self.runner.syncs = [CliResult(0, LOCKED), CliResult(0, LOCKED)]
        self.assertEqual(self.peer.tick(), "locked")
        self.assertEqual(self.peer.status["next_sync"], self.clock.t + 120)
        self.assertIn("locked", self.peer.status["message"])
        self.clock.advance(121)
        self.peer.tick()
        self.assertEqual(self.peer.status["next_sync"], self.clock.t + 300)
        self.assertEqual(self.told, [])  # it sorts itself out; nobody needs telling
        self.clock.advance(301)
        self.assertEqual(self.peer.tick(), "synced")
        self.assertEqual(self.peer.status["failures"], 0)

    def test_a_password_problem_is_told_once_and_waits_for_the_user(self):
        self.clock.advance(jp.SYNC_EVERY)
        self.runner.syncs = [CliResult(0, "Error: Unauthorized (401)"), CliResult(0, "Error: Unauthorized (401)")]
        self.assertEqual(self.peer.tick(), "auth")
        self.assertEqual(self.peer.status["next_sync"], self.clock.t + jp.RETRY_NEEDS_USER)
        self.clock.advance(jp.RETRY_NEEDS_USER)
        self.peer.tick()
        self.assertEqual(len(self.told), 1)
        self.assertIn("Preferences → Tasks", self.told[0])

    def test_a_crossed_change_is_reported(self):
        self.clock.advance(jp.SYNC_EVERY)
        self.runner.on_sync = lambda: self.peer_db(conflicts=[("c1", "Today")])
        self.assertEqual(self.peer.tick(), "synced")
        self.assertEqual([c["title"] for c in self.peer.status["conflicts"]], ["Today"])
        self.assertIn("“Today” crossed with one from another device", self.told[0])
        self.runner.on_sync = None
        self.clock.advance(jp.SYNC_EVERY)
        self.peer.tick()
        self.assertEqual(len(self.told), 1)  # the same conflict copy isn't news twice

    def test_a_server_that_wont_start(self):
        self.peer.stop_server()
        self.runner.server_fails = True
        self.clock.advance(jp.SYNC_EVERY)
        self.assertEqual(self.peer.tick(), "synced")
        self.assertFalse(self.peer.serving())
        self.assertEqual(self.peer.status["result"], "error")
        self.assertIn("didn't start", self.peer.status["message"])

    def test_a_server_left_by_an_earlier_run_is_stopped_by_its_pid(self):
        self.peer._server = None  # Thirties restarted; the old server still answers
        (self.peer.profile / "clipper-pid.txt").write_text("4242")
        self.peer.stop_server()
        self.assertEqual(self.runner.killed, [4242])
        self.assertFalse(self.peer_api.up)

    def test_encrypted_notes_need_the_master_password(self):
        decrypts = []
        self.runner.syncs = [CliResult(0, SYNC_OK + "\n/!\\ Your password is needed to decrypt some of your data.")]
        self.runner.on_sync = lambda: self.peer_db(encrypted=2)
        self.clock.advance(jp.SYNC_EVERY)
        self.assertEqual(self.peer.tick(), "password")
        self.assertEqual(self.peer.needs(), ["master_password"])
        self.assertIn("master password", self.told[0])
        self.cfg.joplin.peer_master_password = "sesame"  # given in Preferences: set up again, synced at once
        orig = self.runner.run

        def run(cmd, **kw):
            if "e2ee" in cmd:
                decrypts.append(cmd)
                return CliResult(0, "Decrypted items: 2\nCompleted decryption.")
            return orig(cmd, **kw)
        self.runner.run = run
        self.runner.on_sync = None
        self.assertEqual(self.peer.tick(), "synced")
        self.assertEqual(self.runner.imports[-1][0]["encryption.masterPassword"], "sesame")
        self.assertEqual(len(decrypts), 1)

    def test_nothing_encrypted_nothing_to_decrypt(self):
        self.cfg.joplin.peer_master_password = "sesame"
        self.clock.advance(jp.SYNC_EVERY)
        self.assertEqual(self.peer.tick(), "synced")
        self.assertFalse(any("e2ee" in c for c in self.runner.calls))  # it would fail: no master key loaded

    def test_encrypted_notes_on_an_old_node_ask_for_a_newer_one(self):
        self.runner.node_version = "v20.18.0"
        self.peer._node = None
        self.peer._set(encrypted=True)
        self.cfg.joplin.peer_master_password = "sesame"
        self.assertEqual(self.peer.needs(), ["newer_node"])

    def test_stale_after_long_without_a_sync(self):
        self.assertTrue(self.peer.fresh())
        self.clock.advance(jp.STALE_AFTER + 1)
        self.assertFalse(self.peer.fresh())


class TestRouting(PeerCase):

    def setUp(self):
        super().setUp()
        self.notes = {"n1": {"id": "n1", "title": "Errands", "body": "- [ ] Taxes\n", "updated_time": 1000}}
        self.desktop.notes = {k: dict(v) for k, v in self.notes.items()}
        self.peer_api.notes = {k: dict(v) for k, v in self.notes.items()}
        self.peer._set(last_ok_sync=self.clock.t, result="ok")
        self.router = JoplinRouter(self.cfg, desktop=self.desktop, peer_api=self.peer_api, peer=self.peer,
                                   clock=self.clock)
        self.sched = make_scheduler()
        self.sched.config.joplin = self.cfg.joplin
        self.writer = JoplinWriter(self.sched.config, self.sched.state_db, self.router)

    def fresh_look(self):
        self.clock.advance(JoplinRouter.CACHE_SECONDS)

    def test_the_desktop_app_while_it_runs_else_the_peer(self):
        self.desktop.up = self.peer_api.up = True
        self.assertEqual(self.router.which, "desktop")
        self.fresh_look()
        self.desktop.up = False
        self.assertEqual(self.router.which, "peer")
        self.fresh_look()
        self.cfg.joplin.api_token = ""  # running, but Thirties has no token for it
        self.desktop.up = True
        self.assertEqual(self.router.which, "peer")
        self.fresh_look()
        self.peer_api.up = False
        self.assertEqual(self.router.which, "")
        self.assertFalse(self.router.ping())

    def test_a_stale_peer_takes_no_writes(self):
        self.peer_api.up = True
        self.clock.advance(jp.STALE_AFTER + 1)
        self.assertFalse(self.router.ping())
        status, _ = self.writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Taxes"})
        self.assertEqual(status, "queued")
        self.assertEqual(self.peer_api.updates, [])

    def test_write_back_needs_no_desktop_token_with_the_peer(self):
        self.cfg.joplin.api_token = ""
        self.assertTrue(self.writer.allowed)
        self.cfg.joplin.peer_enabled = False
        self.assertFalse(self.writer.allowed)

    def test_the_outbox_drains_through_the_peer_and_it_syncs_soon(self):
        op = {"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Taxes"}
        self.assertEqual(self.writer.submit(op)[0], "queued")  # nothing up yet
        self.writer.submit({"op": "add_task", "note_id": "n1", "title": "Buy stamps"})
        self.assertEqual(self.writer.flush(), 0)
        self.peer_api.up = True
        self.assertEqual(self.writer.flush(), 2)
        self.assertIn("- [x] Taxes", self.peer_api.notes["n1"]["body"])
        self.assertIn("- [ ] Buy stamps", self.peer_api.notes["n1"]["body"])
        self.assertEqual(self.desktop.updates, [])
        self.assertTrue(self.peer.status["dirty"])
        self.assertEqual(self.peer.fence.entry("n1")["via"], "peer")

    def test_flush_never_opens_the_app_when_the_peer_is_on(self):
        self.cfg.joplin.launch_when_needed = True
        self.writer.submit({"op": "add_task", "note_id": "n1", "title": "X"})
        with unittest.mock.patch("thirties_core.joplin_writeback.launch_joplin") as launch:
            self.assertEqual(self.writer.flush(), 0)
        launch.assert_not_called()

    def test_a_note_changed_through_the_peer_waits_for_the_app_to_sync_it(self):
        self.peer_api.up = True
        self.writer.submit({"op": "complete", "is_checkbox_item": True, "note_id": "n1", "title": "Taxes"})
        synced = self.peer_api.notes["n1"]
        self.desktop.up = True  # the app opens before it has pulled the change
        self.fresh_look()
        status, _ = self.writer.submit({"op": "add_task", "note_id": "n1", "title": "Buy stamps"})
        self.assertEqual(status, "queued")
        self.assertEqual(self.writer.flush(), 0)
        self.assertEqual(self.desktop.updates, [])
        self.desktop.notes["n1"] = dict(synced)  # the app synced: it has the peer's change, same updated_time
        self.assertEqual(self.writer.flush(), 1)
        self.assertIn("- [x] Taxes\n- [ ] Buy stamps", self.desktop.notes["n1"]["body"])
        self.assertEqual(self.peer.fence.entry("n1")["via"], "desktop")  # the app's turn now

    def test_the_fence_gives_up_after_a_day(self):
        fence = WriteFence(self.tmp / "f.json", self.clock)
        fence.record("n9", "peer", 5000)
        self.assertTrue(fence.holds("n9", "desktop", 4000))
        self.assertTrue(fence.holds("n9", "desktop", None))  # the app doesn't have the note yet
        self.assertFalse(fence.holds("n9", "peer", 1))
        self.clock.advance(WriteFence.MAX_AGE + 1)
        self.assertFalse(fence.holds("n9", "desktop", 4000))
        self.assertIsNone(fence.entry("n9"))

    def test_the_engine_uses_the_router_only_with_the_peer_on(self):
        from thirties_core.joplin_engine import JoplinAPIClient, JoplinEngine
        self.assertIsInstance(JoplinEngine(self.cfg).api_client, JoplinRouter)
        off = ThirtiesConfig()
        self.assertIsInstance(JoplinEngine(off).api_client, JoplinAPIClient)


class TestReading(PeerCase):

    def test_which_database_is_read(self):
        old = time.time() - 3600
        os.utime(self.desktop_db, (old, old))
        self.peer_db()
        self.assertEqual(reading_db_path(self.cfg, self.desktop_db, self.peer), self.desktop_db)  # never synced
        self.peer._set(last_ok_sync=time.time())
        self.assertEqual(reading_db_path(self.cfg, self.desktop_db, self.peer), self.peer.db_path)
        self.desktop.up = True
        self.clock.advance(10)
        self.assertEqual(reading_db_path(self.cfg, self.desktop_db, self.peer), self.desktop_db)  # the app's open
        self.desktop.up = False
        self.clock.advance(10)
        os.utime(self.desktop_db, None)  # the app changed its copy after the peer's last sync
        self.peer._set(last_ok_sync=time.time() - 60)
        self.assertEqual(reading_db_path(self.cfg, self.desktop_db, self.peer), self.desktop_db)
        self.assertEqual(reading_db_path(self.cfg, None, self.peer), self.peer.db_path)  # no app on this computer

    def test_the_config_asks_the_peer_only_when_it_is_on(self):
        self.cfg.joplin.peer_enabled = False
        self.assertEqual(self.cfg.resolve_joplin_db_path(), self.desktop_db)
        self.cfg.joplin.peer_enabled = True
        with unittest.mock.patch("thirties_core.joplin_peer.reading_db_path", return_value=Path("/x")) as pick:
            self.assertEqual(self.cfg.resolve_joplin_db_path(), Path("/x"))
            self.assertEqual(self.cfg.resolve_joplin_db_path(desktop_only=True), self.desktop_db)
        self.assertEqual(pick.call_args[0][1], self.desktop_db)


class TestSignIn(PeerCase):

    def test_dropbox_sign_in_with_a_code(self):
        self.cfg.joplin.peer_target = 7
        self.install()
        self.peer_db()
        proc = FakeProc(lines=DROPBOX_AUTH.splitlines()[:3] + ["Starting synchronization...", "Completed: x"])
        self.runner.sign_in_proc = proc
        run, msg = self.peer.begin_sign_in()
        self.assertIsNotNone(run, msg)
        self.assertEqual(run.kind, "code")
        run.answer("the-code")
        self.assertTrue(run.done.wait(5))
        self.assertTrue(run.url.startswith("https://www.dropbox.com/oauth2/authorize"))
        self.assertEqual(proc.stdin.getvalue(), "the-code\n")
        self.assertTrue(run.outcome.ok)
        self.assertEqual(self.peer.needs(), [])  # signed in
        self.assertTrue(self.peer_api.up)  # and serving
        self.assertTrue(self.peer._cli_lock.acquire(blocking=False))  # the lock was given back
        self.peer._cli_lock.release()


class TestPresenceAndStatus(unittest.TestCase):

    def test_offered_once_when_the_app_is_often_closed(self):
        sched = make_scheduler()
        cfg = sched.config
        cfg.joplin.write_back = True
        presence = Presence(sched.state_db)
        day = date(2026, 10, 20)
        for i in range(2):
            presence.sample(False, day + timedelta(days=i))
        self.assertFalse(presence.should_offer(cfg))  # two days isn't enough to judge
        presence.sample(True, day + timedelta(days=2))
        presence.sample(False, day + timedelta(days=2))
        self.assertTrue(presence.should_offer(cfg))
        presence.offered()
        self.assertFalse(presence.should_offer(cfg))

    def test_not_offered_when_the_app_is_usually_open(self):
        sched = make_scheduler()
        sched.config.joplin.write_back = True
        presence = Presence(sched.state_db)
        for i in range(4):
            presence.sample(True, date(2026, 10, 20) + timedelta(days=i))
        presence.sample(False, date(2026, 10, 24))
        self.assertFalse(presence.should_offer(sched.config))

    def test_the_background_service_offers_it(self):
        from thirties_core.background import BackgroundService
        from thirties_core.inference import MockInferenceEngine
        sched = make_scheduler()
        sched.config.joplin.write_back = True
        joplin = MagicMock()
        joplin.api_client.ping.return_value = False
        sent = []
        svc = BackgroundService(sched.config, lambda t, b, k: sent.append((k, b)), scheduler=sched,
                                joplin_engine=joplin, inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        svc.send = lambda text, kind, **kw: sent.append((kind, text))
        presence = Presence(sched.state_db)
        for i in range(3):
            presence.sample(False, date(2026, 10, 20) + timedelta(days=i))
        from datetime import datetime
        svc.joplin_presence(datetime(2026, 10, 23, 10, 0))
        svc._last_presence = 0
        svc.joplin_presence(datetime(2026, 10, 23, 10, 30))
        self.assertEqual(sent, [("joplin_peer", jp.OFFER_TEXT)])

    def test_status_line(self):
        now = 1_800_000_000.0
        self.assertEqual(describe({}, now), "Not installed yet")
        self.assertEqual(describe({"last_ok_sync": now - 240, "result": "ok"}, now, "3.7.1"),
                         "Joplin 3.7.1; last synced 4 minutes ago")
        self.assertEqual(describe({"result": "needs"}, now, "3.7.1", needs=["password"]),
                         "Joplin 3.7.1; not synced yet; needs the password")
        line = describe({"last_ok_sync": now - 7200, "result": "locked", "message": "Locked; trying again shortly.",
                         "conflicts": [{"title": "Today", "at": now - 60}]}, now, "3.7.1", desktop_up=True)
        self.assertEqual(line, "Joplin 3.7.1; the Joplin app is open, so Thirties uses it; last synced 2 hours ago; "
                               "Locked; trying again shortly.; 1 conflict copy this week")


if __name__ == "__main__":
    unittest.main()
