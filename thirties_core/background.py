"""What Thirties does while no window is open: messages, Telegram, Joplin delivery, sync.

The desktop app runs this in-process and keeps running in the background (with
the user's permission, through the Background portal), so it works with the
window closed. Every few seconds it:

- asks about planned items that ended with no outcome (once each, unless muted),
- delivers reminders the user asked for,
- says when a planned item starts,
- delivers queued changes to Joplin, and syncs if sync is on.

Messages go to the desktop (a callback the app provides; GNOME's per-app
notification switch turns those off) and to Telegram when a phone is paired and
the bridge runs on this computer. Each message is also added to the day's
conversation, so the reply can come from either place.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.backlog import load_backlog
from thirties_core.clock import format_clock
from thirties_core.conversation import ConversationManager
from thirties_core.models import SpanCategory, SpanStatus

logger = logging.getLogger(__name__)

DesktopNotify = Callable[[str, str, str], None]  # (title, body, kind) -> None


class BackgroundService:
    TICK_SECONDS = 15
    OUTBOX_EVERY = 60
    SYNC_EVERY = 300

    def __init__(self, config, desktop_notify: Optional[DesktopNotify] = None, *, scheduler=None, joplin_engine=None,
                 inference_engine=None, sync_engine=None) -> None:
        from thirties_core.inference import get_inference_engine
        from thirties_core.joplin_engine import JoplinEngine
        from thirties_core.scheduler import DeterministicScheduler
        from thirties_core.sync_engine import SyncEngine
        self.config = config
        self.desktop_notify = desktop_notify
        self.scheduler = scheduler or DeterministicScheduler(config)
        self.joplin_engine = joplin_engine or JoplinEngine(config)
        self.inference_engine = inference_engine or get_inference_engine(config)
        self.sync_engine = sync_engine or SyncEngine(config=config, state_db=self.scheduler.state_db,
                                                     joplin_engine=self.joplin_engine)
        self.bridge = None
        self._day: Optional[date] = None
        self._manager: Optional[ConversationManager] = None
        self._lock = threading.RLock()
        self._last_buzz_span: Optional[str] = None
        self._last_outbox = 0.0
        self._last_sync = 0.0
        self._threads: List[threading.Thread] = []
        self.running = False

    # --- the day's conversation (shared with the window and Telegram through the database) ---

    def today(self) -> date:
        gen = self.config.general
        return get_diurnal_date(lat=gen.latitude, lon=gen.longitude, tz_name=gen.timezone)

    def manager(self) -> ConversationManager:
        with self._lock:
            today = self.today()
            if self._manager is None or today != self._day:
                tasks, notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
                plan, pending = self.scheduler.build_day_plan(today)
                self._manager = ConversationManager(plan, tasks, pending, self.scheduler, joplin_engine=self.joplin_engine,
                                                    inference_engine=self.inference_engine, active_notes=notes)
                self._day = today
            return self._manager

    def now(self) -> datetime:
        return get_current_time(self.manager().day_plan.day_start.tzinfo)

    # --- delivering a message ---

    def send(self, text: str, kind: str, title: str = "Thirties", telegram_text: Optional[str] = None,
             to_conversation: bool = True) -> None:
        """Send a message to the desktop and, if a phone is paired here, to Telegram."""
        if to_conversation:
            try:
                manager = self.manager()
                manager.refresh()
                manager.add_assistant_message(text)
            except Exception as e:
                logger.warning("Couldn't add the message to the conversation: %s", e)
        if self.desktop_notify is not None:
            try:
                self.desktop_notify(title, text, kind)
            except Exception as e:
                logger.warning("Desktop notification failed: %s", e)
        if self.bridge is not None:
            self.bridge.broadcast(telegram_text or text)

    # --- duties ---

    def _asked(self) -> Dict[str, str]:
        raw = self.scheduler.state_db.get_sync_meta("checkins_asked")
        return json.loads(raw) if raw else {}

    def check_ins(self, now: datetime) -> None:
        cfg = self.config.assistant
        if not cfg.checkins:
            return
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        manager = self.manager()
        manager.refresh()
        plan = manager.day_plan
        asked = self._asked()
        grace = timedelta(minutes=cfg.checkin_after_minutes)
        due = [s for s in plan.active_spans()
               if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and s.status == SpanStatus.PLANNED
               and s.id not in asked and s.end_dt + grace <= now < s.end_dt + timedelta(hours=2)
               and not mutes.is_muted(s.id, now)]
        if not due:
            return
        span = due[-1]
        for s in due:
            asked[s.id] = now.date().isoformat()
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        self.scheduler.state_db.set_sync_meta("checkins_asked",
                                              json.dumps({k: v for k, v in asked.items() if v >= cutoff}))
        following = next((s for s in plan.active_spans()
                          if s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and s.covers(now)), None)
        question = f"👋 Just checking in: did you get to {span.title}"
        question += f", or are you still on it? (Next up: {following.title}.)" if following else "?"
        self.send(question, "checkin", telegram_text=question.replace(span.title, f"*{span.title}*", 1))

    def reminders(self, now: datetime) -> None:
        from thirties_core.memory import ReminderStore
        store = ReminderStore(self.scheduler.state_db)
        for rem in store.due(now):
            store.set_state(rem.id, "sent")  # first, so a crash can't repeat it forever
            if now - rem.due > timedelta(hours=12):
                text = f"⏰ Reminder (from {rem.due.astimezone(now.tzinfo).strftime('%a')} " \
                       f"{format_clock(rem.due.astimezone(now.tzinfo))}): {rem.text}"
            else:
                text = f"⏰ Reminder: {rem.text}"
            self.send(text, "reminder")

    def starts(self, now: datetime) -> None:
        """Say when a planned item (or bedtime) starts."""
        if not self.config.telegram.notify_block_transitions:
            return
        plan = self.manager().day_plan
        starting = [s for s in plan.active_spans()
                    if s.start_dt <= now < s.start_dt + timedelta(minutes=2) and not s.tentative
                    and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK, SpanCategory.CALENDAR, SpanCategory.SLEEP)]
        for span in starting:
            if span.id == self._last_buzz_span:
                continue
            self._last_buzz_span = span.id
            if span.category == SpanCategory.SLEEP:
                text = f"🌙 Bedtime ({format_clock(span.start_dt)}). Wind down and rest well!"
            else:
                text = f"🔔 {span.title} starts now ({format_clock(span.start_dt)} – {format_clock(span.end_dt)})"
            self.send(text, "start", to_conversation=False)

    def deliver_outbox(self) -> None:
        if time.time() - self._last_outbox < self.OUTBOX_EVERY:
            return
        self._last_outbox = time.time()
        from thirties_core.joplin_writeback import JoplinWriter
        try:
            delivered = JoplinWriter(self.config, self.scheduler.state_db, self.joplin_engine.api_client).flush()
            if delivered:
                logger.info("Delivered %d queued change(s) to Joplin.", delivered)
        except Exception as e:
            logger.warning("Joplin delivery failed: %s", e)

    def sync(self) -> None:
        if not self.config.sync.enabled or time.time() - self._last_sync < self.SYNC_EVERY:
            return
        self._last_sync = time.time()
        try:
            self.sync_engine.sync()
        except Exception as e:
            logger.warning("Background sync failed: %s", e)

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or self.now()
        for duty in (self.check_ins, self.reminders, self.starts):
            try:
                duty(now)
            except Exception as e:
                logger.exception("Background duty %s failed: %s", duty.__name__, e)
        self.deliver_outbox()
        self.sync()

    # --- running ---

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        tg = self.config.telegram
        if tg.enabled and tg.bot_token and tg.allowed_chat_ids:
            from thirties_core.telegram_bridge import ThirtiesTelegramBridge
            try:
                self.bridge = ThirtiesTelegramBridge(config=self.config, service=self)
                t = threading.Thread(target=self.bridge.poll_forever, daemon=True, name="TelegramPoll")
                t.start()
                self._threads.append(t)
            except Exception as e:
                logger.warning("Telegram bridge didn't start: %s", e)
                self.bridge = None
        t = threading.Thread(target=self._loop, daemon=True, name="ThirtiesBackground")
        t.start()
        self._threads.append(t)
        logger.info("Background service started (Telegram %s).", "on" if self.bridge else "off")

    def _loop(self) -> None:
        while self.running:
            self.tick()
            for _ in range(self.TICK_SECONDS * 2):
                if not self.running:
                    return
                time.sleep(0.5)

    def stop(self) -> None:
        self.running = False
        if self.bridge is not None:
            self.bridge.stop()
        self.bridge = None
        self._threads = []
