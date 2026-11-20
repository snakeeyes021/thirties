"""One Agent mode turn: the person's message to a full agy Gemini run, and its reply back.

Gemini works in its own workspace (agent_dir: in the app's data folder, mode 0700, outside every repository): its
notes (it keeps state.md and history/<date>.md, or whatever it chooses), the write journal, the day's conversation
ids, and the files agy reads there:

- AGENTS.md: the short brief (agy reads a workspace's AGENTS.md as its rules);
- mcp_config.json in agy's per-workspace folder (AGY_DIR): the tool server (tools_server.py, copied to bin/ so the host can run it);
- agy-settings.json: passed with --settings, allowing the tool server's tools without asking.

The call itself is made the way inference.py makes its agy calls: the same binary lookup, flatpak-spawn --host
inside the Flatpak, stream-json on stdin and stdout, a timeout, and one ledger line per call (caller CALLER).
The conversation of the day is resumed with --conversation, so Gemini keeps its context through the day.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

from thirties_core.agent_mode import CALLER

logger = logging.getLogger(__name__)

AGENT_DIR_ENV = "THIRTIES_AGENT_DIR"  # tests point this at a temporary folder
DEFAULT_MODEL = "gemini-4-hydrogen"
DEFAULT_EFFORT = "low"  # agy takes the level as a suffix of the name, unless the name has one already
WORKSPACE_NAME = "agent"  # the workspace's folder, in the app's data folder
AGY_DIR = ".agents"  # agy's per-workspace folder: it reads mcp_config.json there
DEFAULT_TIMEOUT = 600.0
SERVER_NAME = "thirties"
SOCKET_NAME = "tools.sock"
NO_REPLY_NUDGE = "(Your last turn ended without a reply to the person. Reply to their last message now, in plain words.)"

BRIEF = """# You are the person's personal assistant

You talk with one person, through their planning app (on their computer, or on their phone through Telegram).
Their messages come to you as they wrote them.

Your tools (the "thirties" server) read and change their real lists and their day plan:
- read_lists shows their notes as written, with numbered lines; add_line, tick_line, untick_line, edit_line and
  remove_line change one line at a time;
- read_calendar, read_plan and set_plan are their day; now gives the time and the current half-hour block.
Every change you make is recorded, and the person can undo the last one.

Keep your own notes in this folder: what you know about the person and how things stand in state.md, and what
happened each day in history/<YYYY-MM-DD>.md. Read them at the start of a conversation and keep them current.
Nothing else here is the person's; don't look outside this folder.

The app shows schedules to the person in half-hour blocks. When you plan, don't quote the minute times you picked
yourself ("10:20"); name the block or the order ("in the 10:00 block", "after lunch"). Times the person gave you,
or that are on their calendar, are fine to say as they are.

Write replies as plain chat text: short, no tables, no markdown headings.
"""


def agent_dir() -> Path:
    """The workspace, created 0700 (and tightened to 0700 if it was looser)."""
    override = os.environ.get(AGENT_DIR_ENV, "").strip()
    path = Path(os.path.expanduser(override)) if override else \
        Path.home() / ".local" / "share" / "thirties" / WORKSPACE_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    (path / "history").mkdir(exist_ok=True, mode=0o700)
    return path


def _write_if_changed(path: Path, text: str, mode: int = 0o600) -> None:
    if not path.is_file() or path.read_text() != text:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text)
    os.chmod(path, mode)


def mcp_schema_dir() -> Path:
    """Where agy keeps the schemas of this server's tools."""
    return Path.home() / ".gemini" / "antigravity-cli" / "mcp" / SERVER_NAME


def prepare_workspace(ws: Path, python: str = "python3") -> Path:
    """Write the instructions, the tool server and agy's config into the workspace. Returns the settings file."""
    _write_if_changed(ws / "AGENTS.md", BRIEF)
    server = ws / "bin" / "tools_server.py"
    _write_if_changed(server, (Path(__file__).parent / "tools_server.py").read_text(), 0o700)
    mcp = {"mcpServers": {SERVER_NAME: {"command": python, "args": ["-I", str(server), str(ws / SOCKET_NAME)]}}}
    _write_if_changed(ws / AGY_DIR / "mcp_config.json", json.dumps(mcp, indent=2) + "\n")
    settings = ws / "agy-settings.json"
    # agy keeps each MCP tool's schema under ~/.gemini/antigravity-cli/mcp/<server>/ and reads it before a call; the
    # workspace is the only place it may write (its own notes). Headless runs soft-deny anything not allowed here.
    schemas = mcp_schema_dir()
    allow = [f"mcp({SERVER_NAME}/*)"]
    reads, writes = "read_file", "write_file"
    grants = ((reads, schemas), (reads, ws), (writes, ws))
    for rule, place in grants:
        # the permission check may see the resolved path (a home behind a symlink): both forms are granted
        for form in dict.fromkeys((str(place), os.path.realpath(place))):
            allow.append(f"{rule}({form}/**)")
    state = ws / "state.md"  # its notes exist from the start, so a first read finds them instead of failing
    if not state.exists():
        _write_if_changed(state, "# State\n\n(Nothing noted yet.)\n")
    _write_if_changed(settings, json.dumps({"permissions": {"allow": allow}}, indent=2) + "\n")
    return settings


def model_name(model: str, effort: str = DEFAULT_EFFORT) -> str:
    """The name agy takes: the model with its effort level ("<model>-<level>"), unless it already has one."""
    from thirties_core.inference import split_thinking_level
    base, level = split_thinking_level(model)
    if level or not effort or effort not in ("low", "medium", "high"):
        return model
    return f"{model}-{effort}"


