"""A headless Joplin sync peer: Thirties' own copy of Joplin, so its reads and writes reach the user's notes (and
their other devices) when the Joplin app is closed, or on a computer where it isn't installed at all.

It is Joplin's official terminal app (the npm package "joplin"), run by Thirties in user space with its own
profile under ~/.local/share/thirties/joplin-peer/, set to the same sync target as the user's Joplin apps. To the
sync target it is one more device, so Joplin's own sync (its locks, its conflict copies, its encryption) keeps
everything consistent; Thirties never writes the sync target's files itself. (Talking to the target directly was
the other option: Joplin's item format, E2EE and locks would all have to be re-implemented. Rejected.)

How it runs (verified with the real CLI 3.7.1 on a throwaway profile and a local folder target, 2026-10-30):

- ``joplin --profile <dir> server start`` serves the same Data API as the desktop app's Web Clipper service, on a
  private port with a token Thirties made, so ``JoplinWriter`` and everything else talk to it unchanged.
- ``joplin --profile <dir> sync`` syncs. Joplin's help says no two CLI processes should use one profile at once
  (each keeps the settings in memory and writes them all back on exit), so a sync stops the server, syncs and
  starts it again; one process at a time, under one lock. Writes that come meanwhile wait in the outbox.
- ``config --import-file`` sets the sync target and its secrets from a 0600 file that is deleted straight after,
  so no password is ever on a command line.
- Node 22.12+ / 20.19+ run it as is; 20.17-20.18 and 22.0-22.11 need ``--experimental-require-module`` (Joplin's
  libraries ``require()`` ES modules); older Nodes can't run it, and Thirties offers its own.

Which copy of Joplin is used (``JoplinRouter`` for the Data API, ``reading_db_path`` for the database):
the desktop app while it runs (with a token), else the peer once it has synced. A note changed through one is
not changed through the other until that one has the change too (``WriteFence``): Joplin would keep both
versions as a conflict, and Thirties' change would end up in the Conflicts notebook.

Credentials: Thirties reads *where* the desktop app syncs (the target and its non-secret settings, from its
settings.json and database), never its passwords or sign-in tokens. Passwords are asked once and kept in the
keyring (settings.py secrets); browser sign-ins (Dropbox, OneDrive, Joplin Cloud) are done by the peer itself, so
its tokens are its own and refreshing one can't sign the desktop app out.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import secrets as _secrets
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from thirties_core.joplin_engine import JoplinAPIClient

logger = logging.getLogger(__name__)

# What's installed: Joplin's newest release, since a sync target can require a minimum app version (its
# info.json "appMinVersion") set by the user's newest Joplin; an older peer would be refused.
JOPLIN_PACKAGE = "joplin"
NODE_DIST = "https://nodejs.org/dist/latest-v22.x/"  # the private Node, when the computer has none fit to run it

SYNC_EVERY = 300            # seconds between syncs while the peer is the live copy
SYNC_AFTER_WRITE = 20       # a sync this long after the last write through the peer (a turn writes in bursts)
STALE_AFTER = 1800          # with no successful sync for this long, the peer isn't trusted for reads or writes
RETRY_AFTER = (120, 300, 900, 1800)  # after failed syncs in a row (a locked target, the network)
RETRY_NEEDS_USER = 3600     # after a failure only the user can fix (a password, a sign-in)
START_TIMEOUT = 30          # for the server to answer after it's started
SYNC_TIMEOUT = 1800         # a first sync of a big notebook takes a while
TICK_SECONDS = 15
# Encrypted notes need a newer Node: on Node 20.18 Joplin's encryption crashes inside Node's WebCrypto ("Assertion
# failed: errors->Empty()", seen 2026-10-30); Node 22.23 worked. 22.12 is a guess at the first good one.
E2EE_NODE_MIN = (22, 12)


def peer_home() -> Path:
    """Where the peer lives: under ~/.local/share/thirties, which the Flatpak shares with the host (the CLI runs
    on the host). THIRTIES_JOPLIN_PEER_HOME moves it (tests, a server)."""
    env = os.environ.get("THIRTIES_JOPLIN_PEER_HOME")
    return Path(os.path.expanduser(env)) if env else Path.home() / ".local" / "share" / "thirties" / "joplin-peer"


# --- the sync targets Joplin supports ---

@dataclass(frozen=True)
class SyncTarget:
    id: int
    name: str
    label: str
    fields: Tuple[Tuple[str, str, bool], ...] = ()  # (sync.N.<suffix>, label, required): plain settings
    secret: str = ""                                 # the suffix of its password, if it has one
    sign_in: bool = False                            # authorised in a browser, by the peer itself
    copy: Tuple[str, ...] = ()                       # more sync.N.<suffix> settings taken from the desktop as is


SYNC_TARGETS: Dict[int, SyncTarget] = {t.id: t for t in (
    SyncTarget(2, "filesystem", "A folder", (("path", "Folder", True),)),
    SyncTarget(3, "onedrive", "OneDrive", sign_in=True),
    SyncTarget(5, "nextcloud", "Nextcloud", (("path", "Nextcloud WebDAV URL", True), ("username", "Username", True)),
               secret="password"),
    SyncTarget(6, "webdav", "WebDAV", (("path", "WebDAV URL", True), ("username", "Username", True)),
               secret="password"),
    SyncTarget(7, "dropbox", "Dropbox", sign_in=True),
    SyncTarget(8, "amazon_s3", "S3", (("path", "Bucket", True), ("url", "URL", False), ("region", "Region", False),
                                      ("username", "Access key", True)), secret="password", copy=("forcePathStyle",)),
    SyncTarget(9, "joplinServer", "Joplin Server", (("path", "Server URL", True), ("username", "Email", True)),
               secret="password"),
    SyncTarget(10, "joplinCloud", "Joplin Cloud", sign_in=True, copy=("path", "website")),
)}


@dataclass
class DesktopTarget:
    """Where the desktop app syncs, as far as can be read without its secrets."""
    target: int = 0
    values: Dict[str, Any] = field(default_factory=dict)  # suffix -> value, e.g. {"path": "https://…"}
    e2ee: bool = False
    found: bool = False  # a desktop profile was there to read

    @property
    def label(self) -> str:
        info = SYNC_TARGETS.get(self.target)
        return info.label if info else ("none" if not self.target else f"target {self.target}")


_SECRET_SUFFIX = re.compile(r"(password|auth|context|secret|token)", re.IGNORECASE)


def read_desktop_target(profile_dir: Optional[Path]) -> DesktopTarget:
    """The desktop app's sync target and its plain settings: public settings are in settings.json, the rest in the
    database's settings table (older versions kept everything there). Passwords and sign-in tokens are never read."""
    out = DesktopTarget()
    if profile_dir is None or not Path(profile_dir).is_dir():
        return out
    out.found = True
    values: Dict[str, Any] = {}
    db = Path(profile_dir) / "database.sqlite"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'sync.%' "
                                    "OR key IN ('encryption.enabled', 'syncInfoCache')").fetchall()
            finally:
                conn.close()
            values.update({k: v for k, v in rows})
        except sqlite3.Error as e:
            logger.info("Couldn't read the Joplin app's settings table: %s", e)
    try:
        values.update(json.loads((Path(profile_dir) / "settings.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        logger.info("Couldn't read the Joplin app's settings.json: %s", e)
    try:
        out.target = int(values.get("sync.target") or 0)
    except (TypeError, ValueError):
        out.target = 0
    info = SYNC_TARGETS.get(out.target)
    if info:
        for suffix in [f for f, _, _ in info.fields] + list(info.copy):
            value = values.get(f"sync.{out.target}.{suffix}")
            if value not in (None, "") and not _SECRET_SUFFIX.search(suffix):
                out.values[suffix] = value
    try:
        cache = values.get("syncInfoCache")
        cache = json.loads(cache) if isinstance(cache, str) else (cache or {})
        out.e2ee = bool((cache.get("e2ee") or {}).get("value"))
    except (ValueError, AttributeError):
        out.e2ee = False
    out.e2ee = out.e2ee or str(values.get("encryption.enabled", "")).lower() in ("1", "true")
    return out


# --- running things on the host ---

def _in_flatpak() -> bool:
    return Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))


def _host(cmd: Sequence[str], watch: bool = False) -> List[str]:
    """Inside the Flatpak the CLI runs on the host (its Node and its keyring are there), like the Joplin launch.
    ``watch``: the host process ends when Thirties does (a long-lived server)."""
    if not _in_flatpak():
        return list(cmd)
    return ["flatpak-spawn", "--host"] + (["--watch-bus"] if watch else []) + list(cmd)


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    """Outside the Flatpak: the server stops if Thirties dies without stopping it (Linux only)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


@dataclass
class CliResult:
    rc: int
    out: str


@dataclass
class Node:
    path: str
    version: Tuple[int, int, int]

    @property
    def flags(self) -> Optional[List[str]]:
        return node_flags(self.version)

    @property
    def label(self) -> str:
        return f"Node {'.'.join(map(str, self.version))}"


def node_flags(version: Tuple[int, ...]) -> Optional[List[str]]:
    """The flags a Node needs to run the Joplin CLI (it require()s ES modules); None if it can't."""
    v = tuple(version[:2])
    if v >= (22, 12) or (20, 19) <= v < (21, 0):
        return []
    if (20, 17) <= v < (21, 0) or (22, 0) <= v < (22, 12):
        return ["--experimental-require-module"]
    return None


def parse_node_version(text: str) -> Optional[Tuple[int, int, int]]:
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class Runner:
    """Runs commands (on the host inside the Flatpak). Tests replace it with a fake."""

    def run(self, cmd: Sequence[str], timeout: float = 60, input: Optional[str] = None,
            env: Optional[Dict[str, str]] = None) -> CliResult:
        try:
            p = subprocess.run(_host(cmd), capture_output=True, text=True, timeout=timeout,
                               input=input, stdin=None if input is not None else subprocess.DEVNULL,
                               env={**os.environ, **env} if env else None)
            return CliResult(p.returncode, (p.stdout or "") + (p.stderr or ""))
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return CliResult(-9, out + "\n(timed out)")
        except OSError as e:
            return CliResult(-1, str(e))

    def spawn(self, cmd: Sequence[str], log_path: Optional[Path] = None, pipes: bool = False) -> subprocess.Popen:
        if pipes:
            return subprocess.Popen(_host(cmd, watch=True), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    preexec_fn=None if _in_flatpak() else _die_with_parent)
        log = open(log_path, "a") if log_path else subprocess.DEVNULL
        try:
            return subprocess.Popen(_host(cmd, watch=True), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, preexec_fn=None if _in_flatpak() else _die_with_parent)
        finally:
            if log_path:
                log.close()

    def kill(self, pid: int) -> None:
        self.run(["kill", "-TERM", str(int(pid))], timeout=10)


def find_node(home: Path, runner: Runner) -> Tuple[Optional[Node], str]:
    """The Node to run the CLI with: Thirties' own if it downloaded one, else the computer's. (node, why not)."""
    candidates = [str(home / "node" / "bin" / "node")]
    found = runner.run(["sh", "-lc", "command -v node"], timeout=10)
    if found.rc == 0 and found.out.strip():
        candidates.append(found.out.strip().splitlines()[-1])
    candidates += [str(Path.home() / ".local" / "bin" / "node"), "/usr/local/bin/node", "/usr/bin/node"]
    too_old = ""
    for path in dict.fromkeys(candidates):
        probe = runner.run([path, "--version"], timeout=10)
        version = parse_node_version(probe.out) if probe.rc == 0 else None
        if version is None:
            continue
        node = Node(path, version)
        if node.flags is not None:
            return node, ""
        too_old = too_old or f"{node.label} is too old for Joplin's terminal app (it needs 20.17 or newer)."
    return None, too_old or "Node.js isn't installed on this computer."


# --- what the CLI's output means ---

@dataclass
class SyncOutcome:
    kind: str     # ok | auth | busy | locked | upgrade_target | app_too_old | password | error
    message: str  # for the user

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


_OUTCOMES = (
    ("auth", r"not authenticated|authentication was not completed|invalid.{0,20}credentials|\b401\b|"
             r"unauthori[sz]ed|forbidden"),
    ("busy", r"synchroni[sz]ation is already in progress|lock file is already being hold"),
    ("locked", r"last error:.*lock|lockerror|exclusive lock|sync lock|cannot acquire"),
    ("upgrade_target", r"sync target must be upgraded"),
    ("app_too_old", r"upgrade joplin|must upgrade|newer version of joplin|please upgrade|mustupgradeapp"),
    ("password", r"your password is needed to decrypt|masterkeynotloaded|master key is not loaded|invalid password"),
)
_MESSAGES = {
    "auth": "Joplin's sync target didn't accept the sign-in. Check the password, or sign in again.",
    "busy": "Another sync of Thirties' copy of Joplin was still running; trying again shortly.",
    "locked": "Another Joplin app has the sync target locked (it may be upgrading it); trying again shortly.",
    "upgrade_target": "The sync target needs upgrading. Open the Joplin app once and let it upgrade the target.",
    "app_too_old": "Your notes were synced by a newer Joplin; Thirties will update its copy.",
    "password": "Your notes are encrypted: enter your Joplin master password so Thirties can read them.",
}


def classify(result: CliResult) -> SyncOutcome:
    text = result.out or ""
    low = text.lower()
    for kind, pattern in _OUTCOMES:
        if re.search(pattern, low):
            return SyncOutcome(kind, _MESSAGES[kind])
    last = re.search(r"last error:\s*(.+)", text, re.IGNORECASE)
    if result.rc != 0 or last:
        detail = (last.group(1) if last else text.strip().splitlines()[-1] if text.strip() else f"exit {result.rc}")
        return SyncOutcome("error", f"Joplin couldn't sync: {detail.strip()[:300]}")
    if re.search(r"to allow joplin to synchroni[sz]e|please login using this url", low) and "completed" not in low:
        return SyncOutcome("auth", _MESSAGES["auth"])  # it asked for a sign-in and never got to syncing
    return SyncOutcome("ok", "")


# --- the fence: one copy of Joplin at a time per note ---

class WriteFence:
    """The last change Thirties made to each note, and through which copy of Joplin (the desktop app or the peer),
    with that note's updated_time after it. A note isn't changed through the other copy until that copy has the
    change too (Joplin keeps an item's updated_time through sync), else Joplin would keep both versions as a
    conflict. After a day the other copy is assumed never to catch up (deleted, or synced elsewhere) and the hold
    is lifted, so nothing waits for ever; Joplin's conflict copy is the safety net then."""

    MAX_AGE = 86400

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("Couldn't save the Joplin write fence: %s", e)

    def record(self, note_id: str, via: str, updated_ms: Optional[int]) -> None:
        if not note_id:
            return
        with self._lock:
            data = self._load()
            data[note_id] = {"via": via, "t": int(updated_ms or 0), "at": self.clock()}
            self._save(data)

    def entry(self, note_id: str) -> Optional[Dict[str, Any]]:
        return self._load().get(note_id)

    def holds(self, note_id: str, via: str, current_ms: Optional[int]) -> bool:
        """Whether changing this note through ``via`` must wait; ``current_ms`` is that copy's updated_time
        (None: it doesn't have the note yet)."""
        with self._lock:
            data = self._load()
            e = data.get(note_id)
            if not e or e.get("via") == via:
                return False
            if self.clock() - float(e.get("at", 0)) > self.MAX_AGE or \
                    (current_ms is not None and int(current_ms) >= int(e.get("t", 0))):
                del data[note_id]  # caught up (or given up on): either copy may change it now
                self._save(data)
                return False
            return True


# --- the peer ---

class SignIn:
    """A browser sign-in done by the peer's own ``sync`` (Dropbox, OneDrive, Joplin Cloud): it prints a URL; for
    Dropbox the user pastes back the code Dropbox shows ("code"), for Joplin Cloud they confirm once they've
    signed in ("confirm"), for OneDrive the browser comes back to the CLI by itself ("browser"). It runs in its own
    thread, which holds the peer's lock throughout, so nothing else runs on the profile meanwhile."""

    KINDS = {7: "code", 10: "confirm"}

    def __init__(self, peer: "JoplinPeer", target: int) -> None:
        self.peer = peer
        self.kind = self.KINDS.get(target, "browser")
        self.lines: List[str] = []
        self.url = ""
        self.outcome: Optional[SyncOutcome] = None
        self.done = threading.Event()
        self._started = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="JoplinPeerSignIn")
        self._thread.start()

    def _run(self) -> None:
        peer = self.peer
        if not peer._cli_lock.acquire(timeout=120):
            self.outcome = SyncOutcome("busy", _MESSAGES["busy"])
            self._started.set()
            self.done.set()
            return
        try:
            peer.stop_server()
            cmd = peer._cli_command(["sync"])
            if cmd is None:
                self.outcome = SyncOutcome("error", "Joplin's terminal app isn't installed.")
                return
            peer._before_conflicts = peer._conflict_ids()
            self._proc = peer.runner.spawn(cmd, pipes=True)
            self._started.set()
            for line in iter(self._proc.stdout.readline, ""):  # the URL comes on a line of its own
                self.lines.append(line.rstrip("\n"))
                m = re.search(r"https?://\S+", line)
                if m and not self.url:
                    self.url = m.group(0)
            self._proc.wait()
            result = CliResult(self._proc.returncode or 0, "\n".join(self.lines))
            peer._log(["sync", "(sign-in)"], result)
            self.outcome = peer._after_sync(result, signed_in=True)
            if self.outcome.ok and not peer.desktop_up():
                peer.start_server()
        except Exception as e:
            logger.warning("The Joplin sign-in failed: %s", e)
            self.outcome = SyncOutcome("error", f"The sign-in failed: {e}")
        finally:
            self._started.set()
            peer._cli_lock.release()
            self.done.set()

    def answer(self, text: str = "y") -> None:
        """The code (Dropbox) or "y" (Joplin Cloud, once signed in in the browser)."""
        self._started.wait(10)
        try:
            self._proc.stdin.write(text.strip() + "\n")
            self._proc.stdin.flush()
        except (AttributeError, OSError, ValueError) as e:
            logger.warning("Couldn't answer the Joplin sign-in: %s", e)

    def cancel(self) -> None:
        try:
            if self._proc is not None:
                self._proc.terminate()
        except OSError:
            pass


