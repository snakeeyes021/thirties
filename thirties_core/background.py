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
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.backlog import load_backlog
from thirties_core.clock import format_clock, thirty_of
from thirties_core.conversation import ConversationManager
from thirties_core.models import SpanCategory, SpanStatus

logger = logging.getLogger(__name__)

DesktopNotify = Callable[[str, str, str], None]  # (title, body, kind) -> None


class BackgroundService:
    TICK_SECONDS = 15
    OUTBOX_EVERY = 60
    SYNC_EVERY = 300
    LISTS_EVERY = 1800

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
        self._last_outbox = 0.0
        self._last_sync = 0.0
        self._last_lists = 0.0
        self._sorting = False
        self.sort_lists_enabled = not os.environ.get("THIRTIES_OFFLINE")  # off in tests and evals
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
                try:
                    from thirties_core.memory import ConversationStore
                    ConversationStore(self.scheduler.state_db).summarize_finished(today)
                except Exception as e:
                    logger.warning("Summarizing finished conversations failed: %s", e)
            return self._manager

    def now(self) -> datetime:
        return get_current_time(self.manager().day_plan.day_start.tzinfo)

    # --- delivering a message ---

    def send(self, text: str, kind: str, title: str = "Thirties", telegram_text: Optional[str] = None,
             to_conversation: bool = True, now: Optional[datetime] = None) -> None:
        """Send a message to the desktop and, if a phone is paired here, to Telegram."""
        from thirties_core.clock import trim_meridiems
        try:
            now = now or self.now()
            text = trim_meridiems(text, now)
            telegram_text = trim_meridiems(telegram_text, now) if telegram_text else None
        except Exception:
            pass
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

    def _remembered(self, key: str) -> Dict[str, str]:
        raw = self.scheduler.state_db.get_sync_meta(key)
        return json.loads(raw) if raw else {}

    def _remember(self, key: str, done: Dict[str, str], now: datetime) -> None:
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        self.scheduler.state_db.set_sync_meta(key, json.dumps({k: v for k, v in done.items() if v >= cutoff}))

    @staticmethod
    def _plannable(span) -> bool:
        return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK)

    def check_ins(self, now: datetime) -> None:
        """Once a thirty is over (plus the grace period), ask in one message about the items in it that
        have no outcome: never twice about an item, never about one already reported or muted."""
        cfg = self.config.assistant
        if not cfg.checkins:
            return
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        manager = self.manager()
        manager.refresh()
        plan = manager.day_plan
        asked = self._remembered("checkins_asked")
        grace = timedelta(minutes=cfg.checkin_after_minutes)

        def thirty_end(span):  # an item is over when the thirty it ends in is over
            end = thirty_of(span.end_dt - timedelta(seconds=1)) + timedelta(minutes=30)
            return max(end, span.end_dt)
        due = [s for s in plan.active_spans()
               if self._plannable(s) and s.status == SpanStatus.PLANNED and s.id not in asked
               and thirty_end(s) + grace <= now < thirty_end(s) + timedelta(hours=2) and not mutes.is_muted(s.id, now)]
        if not due:
            return
        for s in due:
            asked[s.id] = now.date().isoformat()
        self._remember("checkins_asked", asked, now)
        titles = [s.title for s in sorted(due, key=lambda s: s.start_dt)]
        if len(titles) == 1:
            question = f"👋 Just checking in: did you get to {titles[0]}, or are you still on it?"
            bold = question.replace(titles[0], f"*{titles[0]}*", 1)
        else:
            listed = ", ".join(titles[:-1]) + f" and {titles[-1]}"
            question = f"👋 How did it go? Did you get to {listed}?"
            bold = question
        self.send(question, "checkin", telegram_text=bold, now=now)

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
            self.send(text, "reminder", now=now)

    def starts(self, now: datetime) -> None:
        """At the start of a thirty, one message for what's planned to start in it; each item is announced
        at most once (moving it doesn't announce it again), and times are told in thirties."""
        if not self.config.telegram.notify_block_transitions:
            return
        plan = self.manager().day_plan
        block = thirty_of(now)
        if now - block > timedelta(minutes=5):
            return  # only right at the start of a thirty
        announced = self._remembered("starts_announced")
        starting = [s for s in plan.active_spans()
                    if thirty_of(s.start_dt) == block and s.id not in announced and not s.tentative
                    and s.status == SpanStatus.PLANNED
                    and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK, SpanCategory.CALENDAR, SpanCategory.SLEEP)]
        if not starting:
            return
        for s in starting:
            announced[s.id] = now.date().isoformat()
        self._remember("starts_announced", announced, now)
        sleep = [s for s in starting if s.category == SpanCategory.SLEEP]
        rest = sorted((s for s in starting if s.category != SpanCategory.SLEEP), key=lambda s: s.start_dt)
        if rest:
            self.send(f"🔔 {format_clock(block)}: " + ", ".join(s.title for s in rest), "start",
                      to_conversation=False, now=now)
        if sleep:
            self.send(f"🌙 Bedtime ({format_clock(block)}). Wind down and rest well!", "start",
                      to_conversation=False, now=now)

    def rollover(self, now: datetime) -> None:
        """Once a day, after waking: offer to roll yesterday's unfinished items into today."""
        db = self.scheduler.state_db
        manager = self.manager()
        day = manager.day_plan.target_date
        key = f"rollover_offered:{day.isoformat()}"
        if db.get_sync_meta(key):
            return
        gen = self.config.general
        wake = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo) + \
            timedelta(minutes=30 * gen.sleep_end_thirty + 30)
        if now < wake:
            return
        db.set_sync_meta(key, "1")
        text = manager.execute_tool("rollover_proposal", {})
        if text.startswith("Nothing from yesterday") or text.startswith("NOT_DONE"):
            return
        self.send("☀️ Good morning! " + text, "rollover", now=now)

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

    def sort_lists(self) -> None:
        """Every half hour, in its own thread: the chosen model guesses the role of new notes and the urgency
        of new items (lists.sort_lists). The user confirms or corrects them in Profile → Your lists."""
        if not self.sort_lists_enabled or self._sorting or time.time() - self._last_lists < self.LISTS_EVERY:
            return
        self._last_lists, self._sorting = time.time(), True

        def run() -> None:
            try:
                from thirties_core.brains.list_sorter import list_sorter_for
                from thirties_core.lists import ListCatalog, sort_lists
                notes, tasks = self.joplin_engine.fetch_active_notes(), self.joplin_engine.fetch_tasks()
                if not notes:
                    return
                sorter = list_sorter_for(self.config, self.scheduler.state_db, self.inference_engine)
                if sorter is None:
                    return
                counts = sort_lists(ListCatalog(self.scheduler.state_db), notes, tasks, sorter, self.today())
                if any(counts.values()):
                    logger.info("Sorted lists: %d note roles and %d item urgencies guessed.",
                                counts["roles"], counts["items"])
                    with self._lock:
                        self._manager = None  # the next turn sees them
            except Exception as e:
                logger.warning("Sorting lists failed: %s", e)
            finally:
                self._sorting = False
        threading.Thread(target=run, daemon=True, name="ListSorting").start()

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or self.now()
        for duty in (self.rollover, self.check_ins, self.reminders, self.starts):
            try:
                duty(now)
            except Exception as e:
                logger.exception("Background duty %s failed: %s", duty.__name__, e)
        self.deliver_outbox()
        self.sync()
        self.sort_lists()

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
