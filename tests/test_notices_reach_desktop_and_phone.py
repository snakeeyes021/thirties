"""The bells really reach the desktop and the phone: the app's own desktop notification path (GNotification, through
the session bus to a notification server) and the Telegram client (HTTP sendMessage), each to a stand-in that
records what it got.

Run with ``python3 tests/headless.py`` (a private headless session with its own session bus); skipped anywhere
else. The notification server is tests/fake_notifications.py on that private bus; Telegram is a local HTTP server.
The day is invented: an afternoon at a map maker's desk.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.fixtures import DAY, at, make_scheduler, mark_up
from tests.test_gui import GTK
from thirties_core.astronomy import set_debug_time
from thirties_core.background import BackgroundService
from thirties_core.inference import MockInferenceEngine
from thirties_core.models import EventSpan, SpanCategory

BUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
PRIVATE = bool(GTK) and bool(BUS) and "/run/user/" not in BUS
CHAT = 4242
ITEMS = (
    ("co", "Plot the coastline", "15:10", "15:40"),
    ("ha", "Hatch the hachures", "15:40", "16:10"),
    ("ca", "Letter the cartouche", "16:10", "16:50"),
    ("cn", "Shade the contours", "16:50", "17:20"),
)

if PRIVATE:
    from gi.repository import Gio, GLib
    from thirties.main import ThirtiesApplication  # the package symlink is set up in tests.test_gui


class _Telegram(BaseHTTPRequestHandler):
    """Telegram's sendMessage, recorded; every other method answers ok."""
    got: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/sendMessage"):
            type(self).got.append(body)
        out = json.dumps({"ok": True, "result": {"message_id": len(type(self).got)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@unittest.skipUnless(PRIVATE, "needs the private headless session (tests/headless.py) and its own session bus")
class NoticesReachDesktopAndPhone(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.tmp = Path(tempfile.mkdtemp(prefix="thirties_notices_"))
        cls.record = cls.tmp / "desktop.jsonl"
        cls.server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_notifications.py")),
                                       str(cls.record)])
        deadline = time.monotonic() + 10
        while not Path(str(cls.record) + ".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cls.http = HTTPServer(("127.0.0.1", 0), _Telegram)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.app = Gio.Application(application_id="tech.redfoxlabs.Thirties",
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.server.terminate()
        cls.server.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.record.write_text("")
        _Telegram.got = []
        set_debug_time(at("07:00"))
        self.addCleanup(set_debug_time, None)
        sched = make_scheduler()
        cfg = sched.config
        cfg.assistant.message_level = "heavy"
        cfg.telegram.notify_block_transitions = True
        cfg.general.work_start_thirty, cfg.general.work_end_thirty = 18, 22  # 9:00 - 11:00 AM, out of the way
        for sid, title, start, end in ITEMS:
            sched.state_db.upsert_span(EventSpan(sid, title, at(start), at(end), SpanCategory.ACTIVITY))
        joplin = MagicMock()
        joplin.fetch_tasks.return_value, joplin.fetch_active_notes.return_value = [], []
        notify = functools.partial(ThirtiesApplication.notify_from_background, self.app)  # the app's own path
        self.svc = BackgroundService(cfg, notify, scheduler=sched, joplin_engine=joplin,
                                     inference_engine=MockInferenceEngine(), sync_engine=MagicMock())
        self.svc.today = lambda: DAY
        self.svc.run_in_threads = False
        self.svc.sort_lists_enabled = False
        from thirties_core.telegram_bridge import TelegramClient, ThirtiesTelegramBridge
        bridge = SimpleNamespace(client=TelegramClient("1:test", base_url=f"http://127.0.0.1:{self.http.server_port}"),
                                 allowed_chat_ids=[CHAT])
        bridge.broadcast = functools.partial(ThirtiesTelegramBridge.broadcast, bridge)  # the bridge's own send
        self.svc.bridge = bridge
        mark_up(sched.state_db, at("07:40"))

    def run_day(self, start, end):
        t = at(start)
        while t <= at(end):
            set_debug_time(t)
            self.svc.tick(t)
            self._pump()
            t += timedelta(minutes=5)

    def _pump(self, timeout=2.0):
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = False
            while ctx.pending():
                ctx.iteration(False)
                busy = True
            if not busy:
                time.sleep(0.02)
                if not ctx.pending():
                    break

    def desktop(self):
        deadline = time.monotonic() + 5
        want = len(_Telegram.got)
        while time.monotonic() < deadline:
            self._pump(0.1)
            lines = [json.loads(x) for x in self.record.read_text().splitlines() if x.strip()]
            if len(lines) >= want:
                return lines
        return lines

    def test_each_thirtys_bell_reaches_the_desktop_and_the_phone(self):
        self.run_day("14:50", "17:40")
        want = ["🔔 3:00 PM: Plot the coastline", "🔔 3:30 PM: Hatch the hachures",
                "🔔 4:00 PM: Letter the cartouche", "🔔 4:30 PM: Shade the contours"]
        phone = [m["text"].split("\n")[0] for m in _Telegram.got if m["chat_id"] == CHAT]
        desk = [n["body"].split("\n")[0] for n in self.desktop()]
        print("\nphone:", phone, "\ndesktop:", desk, file=sys.stderr)
        self.assertEqual([b for b in phone if b.startswith("🔔")], want)
        self.assertEqual([b for b in desk if b.startswith("🔔")], want)


if __name__ == "__main__":
    unittest.main()