class JoplinPeer:
    """Installs, configures, runs and syncs the peer. One per profile (``peer_for``); its duty runs in its own
    thread (``run``), so a long sync never holds up the background service's messages."""

    def __init__(self, config, home: Optional[Path] = None, runner: Optional[Runner] = None,
                 desktop: Optional[JoplinAPIClient] = None, peer_api: Optional[JoplinAPIClient] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.home = Path(home) if home else peer_home()
        self.runner = runner or Runner()
        self.clock = clock
        self._desktop = desktop
        self._peer_api = peer_api
        self._cli_lock = threading.RLock()  # one CLI process on the profile at a time
        self._server = None                 # the running ``server start`` process
        self._node: Optional[Node] = None
        self._desktop_seen: Optional[bool] = None
        self._desktop_cache: Tuple[float, bool] = (0.0, False)
        self._status_cache: Tuple[float, Dict[str, Any]] = (-1.0, {})
        self.fence = WriteFence(self.home / "fence.json", clock)
        self.on_ready: Optional[Callable[[], None]] = None      # the peer started serving (deliver the outbox)
        self.notify: Optional[Callable[[str], None]] = None     # something the user should hear about
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- paths and settings ---

    @property
    def cfg(self):
        return self.config.joplin

    @property
    def profile(self) -> Path:
        return self.home / "profile"

    @property
    def db_path(self) -> Path:
        return self.profile / "database.sqlite"

    @property
    def main_js(self) -> Path:
        return self.home / "npm" / "node_modules" / "joplin" / "main.js"

    @property
    def desktop(self) -> JoplinAPIClient:
        return self._desktop or JoplinAPIClient(self.cfg.api_host, self.cfg.api_port, self.cfg.api_token)

    @property
    def peer_api(self) -> JoplinAPIClient:
        return self._peer_api or JoplinAPIClient("127.0.0.1", self.cfg.peer_port, self.cfg.peer_token)

    # --- status (a file, so every process and the Preferences page see the same) ---

    @property
    def status(self) -> Dict[str, Any]:
        path = self.home / "status.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        if mtime != self._status_cache[0]:
            try:
                self._status_cache = (mtime, json.loads(path.read_text()))
            except (OSError, ValueError):
                self._status_cache = (mtime, {})
        return dict(self._status_cache[1])

    def _set(self, **changes: Any) -> Dict[str, Any]:
        data = {**self.status, **changes}
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            tmp = self.home / "status.tmp"
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.home / "status.json")
            self._status_cache = (-1.0, {})
        except OSError as e:
            logger.warning("Couldn't save the Joplin peer's status: %s", e)
        return data

    def _log(self, args: Sequence[str], result: CliResult) -> None:
        """What each CLI run said (arguments never hold secrets), kept to the last half megabyte."""
        path = self.home / "peer.log"
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 512_000:
                path.write_text(path.read_text(errors="replace")[-256_000:])
            with open(path, "a") as f:
                f.write(f"--- {datetime.now().isoformat(timespec='seconds')} joplin {' '.join(args)} -> {result.rc}\n"
                        f"{result.out.strip()[-4000:]}\n")
        except OSError:
            pass

    # --- installing ---

    def node(self) -> Tuple[Optional[Node], str]:
        if self._node is None:
            node, why = find_node(self.home, self.runner)
            if node is None:
                return None, why
            self._node = node
        return self._node, ""

    def installed_version(self) -> str:
        try:
            return json.loads((self.main_js.parent / "package.json").read_text()).get("version", "")
        except (OSError, ValueError):
            return ""

    def install_state(self) -> str:
        """ready | no_node | not_installed."""
        node, _ = self.node()
        if node is None:
            return "no_node"
        return "ready" if self.installed_version() else "not_installed"

    def _npm(self, node: Node) -> Optional[List[str]]:
        """npm, run by this Node (the host's PATH inside flatpak-spawn may not have Node on it)."""
        own = self.home / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if node.path == str(self.home / "node" / "bin" / "node") and own.is_file():
            return [node.path, str(own)]
        found = self.runner.run(["sh", "-lc", f'readlink -f "$(dirname "{node.path}")/npm" 2>/dev/null || '
                                              'readlink -f "$(command -v npm)"'], timeout=10)
        script = found.out.strip().splitlines()[-1] if found.rc == 0 and found.out.strip() else ""
        return [node.path, script] if script.endswith(".js") else None

    def install(self) -> Tuple[bool, str]:
        """npm install the CLI into the peer's own prefix (minutes; call from a thread)."""
        node, why = self.node()
        if node is None:
            return False, why
        npm = self._npm(node)
        if npm is None:
            return False, "npm wasn't found next to Node. Use Thirties' own Node instead (Download Node)."
        with self._cli_lock:
            (self.home / "npm").mkdir(parents=True, exist_ok=True)
            args = ["install", "--prefix", str(self.home / "npm"), "--no-audit", "--no-fund", "--omit=dev",
                    JOPLIN_PACKAGE]
            result = self.runner.run(npm + args, timeout=1200)
            self._log(["(npm)"] + args, result)
        version = self.installed_version()
        if result.rc != 0 or not version:
            tail = result.out.strip().splitlines()[-1] if result.out.strip() else f"exit {result.rc}"
            return False, f"Installing Joplin's terminal app failed: {tail[:300]}"
        self._set(installed=version, node=f"{node.path} ({node.label})")
        return True, f"Installed Joplin {version}."

    def download_node(self, progress: Optional[Callable[[int, int], None]] = None,
                      fetch: Callable[..., Any] = urllib.request.urlopen) -> Tuple[bool, str]:
        """Thirties' own Node (the official Linux build, about 30 MB, checked against its published SHA-256), for a
        computer whose Node is missing or too old. Only when the user asks for it."""
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if arch is None:
            return False, f"There's no Node build for this computer ({platform.machine()})."
        try:
            sums = fetch(NODE_DIST + "SHASUMS256.txt", timeout=30).read().decode()
            m = re.search(rf"^([0-9a-f]{{64}})\s+(node-v[\d.]+-linux-{arch}\.tar\.xz)$", sums, re.MULTILINE)
            if not m:
                return False, "Couldn't find a Node build to download."
            digest, name = m.group(1), m.group(2)
            self.home.mkdir(parents=True, exist_ok=True)
            part = self.home / (name + ".part")
            h = hashlib.sha256()
            with fetch(NODE_DIST + name, timeout=60) as resp, open(part, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0) if getattr(resp, "headers", None) else 0
                done = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if h.hexdigest() != digest:
                part.unlink(missing_ok=True)
                return False, "The Node download didn't match its checksum; nothing was installed."
            staging = self.home / "node.new"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            with tarfile.open(part) as tar:
                for member in tar.getmembers():  # drop the top folder (node-vX-linux-x64/)
                    member.name = "/".join(member.name.split("/")[1:])
                tar.extractall(staging, members=[m for m in tar.getmembers() if m.name], filter="data")
            part.unlink(missing_ok=True)
            shutil.rmtree(self.home / "node", ignore_errors=True)
            staging.replace(self.home / "node")
            self._node = None
            return True, f"Downloaded {name.split('-linux')[0]}."
        except Exception as e:
            return False, f"Downloading Node failed: {e}"

    # --- running the CLI ---

    def cli(self, args: Sequence[str], timeout: float = 120, input: Optional[str] = None) -> CliResult:
        node, why = self.node()
        if node is None:
            return CliResult(-1, why)
        if not self.main_js.is_file():
            return CliResult(-1, "Joplin's terminal app isn't installed.")
        self.profile.mkdir(parents=True, exist_ok=True)
        cmd = [node.path] + (node.flags or []) + ["--no-warnings", str(self.main_js), "--profile", str(self.profile)]
        with self._cli_lock:
            result = self.runner.run(cmd + list(args), timeout=timeout, input=input)
        self._log(args, result)
        return result

    def _cli_command(self, args: Sequence[str]) -> Optional[List[str]]:
        node, _ = self.node()
        if node is None or not self.main_js.is_file():
            return None
        self.profile.mkdir(parents=True, exist_ok=True)
        return [node.path] + (node.flags or []) + ["--no-warnings", str(self.main_js), "--profile",
                                                   str(self.profile)] + list(args)

    # --- the sync target ---

    def desktop_target(self) -> DesktopTarget:
        path = self.config.resolve_joplin_db_path(desktop_only=True)
        return read_desktop_target(path.parent if path else None)

    def target(self) -> Tuple[int, Dict[str, Any], DesktopTarget]:
        """(target id, its sync.N.* values by suffix, what the desktop app has): the user's choices over the
        desktop app's."""
        desk = self.desktop_target()
        target = int(self.cfg.peer_target or desk.target or 0)
        values = dict(desk.values) if target == desk.target else {}
        values.update({k: v for k, v in (self.cfg.peer_target_values or {}).items() if v not in ("", None)})
        return target, values, desk

    def needs(self) -> List[str]:
        """What only the user can give: "target", "unsupported", a field suffix ("path", "username"…),
        "password", "sign_in", "master_password"."""
        target, values, desk = self.target()
        if not target:
            return ["target"]
        info = SYNC_TARGETS.get(target)
        if info is None:
            return ["unsupported"]
        out = [suffix for suffix, _, required in info.fields if required and not values.get(suffix)]
        if info.secret and not self.cfg.peer_password:
            out.append("password")
        st = self.status
        if info.sign_in and not (st.get("signed_in") and st.get("signed_in_target") == target):
            out.append("sign_in")
        encrypted = desk.e2ee or st.get("result") == "password" or bool(st.get("encrypted"))
        if encrypted and not self.cfg.peer_master_password:
            out.append("master_password")
        if encrypted:
            node, _ = self.node()
            if node is not None and tuple(node.version[:2]) < E2EE_NODE_MIN:
                out.append("newer_node")
        return out

    def _settings_to_import(self) -> Dict[str, Any]:
        target, values, _ = self.target()
        info = SYNC_TARGETS[target]
        out: Dict[str, Any] = {"sync.target": target, "api.port": int(self.cfg.peer_port),
                               "api.token": self.cfg.peer_token, "locale": "en_US"}
        for suffix in [f for f, _, _ in info.fields] + list(info.copy):
            if values.get(suffix) not in (None, ""):
                out[f"sync.{target}.{suffix}"] = values[suffix]
        if info.secret and self.cfg.peer_password:
            out[f"sync.{target}.{info.secret}"] = self.cfg.peer_password
        if self.cfg.peer_master_password:
            out["encryption.masterPassword"] = self.cfg.peer_master_password
        return out

    def configure(self, force: bool = False) -> Tuple[bool, str]:
        """Set the peer's profile to the sync target (again only when something changed). The settings go through
        a 0600 file deleted straight after; only a hash of them is kept."""
        missing = [n for n in self.needs() if n not in ("sign_in", "newer_node")]
        if missing:
            return False, "Thirties needs more to sync: " + ", ".join(missing)
        if not self.cfg.peer_token:
            return False, "The peer has no API token yet (turn it on in Preferences)."
        values = self._settings_to_import()
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        st = self.status
        if not force and st.get("configured") == fingerprint:
            return True, ""
        self.profile.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        path = self.home / "import.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(values, f)
            result = self.cli(["config", "--import-file", str(path)])
        finally:
            path.unlink(missing_ok=True)
        if result.rc != 0:
            return False, f"Couldn't set up Joplin's terminal app: {result.out.strip()[-300:]}"
        changes: Dict[str, Any] = {"configured": fingerprint, "next_sync": 0}
        if st.get("target") != values["sync.target"]:
            changes.update(target=values["sync.target"], signed_in=False, last_ok_sync=0)
        self._set(**changes)
        return True, "Set up."

    # --- the server (the Data API) ---

    def serving(self) -> bool:
        return self.peer_api.ping()

    def start_server(self) -> bool:
        """Start the Data API (it holds the profile while it runs). True once it answers."""
        if self.serving():
            return True
        cmd = self._cli_command(["server", "start", "--quiet"])
        if cmd is None:
            return False
        with self._cli_lock:
            try:
                self._server = self.runner.spawn(cmd, self.home / "server.log")
            except OSError as e:
                self._set(result="error", message=f"Couldn't start Joplin's terminal app: {e}")
                return False
            deadline = self.clock() + START_TIMEOUT
            while self.clock() < deadline:
                if self.serving():
                    logger.info("The Joplin peer is serving on port %s.", self.cfg.peer_port)
                    if self.on_ready:
                        try:
                            self.on_ready()
                        except Exception as e:
                            logger.warning("Joplin peer on_ready failed: %s", e)
                    return True
                if self._server.poll() is not None:
                    break
                time.sleep(0.5)
            self._set(result="error", message="Joplin's terminal app didn't start its Data API (see peer.log).")
            self._kill_server()
            return False

    def _kill_server(self) -> None:
        proc, self._server = self._server, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_server(self) -> None:
        """Stop the Data API: our own process, or one left by an earlier run (its pid is in the profile)."""
        with self._cli_lock:
            self._kill_server()
            if not self.serving():
                return
            try:
                pid = int((self.profile / "clipper-pid.txt").read_text().strip())
                self.runner.kill(pid)
            except (OSError, ValueError):
                return
            deadline = self.clock() + 10
            while self.serving() and self.clock() < deadline:
                time.sleep(0.5)

    # --- syncing ---

    def _conflict_ids(self) -> Dict[str, str]:
        if not self.db_path.is_file():
            return {}
        try:
            conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True, timeout=5.0)
            try:
                return {r[0]: r[1] for r in conn.execute("SELECT id, title FROM notes WHERE is_conflict = 1")}
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    def _encrypted_items(self) -> int:
        """Items still encrypted in the peer's copy (synced from an encrypted target, not decrypted yet). Only then
        is ``e2ee decrypt`` run: with nothing to decrypt it fails ("no master key is currently loaded")."""
        if not self.db_path.is_file():
            return 0
        try:
            conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True, timeout=5.0)
            try:
                return sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE encryption_applied = 1").fetchone()[0]
                           for table in ("notes", "folders"))
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    def sync(self, serve_after: bool = True) -> SyncOutcome:
        """Stop the server, sync (and decrypt), start the server again. Blocking; call from the peer's thread."""
        with self._cli_lock:
            self.stop_server()
            self._before_conflicts = self._conflict_ids()
            self._set(last_attempt=self.clock())
            result = self.cli(["sync"], timeout=SYNC_TIMEOUT)
            outcome = self._after_sync(result)
            if serve_after and outcome.ok and not self.desktop_up():
                self.start_server()
            return outcome

    def _after_sync(self, result: CliResult, signed_in: bool = False) -> SyncOutcome:
        outcome = classify(result)
        target = self.status.get("target")
        if outcome.ok and self._encrypted_items():
            self._set(encrypted=True)
            decrypt = self.cli(["e2ee", "decrypt", "--force"], timeout=SYNC_TIMEOUT)  # the CLI doesn't by itself
            if decrypt.rc != 0 or re.search(r"invalid password|password is needed|masterkeynotloaded|"
                                             r"master key is not loaded", decrypt.out.lower()):
                outcome = SyncOutcome("password", _MESSAGES["password"])
                node, _ = self.node()
                if node is not None and tuple(node.version[:2]) < E2EE_NODE_MIN and self.cfg.peer_master_password:
                    outcome = SyncOutcome("password", f"Your notes are encrypted, and this computer's {node.label} "
                                                      "can't decrypt them. Download Thirties' own Node.js "
                                                      "(Preferences → Tasks).")
        now = self.clock()
        st = self.status
        if outcome.ok:
            changes: Dict[str, Any] = dict(result="ok", message="", failures=0, last_ok_sync=now, dirty=False,
                                           next_sync=now + SYNC_EVERY)
            if signed_in or st.get("signed_in_target") == target:
                changes.update(signed_in=True, signed_in_target=target)
            new = {k: v for k, v in self._conflict_ids().items() if k not in getattr(self, "_before_conflicts", {})}
            if new:
                conflicts = (st.get("conflicts") or []) + [{"title": t, "at": now} for t in new.values()]
                changes["conflicts"] = conflicts[-20:]
                for title in new.values():
                    self._tell(f"A change Thirties made to “{title}” crossed with one from another device. Joplin "
                               "kept both: Thirties' copy is in Joplin's Conflicts notebook.")
            self._set(**changes)
            return outcome
        failures = int(st.get("failures", 0)) + 1
        if outcome.kind in ("auth", "password", "upgrade_target"):
            wait = RETRY_NEEDS_USER
        else:
            wait = RETRY_AFTER[min(failures, len(RETRY_AFTER)) - 1]
        changes = dict(result=outcome.kind, message=outcome.message, failures=failures, next_sync=now + wait)
        if outcome.kind == "auth":
            changes["signed_in"] = False
        if outcome.kind != st.get("result"):
            if outcome.kind in ("auth", "password", "upgrade_target"):
                self._tell(outcome.message + " (Preferences → Tasks)")
        self._set(**changes)
        if outcome.kind == "app_too_old":
            self.install()  # the newest CLI; the next round syncs with it
        return outcome

    def _tell(self, text: str) -> None:
        if self.notify:
            try:
                self.notify(text)
            except Exception as e:
                logger.warning("Joplin peer notice failed: %s", e)

    def note_write(self) -> None:
        """A change landed through the peer: sync it out soon (after the rest of the turn's writes)."""
        st = self.status
        self._set(dirty=True, dirty_at=self.clock(), dirty_since=st.get("dirty_since") or self.clock())

    def begin_sign_in(self) -> Tuple[Optional[SignIn], str]:
        """Start the peer's own browser sign-in (from Preferences; poll ``url`` and ``done``)."""
        ok, msg = self.configure()
        if not ok:
            return None, msg
        if not self.main_js.is_file():
            return None, "Joplin's terminal app isn't installed."
        return SignIn(self, self.target()[0]), ""

    # --- which copy is live ---

    def desktop_up(self) -> bool:
        """The desktop app is running with its Data API on and Thirties has its token (cached for a few seconds).
        Running without the token, it's just another device to the peer."""
        at, up = self._desktop_cache
        if self.clock() - at < 5:
            return up
        up = bool(self.cfg.api_token) and bool(self.desktop.ping())  # without its token Thirties can't use it
        self._desktop_cache = (self.clock(), up)
        return up

    def fresh(self) -> bool:
        """Synced recently enough to take writes: a note missing from a long-unsynced copy may only be new
        elsewhere, and the writer would wrongly give up on it as deleted. (Offline for a few minutes is fine:
        Joplin keeps the change and syncs it later.)"""
        last = float(self.status.get("last_ok_sync") or 0)
        return bool(last) and self.clock() - last < STALE_AFTER

    # --- the background duty ---

    def tick(self) -> str:
        """One round: what it did (for tests and the log)."""
        if not self.cfg.peer_enabled:
            if self._server is not None:
                self.stop_server()
            return "off"
        if not self._cli_lock.acquire(blocking=False):
            return "busy"  # a sign-in, or a sync the user started
        try:
            state = self.install_state()
            if state != "ready":
                self._set(result=state, message="")
                return state
            ok, msg = self.configure()
            if not ok or "sign_in" in self.needs():  # a sync would sit waiting for a browser nobody opened
                self._set(result="needs", message=msg if not ok else "Sign in to the sync target (Preferences).")
                return "needs"
            st, now = self.status, self.clock()
            desktop = self.desktop_up()
            came_back = desktop and self._desktop_seen is False
            went_away = not desktop and self._desktop_seen in (None, True)
            self._desktop_seen = desktop
            if desktop:
                # The app has the notes now: push what Thirties wrote through the peer, then step aside.
                if st.get("dirty") or self._server is not None or came_back:
                    if st.get("dirty"):
                        self.sync(serve_after=False)
                    self.stop_server()
                    return "handed over"
                return "desktop"
            due = went_away or now >= float(st.get("next_sync", 0)) or \
                (st.get("dirty") and now - float(st.get("dirty_at", 0)) >= SYNC_AFTER_WRITE)
            if due:
                outcome = self.sync()
                return "synced" if outcome.ok else outcome.kind
            if self.fresh() and not self.serving():
                self.start_server()
                return "started"
            return "idle"
        finally:
            self._cli_lock.release()

    def run(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or self._stop
        while not stop.is_set():
            try:
                did = self.tick()
                if did not in ("idle", "desktop", "off", "busy"):
                    logger.info("Joplin peer: %s", did)
            except Exception as e:
                logger.exception("The Joplin peer's round failed: %s", e)
            stop.wait(TICK_SECONDS)

    def start(self) -> None:
        """A thread of its own with its own stop signal: a settings reload stops the old one and starts a new one
        at once, and the old one may still be finishing a sync (the lock keeps them from overlapping)."""
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.run, args=(self._stop,), daemon=True, name="JoplinPeer")
        self._thread.start()

    def stop(self, server: bool = False) -> None:
        """Stop the duty; ``server`` too (quitting, or the peer turned off). A settings reload restarts the duty
        but leaves the server up."""
        self._stop.set()
        if server:
            try:
                self.stop_server()
            except Exception as e:
                logger.warning("Stopping the Joplin peer failed: %s", e)

    def sync_now(self) -> SyncOutcome:
        """A sync the user asked for (Preferences)."""
        ok, msg = self.configure()
        if not ok:
            return SyncOutcome("needs", msg)
        return self.sync(serve_after=not self.desktop_up())


