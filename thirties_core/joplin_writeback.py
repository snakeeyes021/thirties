"""Writing back to Joplin: checking tasks off, adding tasks, breaking a task into subtasks.

Every change goes through Joplin's local Data API (the Web Clipper service),
never into its files. Joplin only answers while it's running, so changes go
through an outbox: delivered at once when Joplin is up, otherwise kept and
retried (and, if the user allows it, Joplin is started to receive them).

The note-body edits are pure functions over Markdown checklists, so they keep
the user's indentation and touch only the lines they mean to.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.joplin_engine import CHECKBOX_REGEX, DONE_CHECKBOX_REGEX, HEADING_REGEX, JoplinAPIClient

logger = logging.getLogger(__name__)


# --- note body edits (pure) ---

def _open_item(line: str, title: str, ticked_too: bool = False) -> Optional[re.Match]:
    m = CHECKBOX_REGEX.match(line) or (DONE_CHECKBOX_REGEX.match(line) if ticked_too else None)
    return m if m and m.group(2).strip() == title.strip() else None


def check_off(body: str, title: str, occurrence: int = 0) -> Optional[str]:
    """Tick the open checklist item with this text (the n-th one if repeated); None if it's gone."""
    lines = body.splitlines(keepends=True)
    seen = 0
    for i, line in enumerate(lines):
        m = _open_item(line.rstrip("\r\n"), title)
        if m:
            if seen == occurrence:
                ending = line[len(line.rstrip("\r\n")):]
                lines[i] = f"{m.group(1)}- [x] {m.group(2).strip()}{ending}"
                return "".join(lines)
            seen += 1
    return None


def _indent_unit(lines: List[str]) -> str:
    """The note's own nesting step (tab, 2 or 4 spaces), from its existing nested items."""
    widths = []
    for line in lines:
        m = CHECKBOX_REGEX.match(line) or DONE_CHECKBOX_REGEX.match(line)
        if m and m.group(1):
            if "\t" in m.group(1):
                return "\t"
            widths.append(len(m.group(1)))
    return " " * min(widths) if widths else "    "


def insert_subtasks(body: str, parent_title: Optional[str], steps: List[str], occurrence: int = 0,
                    ticked_too: bool = False) -> Optional[str]:
    """Add steps as open items nested under a checklist item, after any children it already has.

    With ``parent_title`` None (a to-do note broken down), the steps go at the end of the note.
    ``ticked_too`` lets the parent be a ticked item (a routine note's items stay ticked between days).
    Returns None if the parent item is gone.
    """
    steps = [s.strip() for s in steps if s.strip()]
    lines = body.splitlines()
    if parent_title is None:
        return append_task(body, steps[0], None, more=steps[1:])
    unit = _indent_unit(lines)
    seen = 0
    for i, line in enumerate(lines):
        m = _open_item(line, parent_title, ticked_too)
        if not m:
            continue
        if seen < occurrence:
            seen += 1
            continue
        indent = m.group(1)
        width = len(indent.replace("\t", "    "))
        end = i + 1
        while end < len(lines):
            child = CHECKBOX_REGEX.match(lines[end]) or DONE_CHECKBOX_REGEX.match(lines[end])
            if child and len(child.group(1).replace("\t", "    ")) > width:
                end += 1
            elif not lines[end].strip() and end + 1 < len(lines) and lines[end + 1].startswith(indent + unit):
                end += 1
            else:
                break
        new = [f"{indent}{unit}- [ ] {s}" for s in steps]
        out = lines[:end] + new + lines[end:]
        return "\n".join(out) + ("\n" if body.endswith("\n") else "")
    return None


def append_task(body: str, title: str, section: Optional[str] = None, more: Optional[List[str]] = None) -> str:
    """Add open items at the end of a section's list (by heading text) or of the note."""
    items = [f"- [ ] {t.strip()}" for t in [title] + list(more or []) if t.strip()]
    lines = body.splitlines()
    start, stop = 0, len(lines)
    if section:
        heads = [(i, HEADING_REGEX.match(l).group(1).strip()) for i, l in enumerate(lines) if HEADING_REGEX.match(l)]
        here = next((n for n, (_, text) in enumerate(heads) if text.lower() == section.strip().lower()), None)
        if here is None:
            lines += ([""] if lines and lines[-1].strip() else []) + [f"## {section.strip()}"]
            start = stop = len(lines)
        else:
            start = heads[here][0] + 1
            stop = heads[here + 1][0] if here + 1 < len(heads) else len(lines)
    last = max((i for i in range(start, stop)
                if CHECKBOX_REGEX.match(lines[i]) or DONE_CHECKBOX_REGEX.match(lines[i])), default=None)
    at = last + 1 if last is not None else stop
    if last is None:
        while at > start and not lines[at - 1].strip():
            at -= 1
    out = lines[:at] + items + lines[at:]
    return "\n".join(out) + "\n"


