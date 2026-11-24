"""The bells, check-ins and other messages really reach the desktop and the phone: the app's own desktop notification
path (GNotification, through the session bus to a notification server) and the Telegram client (HTTP sendMessage),
each to a stand-in that records what it got. One afternoon for the bells, and one day at each message level.

Run with ``python3 tests/headless.py`` (a private headless session with its own session bus); skipped anywhere
else. The notification server is tests/fake_notifications.py on that private bus; Telegram is a local HTTP server.
The day is invented: an afternoon at a map maker's desk.
"""

from __future__ import annotations

import functools
import json
import os
import re
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


_STANDS_IN = SimpleNamespace()  # the notification server, the Telegram stand-in and the app, once per run


def setUpModule():
    if not PRIVATE:
        return
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    tmp = Path(tempfile.mkdtemp(prefix="thirties_notices_"))
    record = tmp / "desktop.jsonl"
    server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_notifications.py")), str(record)])
    deadline = time.monotonic() + 10
    while not Path(str(record) + ".ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    http = HTTPServer(("127.0.0.1", 0), _Telegram)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    app = Gio.Application(application_id="tech.redfoxlabs.Thirties", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    _STANDS_IN.__dict__.update(tmp=tmp, record=record, server=server, http=http, app=app)


def tearDownModule():
    if not PRIVATE or not hasattr(_STANDS_IN, "http"):
        return
    _STANDS_IN.http.shutdown()
    _STANDS_IN.server.terminate()
    _STANDS_IN.server.wait(timeout=10)
    shutil.rmtree(_STANDS_IN.tmp, ignore_errors=True)


class _Service(unittest.TestCase):
    """The background service with the app's desktop path and the Telegram client, each to its stand-in."""

    def setUp(self):
        self.record, self.http, self.app = _STANDS_IN.record, _STANDS_IN.http, _STANDS_IN.app
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


@unittest.skipUnless(PRIVATE, "needs the private headless session (tests/headless.py) and its own session bus")
class NoticesReachDesktopAndPhone(_Service):

    def test_each_thirtys_bell_reaches_the_desktop_and_the_phone(self):
        self.run_day("14:50", "17:40")
        want = ["🔔 3:00 PM: Plot the coastline", "🔔 3:30 PM: Hatch the hachures",
                "🔔 4:00 PM: Letter the cartouche", "🔔 4:30 PM: Shade the contours"]
        phone = [m["text"].split("\n")[0] for m in _Telegram.got if m["chat_id"] == CHAT]
        desk = [n["body"].split("\n")[0] for n in self.desktop()]
        self.assertEqual([b for b in phone if b.startswith("🔔")], want)
        self.assertEqual([b for b in desk if b.startswith("🔔")], want)


@unittest.skipUnless(PRIVATE, "needs the private headless session (tests/headless.py) and its own session bus")
class EachLevelOnTheSameDay(_Service):
    """The same invented afternoon and evening at each message level, ticked every five minutes as the service's
    loop would: the four items back to back, a firm talk at 6:10 PM, one more item after it, a "have you started"
    the user asked for, a reminder they set for 9:20 PM, the user writing back at 4:20, 5:20 and 8:20 PM, and bed at 11:00 PM. Whatever goes out reaches both the desktop and the phone;
    each level sends what its setting says, and a recent message never swallows anything."""

    def setUp(self):
        super().setUp()
        db = self.svc.scheduler.state_db
        db.upsert_span(EventSpan("ev", "Inlet tide talk", at("18:10"), at("18:50"), SpanCategory.CALENDAR,
                                 is_firm=True))
        db.upsert_span(EventSpan("sl", "Plot the shoal", at("19:40"), at("20:10"), SpanCategory.ACTIVITY))
        from thirties_core.contextual import ContextualFlags
        ContextualFlags(db).set("ca")  # "ask me if I've started the cartouche"
        from thirties_core.memory import ReminderStore
        ReminderStore(db).add(at("21:20"), "Plot the graticule")  # a reminder the user asked for
        self.svc._manager = None

    def day(self, level):
        self.svc.config.assistant.message_level = level
        t = at("14:30")
        while t <= at("23:30"):
            set_debug_time(t)
            if t in (at("16:20"), at("17:20"), at("20:20")):
                mark_up(self.svc.scheduler.state_db, t)  # the user writes back
            self.svc.tick(t)
            self._pump()
            t += timedelta(minutes=5)
        phone = [m["text"].split("\n")[0] for m in _Telegram.got if m["chat_id"] == CHAT]
        desk = [n["body"].split("\n")[0] for n in self.desktop()]
        self.assertEqual(len(desk), len(phone), (desk, phone))
        for d, ph in zip(desk, phone):  # the same words (the phone shows names in bold)
            self.assertEqual(d, ph.replace("<b>", "").replace("</b>", ""))
        mornings = [d for d in desk if d.startswith("☀️")]  # the first message of the day, at every level but off
        self.assertEqual(len(mornings), 0 if level == "off" else 1, desk)
        return [d for d in desk if not d.startswith("☀️")]

    TITLES = [title for _, title, _, _ in ITEMS] + ["Inlet tide talk", "Plot the shoal", "Plot the graticule"]

    def shapes(self, level):
        """Each message as its sign (and clock, for a bell or a stretch) with the invented items it names, in order."""
        out = []
        for text in self.day(level):
            head = re.match(r"(\S+)(?: (Since|Until|Reminder:))?(?: \(?(\d{1,2}:\d\d [AP]M))?", text)
            named = sorted((text.index(t), t) for t in self.TITLES if t in text)
            out.append((" ".join(x for x in head.groups() if x), tuple(t for _, t in named)))
        return out

    def test_heavy_a_bell_at_each_thirty_and_a_check_in_for_each_item(self):
        coast, hatch, cart, cont = (title for _, title, _, _ in ITEMS)
        self.assertEqual(self.shapes("heavy"), [
            ("🔔 3:00 PM", (coast,)),
            ("🔔 3:30 PM", (hatch,)),
            ("🔔 4:00 PM", (cart,)),
            ("👋", (cart, coast)),         # has the next one started, or still on the coastline?
            ("👀", (cart,)),               # the one the user asked to be asked about
            ("🔔 4:30 PM", (cont,)),       # ten minutes after the user wrote
            ("👋", (cart, hatch)),
            ("👋", (cont, cart)),
            ("👋", (cont,)),
            ("⏰", ("Inlet tide talk", cart, cont)),  # the heads-up, with what isn't reported yet
            ("🔔 6:00 PM", ("Inlet tide talk",)),
            ("🔔 7:30 PM", ("Plot the shoal",)),
            ("👋", ("Plot the shoal",)),
            ("⏰ Reminder:", ("Plot the graticule",)),
            ("🌙", ())])                   # bedtime, at 11:00 PM

    def test_medium_each_stretchs_start_and_one_question_after_it(self):
        coast, hatch, cart, cont = (title for _, title, _, _ in ITEMS)
        self.assertEqual(self.shapes("medium"), [
            ("👀", (cart,)),
            ("🗓 Until 6:00 PM", (cont,)),
            ("👋 Since 2:00 PM", (coast, hatch, cart)),
            ("👋 Since 4:30 PM", (cont,)),
            ("⏰", ("Inlet tide talk", cart, cont)),
            ("🗓 Until 9:00 PM", ("Plot the shoal",)),
            ("👋", ("Plot the shoal",)),   # the evening's one catch-up
            ("⏰ Reminder:", ("Plot the graticule",))])

    def test_light_a_heads_up_before_the_event_and_what_the_user_asked_for(self):
        cart, cont = ITEMS[2][1], ITEMS[3][1]
        self.assertEqual(self.shapes("light"), [("👀", (cart,)), ("⏰", ("Inlet tide talk", cart, cont)),
                                                ("⏰ Reminder:", ("Plot the graticule",))])

    def test_off_only_the_reminder_the_user_set(self):
        self.assertEqual(self.shapes("off"), [("⏰ Reminder:", ("Plot the graticule",))])

if __name__ == "__main__":
    unittest.main()