_PEERS: Dict[str, JoplinPeer] = {}
_PEERS_LOCK = threading.Lock()


def peer_for(config) -> JoplinPeer:
    """The one peer for this profile directory, holding the latest config."""
    home = peer_home()
    with _PEERS_LOCK:
        peer = _PEERS.get(str(home))
        if peer is None:
            peer = _PEERS[str(home)] = JoplinPeer(config, home)
        peer.config = config
        return peer


@atexit.register
def shutdown_all() -> None:
    """Stop every peer's server (quitting Thirties, or the peer turned off)."""
    for peer in list(_PEERS.values()):
        peer.stop(server=True)


def enable(settings, on: bool = True) -> None:
    """The Preferences switch: on, with a Data API token made the first time."""
    if on and not settings.get("joplin.peer_token"):
        settings.set("joplin.peer_token", _secrets.token_hex(24))
    settings.set("joplin.peer_enabled", bool(on))


# --- reading: which database is current ---

def reading_db_path(config, desktop_db: Optional[Path], peer: Optional[JoplinPeer] = None) -> Optional[Path]:
    """The Joplin database to read: the desktop app's while it runs; when it's closed, the peer's if the peer has
    synced since the app last changed its own (an app closed for a day has a day-old copy), else the app's."""
    peer = peer or peer_for(config)
    st = peer.status
    last_ok = float(st.get("last_ok_sync") or 0)
    if not last_ok or not peer.db_path.is_file():
        return desktop_db
    if desktop_db is None:
        return peer.db_path
    try:
        if peer.desktop_up():
            return desktop_db
        return peer.db_path if last_ok >= desktop_db.stat().st_mtime else desktop_db
    except OSError:
        return peer.db_path