def _item_end(lines: List[str], i: int) -> int:
    """The line after item ``i`` and the items nested under it."""
    m = CHECKBOX_REGEX.match(lines[i]) or DONE_CHECKBOX_REGEX.match(lines[i])
    width = len(m.group(1).replace("\t", "    "))
    end = i + 1
    while end < len(lines):
        child = CHECKBOX_REGEX.match(lines[end]) or DONE_CHECKBOX_REGEX.match(lines[end])
        if not (child and len(child.group(1).replace("\t", "    ")) > width):
            break
        end += 1
    return end


def remove_item(body: str, title: str) -> Optional[str]:
    """Take an item (open or ticked) and anything nested under it off the list; None if it's gone."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if _open_item(line, title, ticked_too=True):
            out = lines[:i] + lines[_item_end(lines, i):]
            return "\n".join(out) + ("\n" if body.endswith("\n") and out else "")
    return None


def untick_all(body: str) -> str:
    """Every item open again (a routine note, each morning)."""
    return "".join(re.sub(r"^([ \t]*)-\s*\[[xX]\]", r"\1- [ ]", line) for line in body.splitlines(keepends=True))


def clear_ticked(body: str) -> str:
    """Drop ticked items and what's nested under them (the day's list, once the day is over)."""
    lines = body.splitlines()
    out, i = [], 0
    while i < len(lines):
        if DONE_CHECKBOX_REGEX.match(lines[i]):
            i = _item_end(lines, i)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + ("\n" if body.endswith("\n") and out else "")


# --- launching Joplin ---

def _in_flatpak() -> bool:
    return Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))


def _host(cmd: List[str]) -> List[str]:
    return ["flatpak-spawn", "--host"] + cmd if _in_flatpak() else cmd


def joplin_launch_command() -> Optional[List[str]]:
    """How to start the user's Joplin desktop app, wherever it's installed; None if it isn't."""
    try:
        if subprocess.run(_host(["flatpak", "info", "net.cozic.joplin_desktop"]), capture_output=True,
                          timeout=5).returncode == 0:
            return _host(["flatpak", "run", "net.cozic.joplin_desktop"])
    except (OSError, subprocess.SubprocessError):
        pass
    for name in ("joplin-desktop", "joplin"):
        try:
            found = subprocess.run(_host(["sh", "-c", f"command -v {name}"]), capture_output=True, text=True, timeout=5)
            if found.returncode == 0 and found.stdout.strip():
                return _host([found.stdout.strip()])
        except (OSError, subprocess.SubprocessError):
            pass
    apps = Path.home() / "Applications"
    for image in sorted(apps.glob("Joplin*.AppImage")) if apps.is_dir() else []:
        return _host([str(image)])
    return None


def tray_available() -> bool:
    """Whether the desktop shows tray icons (GNOME needs the AppIndicator extension), so Joplin can
    start minimized to the tray instead of opening a window."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), None,
                              Gio.DBusCallFlags.NONE, 2000, None)
        return bool(reply.unpack()[0])
    except Exception:
        return False


def launch_joplin() -> bool:
    cmd = joplin_launch_command()
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as e:
        logger.warning("Could not start Joplin: %s", e)
        return False


# --- connecting (the access request Joplin shows as a Grant dialog) ---

def request_access(host: str = "127.0.0.1", port: int = 41184) -> str:
    """Ask Joplin for API access; it shows the user a dialog. Returns the request's auth token."""
    req = urllib.request.Request(f"http://{host}:{port}/auth", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())["auth_token"]


def check_access(auth_token: str, host: str = "127.0.0.1", port: int = 41184) -> Tuple[str, Optional[str]]:
    """("waiting" | "accepted" | "rejected", the API token once accepted)."""
    url = f"http://{host}:{port}/auth/check?" + urllib.parse.urlencode({"auth_token": auth_token})
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    return data.get("status", "waiting"), data.get("token")


def clipper_running(host: str = "127.0.0.1", port: int = 41184) -> bool:
    return JoplinAPIClient(host, port, "").ping()


# --- delivering changes ---