class Conversations:
    """The agy conversation id for each day, kept in the workspace."""

    def __init__(self, ws: Path) -> None:
        self.path = ws / "conversations.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, day: date) -> Optional[str]:
        return self._load().get(day.isoformat())

    def set(self, day: date, conversation_id: str) -> None:
        data = self._load()
        data[day.isoformat()] = conversation_id
        _write_if_changed(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def forget(self, day: date) -> None:
        data = self._load()
        if data.pop(day.isoformat(), None) is not None:
            _write_if_changed(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")


@dataclass
class TurnResult:
    ok: bool
    reply: str = ""
    error: str = ""
    conversation_id: str = ""
    quota: bool = False


def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))


def agy_prefix(agy_bin: str, ws: Path) -> List[str]:
    """agy itself, run on the host from inside the Flatpak (as inference.py does)."""
    cmd = [agy_bin]
    if in_flatpak():
        cmd = ["flatpak-spawn", "--host", f"--directory={ws}"] + cmd
    return cmd


def build_command(agy_bin: str, ws: Path, settings: Path, model: str, conversation_id: Optional[str]) -> List[str]:
    cmd = agy_prefix(agy_bin, ws) + [
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", model,
        "--sandbox",
        "--add-dir", str(ws),
        # agy keeps the tools' schemas here and reads one before each call; as part of the workspace the read is
        # granted without a prompt (an allow rule alone doesn't always cover it in headless runs)
        "--add-dir", str(mcp_schema_dir()),
        "--settings", str(settings),
        "--disable-slash-commands",
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    return cmd


def _events(stdout: str) -> List[dict]:
    out = []
    for line in stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def read_output(stdout: str) -> tuple:
    """(result dict or None, conversation id) from agy's stream-json output."""
    result, conv = None, ""
    for ev in _events(stdout):
        conv = str(ev.get("conversation_id") or (ev.get("result") or {}).get("conversation_id")
                   or (ev.get("step_update") or {}).get("conversation_id") or conv)
        if ev.get("event") == "result":
            result = ev.get("result") or {}
    return result, conv


class AgentSession:
    """Runs turns for one person: ``run(text, day)``. ``runner`` stands in for subprocess.run in tests."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT, agy_path: Optional[str] = None,
                 runner=None, workspace: Optional[Path] = None, effort: str = DEFAULT_EFFORT) -> None:
        self.model = model_name(model or DEFAULT_MODEL, effort)
        self.timeout = timeout
        self.agy_path = agy_path
        self._runner = runner or subprocess.run
        self.ws = workspace or agent_dir()
        self.conversations = Conversations(self.ws)

    @property
    def socket_path(self) -> Path:
        return self.ws / SOCKET_NAME

    def agy_binary(self) -> Optional[str]:
        from thirties_core.inference import find_agy_binary
        return find_agy_binary(self.agy_path)

    def run(self, text: str, day: date) -> TurnResult:
        result = self._run_once(text, day)
        if not result.ok and not result.quota and self.conversations.get(day) \
                and "conversation" in result.error.lower() and "not found" in result.error.lower():
            self.conversations.forget(day)  # the day's conversation is gone on agy's side: start a new one
            result = self._run_once(text, day)
        if result.ok and not result.reply.strip() and self.conversations.get(day):
            # a turn that ended without a reply (a soft-denied step ends it): ask once, in the same conversation
            again = self._run_once(NO_REPLY_NUDGE, day)
            if again.ok and again.reply.strip():
                return again
        return result

    def _run_once(self, text: str, day: date) -> TurnResult:
        from thirties_core.inference import AGY_QUOTA_RE, agy_outcome, record_agy_call
        agy_bin = self.agy_binary()
        if not agy_bin:
            return TurnResult(False, error="agy isn't installed on this computer.")
        settings = prepare_workspace(self.ws, python=shutil.which("python3") if not in_flatpak() else "python3")
        # the day's history note exists before the turn: agy's permission check can't approve reading a missing file,
        # and a denied read ends the whole turn with no reply
        for d in dict.fromkeys((day, date.today())):
            note = self.ws / "history" / f"{d.isoformat()}.md"
            if not note.exists():
                _write_if_changed(note, f"# {d.isoformat()}\n")
        conv = self.conversations.get(day)
        message = text if conv else f"{BRIEF}\n---\n\n{text}"  # a new conversation also gets its instructions inline
        cmd = build_command(agy_bin, self.ws, settings, self.model, conv)
        stdin = json.dumps({"event": "user", "message": {"content": message}}) + "\n"
        try:
            res = self._runner(cmd, input=stdin, capture_output=True, text=True, timeout=self.timeout, cwd=self.ws)
        except subprocess.TimeoutExpired:
            record_agy_call(self.model, "timeout", caller=CALLER)
            return TurnResult(False, error="No reply came back in time.")
        except OSError as e:
            return TurnResult(False, error=f"agy couldn't be started: {e}")
        outcome = agy_outcome(res)
        record_agy_call(self.model, outcome, caller=CALLER)
        result, conv_id = read_output(res.stdout if isinstance(res.stdout, str) else "")
        if conv_id and conv_id != conv:
            self.conversations.set(day, conv_id)
        if outcome == "ok" and result is not None:
            return TurnResult(True, reply=str(result.get("response", "")).strip(), conversation_id=conv_id)
        error = str((result or {}).get("error") or res.stderr or "")[-600:]
        return TurnResult(False, error=error or "agy failed", conversation_id=conv_id,
                          quota=outcome == "quota" or bool(AGY_QUOTA_RE.search(error)))