# --- writing: which Data API ---

class JoplinRouter:
    """The Data API Thirties uses when the peer is on: the desktop app's while it runs (and Thirties has its
    token), else the peer's while it's fresh. Same methods as ``JoplinAPIClient``, so JoplinWriter, the
    assistant's tools and the sync note use it unchanged. A write through the peer asks it to sync soon."""

    CACHE_SECONDS = 5

    def __init__(self, config, desktop: Optional[JoplinAPIClient] = None, peer_api: Optional[JoplinAPIClient] = None,
                 peer: Optional[JoplinPeer] = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = config.joplin
        self.desktop = desktop or JoplinAPIClient(self.cfg.api_host, self.cfg.api_port, self.cfg.api_token)
        self.peer_api = peer_api or JoplinAPIClient("127.0.0.1", self.cfg.peer_port, self.cfg.peer_token)
        self.peer = peer or peer_for(config)
        self.clock = clock
        self.token = self.desktop.token
        self._cache: Tuple[float, Optional[Tuple[str, Any]]] = (-1e9, None)

    def endpoint(self, fresh: bool = False) -> Optional[Tuple[str, Any]]:
        at, chosen = self._cache
        if not fresh and self.clock() - at < self.CACHE_SECONDS:
            return chosen
        chosen = None
        if self.cfg.api_token and self.desktop.ping():
            chosen = ("desktop", self.desktop)
        elif self.cfg.peer_enabled and self.peer.fresh() and self.peer_api.ping():
            chosen = ("peer", self.peer_api)
        self._cache = (self.clock(), chosen)
        return chosen

    @property
    def which(self) -> str:
        chosen = self.endpoint()
        return chosen[0] if chosen else ""

    def ping(self) -> bool:
        return self.endpoint(fresh=True) is not None

    def holds(self, note_ids: Sequence[str]) -> bool:
        """Some note here was last changed through the other copy of Joplin, which this one hasn't caught up
        with yet (it hasn't synced): wait, rather than make Joplin keep two versions."""
        chosen = self.endpoint()
        if chosen is None:
            return False
        name, client = chosen
        for note_id in note_ids:
            entry = self.peer.fence.entry(note_id) if note_id else None
            if not entry or entry.get("via") == name:
                continue
            note = client.get_note(note_id, fields="id,updated_time")
            if self.peer.fence.holds(note_id, name, (note or {}).get("updated_time") if note else None):
                return True
        return False

    def _wrote(self, name: str, client, note_id: Optional[str], updated: Optional[int] = None) -> None:
        if not note_id:
            return
        if updated is None:
            note = client.get_note(note_id, fields="id,updated_time")
            updated = (note or {}).get("updated_time")
        self.peer.fence.record(note_id, name, updated)
        if name == "peer":
            self.peer.note_write()

    def _call(self, method: str, *args, default=None, **kwargs):
        chosen = self.endpoint()
        if chosen is None:
            return default
        result = getattr(chosen[1], method)(*args, **kwargs)
        if result in (None, False):
            self._cache = (-1e9, None)  # it may have gone away: look again next time
        return result

    # reads
    def get_note(self, note_id, fields="id,title,body,updated_time"):
        return self._call("get_note", note_id, fields=fields)

    def get_folders(self):
        return self._call("get_folders", default=[]) or []

    def get_folder_notes(self, folder_id):
        return self._call("get_folder_notes", folder_id, default=[]) or []

    def search_notes(self, query, fields="id,title,parent_id,updated_time"):
        return self._call("search_notes", query, fields=fields, default=[]) or []

    # writes
    def update_note(self, note_id, data) -> bool:
        chosen = self.endpoint()
        if chosen is None:
            return False
        ok = chosen[1].update_note(note_id, data)
        if ok:
            self._wrote(chosen[0], chosen[1], note_id)
        else:
            self._cache = (-1e9, None)
        return ok

    def create_note(self, title, body, parent_id=None):
        chosen = self.endpoint()
        if chosen is None:
            return None
        note = chosen[1].create_note(title, body, parent_id)
        if note and note.get("id"):
            self._wrote(chosen[0], chosen[1], note["id"], note.get("updated_time"))
        return note

    def create_folder(self, title, parent_id=None):
        chosen = self.endpoint()
        if chosen is None:
            return None
        folder = chosen[1].create_folder(title, parent_id)
        if folder and chosen[0] == "peer":
            self.peer.note_write()
        return folder


def api_client_for(config) -> Any:
    """The Data API for the engines: the desktop app's, or with the peer on, the router."""
    cfg = config.joplin
    if cfg.peer_enabled:
        return JoplinRouter(config)
    return JoplinAPIClient(host=cfg.api_host, port=cfg.api_port, token=cfg.api_token)


# --- offering it ---

PRESENCE_KEY = "joplin_presence"
OFFERED_KEY = "joplin_peer_offered"
OFFER_MIN_DAYS = 3        # days with samples before judging
OFFER_CLOSED_SHARE = 0.5  # the app was closed at least this share of the samples
PRESENCE_DAYS = 7

OFFER_TEXT = ("Joplin is often closed when I have changes for your notes, so they wait. I can keep your notes in "
              "sync without the Joplin app: Preferences → Tasks → “Keep Joplin in sync without the Joplin app”.")


class Presence:
    """How often the desktop app is closed while write-back is on: samples per day for a week, device-local."""

    def __init__(self, state_db) -> None:
        self.db = state_db

    def _load(self) -> Dict[str, List[int]]:
        try:
            return json.loads(self.db.get_sync_meta(PRESENCE_KEY) or "{}")
        except ValueError:
            return {}

    def sample(self, up: bool, day: date) -> None:
        data = self._load()
        up_n, total = data.get(day.isoformat(), [0, 0])
        data[day.isoformat()] = [up_n + int(bool(up)), total + 1]
        cutoff = (day - timedelta(days=PRESENCE_DAYS)).isoformat()
        self.db.set_sync_meta(PRESENCE_KEY, json.dumps({k: v for k, v in data.items() if k > cutoff}))

    def often_closed(self) -> bool:
        data = self._load()
        if len(data) < OFFER_MIN_DAYS:
            return False
        up = sum(v[0] for v in data.values())
        total = sum(v[1] for v in data.values())
        return total > 0 and (total - up) / total >= OFFER_CLOSED_SHARE

    def should_offer(self, config) -> bool:
        """Once: write-back is on, the peer is off, and the app is often closed."""
        return bool(config.joplin.write_back and not config.joplin.peer_enabled
                    and not self.db.get_sync_meta(OFFERED_KEY) and self.often_closed())

    def offered(self) -> None:
        self.db.set_sync_meta(OFFERED_KEY, datetime.now(timezone.utc).isoformat())


# --- for the Preferences page ---

def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' * (minutes != 1)} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' * (hours != 1)} ago"
    days = hours // 24
    return f"{days} day{'s' * (days != 1)} ago"