class JoplinWriter:
    """Delivers write-back operations now if Joplin is up, else queues them in the outbox."""

    def __init__(self, config, state_db, api: Optional[JoplinAPIClient] = None) -> None:
        self.cfg = config.joplin
        self.db = state_db
        self.api = api or JoplinAPIClient(self.cfg.api_host, self.cfg.api_port, self.cfg.api_token)
        self._launched_at = 0.0

    @property
    def allowed(self) -> bool:
        return bool(self.cfg.write_back and self.cfg.api_token)

    def submit(self, op: Dict[str, Any]) -> Tuple[str, str]:
        """("done" | "queued" | "failed" | "not_allowed", message). Never blocks long."""
        if not self.allowed:
            return "not_allowed", "Thirties isn't allowed to change Joplin (Preferences → Tasks)."
        if self.api.ping():
            ok, msg = self._apply(op)
            if ok or msg.startswith("gone:"):
                return ("done", msg) if ok else ("failed", msg[5:])
        self.db.outbox_add(json.dumps(op), datetime.now(timezone.utc).isoformat())
        return "queued", "Joplin isn't running; it'll be updated as soon as Joplin is open."

    def flush(self, launch: bool = True, wait_seconds: float = 45.0) -> int:
        """Deliver queued operations; returns how many were delivered. Call from a background thread."""
        pending = self.db.outbox_pending()
        if not pending or not self.allowed:
            return 0
        if not self.api.ping():
            if not (launch and self.cfg.launch_when_needed) or time.time() - self._launched_at < 600:
                return 0
            if not launch_joplin():
                return 0
            self._launched_at = time.time()
            deadline = time.time() + wait_seconds
            while time.time() < deadline and not self.api.ping():
                time.sleep(2)
            if not self.api.ping():
                return 0
        delivered = 0
        for row in pending:
            ok, msg = self._apply(json.loads(row["op"]))
            if ok:
                self.db.outbox_done(row["id"])
                delivered += 1
            elif msg.startswith("gone:"):
                self.db.outbox_fail(row["id"], msg[5:])
            else:
                self.db.outbox_attempt(row["id"], msg)
                break  # Joplin went away; keep the order
        return delivered

    # --- the operations ---

    def _edit_body(self, note_id: str, edit) -> Tuple[bool, str]:
        note = self.api.get_note(note_id, fields="id,title,body")
        if not note or "body" not in note:
            return False, "Couldn't read the note from Joplin."
        new = edit(note["body"])
        if new is None:
            return False, "gone: the item isn't in the note any more (it was changed in Joplin)."
        if not self.api.update_note(note_id, {"body": new}):
            return False, "Joplin didn't accept the change."
        return True, ""

    def _apply(self, op: Dict[str, Any]) -> Tuple[bool, str]:
        kind = op.get("op")
        if kind == "complete":
            if op.get("is_checkbox_item"):
                return self._edit_body(op["note_id"], lambda b: check_off(b, op["title"], op.get("occurrence", 0)))
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            ok = self.api.update_note(op["note_id"], {"todo_completed": now_ms})
            return ok, "" if ok else "Joplin didn't accept the change."
        if kind == "add_task":
            return self._edit_body(op["note_id"], lambda b: append_task(b, op["title"], op.get("section")))
        if kind == "remove":
            return self._edit_body(op["note_id"], lambda b: remove_item(b, op["title"]))
        if kind == "untick_all":
            return self._edit_body(op["note_id"], untick_all)
        if kind == "clear_ticked":
            return self._edit_body(op["note_id"], clear_ticked)
        if kind == "break_down":
            return self._edit_body(op["note_id"], lambda b: insert_subtasks(b, op.get("parent_title"), op["steps"],
                                                                              op.get("occurrence", 0),
                                                                              op.get("ticked_too", False)))
        return False, f"gone: unknown operation {kind!r}"


def complete_op(task) -> Dict[str, Any]:
    if task.is_checkbox_item:
        return {"op": "complete", "is_checkbox_item": True, "note_id": task.parent_note_id, "title": task.title}
    return {"op": "complete", "is_checkbox_item": False, "note_id": task.id, "title": task.title}


def break_down_op(task, steps: List[str]) -> Dict[str, Any]:
    if task.is_checkbox_item:
        return {"op": "break_down", "note_id": task.parent_note_id, "parent_title": task.title, "steps": steps}
    return {"op": "break_down", "note_id": task.id, "parent_title": None, "steps": steps}
