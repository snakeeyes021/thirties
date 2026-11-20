"""The app's side of Gemini's tools: a Unix socket in Gemini's workspace, served while a turn runs.

The MCP server agy starts (tools_server.py) connects here for each tool call and sends one JSON line,
{"tool": name, "args": {...}}; the reply is one JSON line, {"result": text, "error": bool}. The socket is created
mode 0600 inside the 0700 workspace, and removed when the turn ends.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ToolBridge:
    def __init__(self, socket_path: Path, call: Callable[[str, dict], str]) -> None:
        self.path = Path(socket_path)
        self._call = call
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def __enter__(self) -> "ToolBridge":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        sock.listen(4)
        sock.settimeout(0.2)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="AgentToolBridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:  # one call at a time: the tools write the person's data in order
                try:
                    self._one(conn)
                except Exception as e:
                    logger.warning("A tool call from Gemini failed: %s", e)

    def _one(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            request = json.loads(data.decode() or "{}")
        except ValueError:
            request = {}
        text = self._call(str(request.get("tool", "")), request.get("args") or {})
        try:
            failed = "error" in json.loads(text)
        except (ValueError, TypeError):
            failed = False
        conn.sendall((json.dumps({"result": text, "error": failed}) + "\n").encode())