def describe(status: Dict[str, Any], now: float, installed: str = "", desktop_up: bool = False,
             needs: Sequence[str] = ()) -> str:
    """One line of plain status for Preferences: installed / syncing / last sync / the error."""
    if not installed:
        return "Not installed yet"
    parts = [f"Joplin {installed}"]
    if desktop_up:
        parts.append("the Joplin app is open, so Thirties uses it")
    last = float(status.get("last_ok_sync") or 0)
    parts.append(f"last synced {_ago(now - last)}" if last else "not synced yet")
    result = status.get("result")
    if needs:
        parts.append("needs " + ", ".join(NEED_LABELS.get(n, n) for n in needs))
    elif result not in (None, "ok", "busy") and status.get("message"):
        parts.append(status["message"])
    conflicts = [c for c in status.get("conflicts") or [] if now - float(c.get("at", 0)) < 7 * 86400]
    if conflicts:
        parts.append(f"{len(conflicts)} conflict cop{'ies' if len(conflicts) != 1 else 'y'} this week")
    return "; ".join(parts)


NEED_LABELS = {"target": "a sync target", "unsupported": "a sync target the terminal app supports",
               "path": "the address or folder", "username": "the username", "password": "the password",
               "sign_in": "a sign-in", "master_password": "the encryption master password",
               "url": "the URL", "region": "the region", "newer_node": "a newer Node.js (for encrypted notes)"}


__all__ = ["JoplinPeer", "JoplinRouter", "WriteFence", "Presence", "SyncTarget", "SYNC_TARGETS", "DesktopTarget",
           "read_desktop_target", "reading_db_path", "api_client_for", "peer_for", "enable", "classify",
           "node_flags", "describe", "OFFER_TEXT"]
