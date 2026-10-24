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
from thirties_core.clock import format_clock, thirty_ceil, thirty_of
from thirties_core.conversation import ConversationManager
from thirties_core.models import SpanCategory, SpanStatus

logger = logging.getLogger(__name__)

DesktopNotify = Callable[[str, str, str], None]  # (title, body, kind) -> None


LEVELS = ("off", "light", "medium", "heavy")
STRETCH_MAX = timedelta(hours=3)
QUIET_AFTER_TALK = timedelta(minutes=30)


def day_stretches(plan) -> List[tuple]:
    """The day cut into natural stretches for messages: from waking to sleep, cut at fixed commitments (calendar
    events, work), at sunset and at sleep; stretches over three hours are split evenly at thirties."""
    fixed = [s for s in plan.active_spans() if not s.tentative and
             (s.category in (SpanCategory.CALENDAR, SpanCategory.WORK) or s.is_firm)]
    sleeps = sorted((s for s in plan.spans if s.category == SpanCategory.SLEEP), key=lambda s: s.start_dt)
    wake = max([s.end_dt for s in sleeps if s.end_dt <= plan.day_start + timedelta(hours=12)] + [plan.day_start])
    bed = min([s.start_dt for s in sleeps if s.start_dt > wake] + [plan.day_end])
    def nearest_thirty(t):  # only :00 and :30 are ever said
        return thirty_of(t + timedelta(minutes=15))
    cuts = sorted({nearest_thirty(t) for t in {wake, bed, plan.sunset} | {t for s in fixed for t in (s.start_dt, s.end_dt)}})
    cuts = [t for t in cuts if nearest_thirty(wake) <= t <= nearest_thirty(bed)]
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if any(f.start_dt <= a and b <= f.end_dt for f in fixed) or b - a < timedelta(minutes=30):
            continue
        parts = -(-(b - a) // STRETCH_MAX)  # ceiling division
        step = thirty_ceil(a + (b - a) / parts) - a if parts > 1 else b - a
        t = a
        while t < b:
            end = b if b - t < step + timedelta(minutes=30) else t + step
            out.append((t, end))
            t = end
    return out


class BackgroundService:
    TICK_SECONDS = 15
    OUTBOX_EVERY = 60
    SYNC_EVERY = 300
    LISTS_EVERY = 1800
    TODAY_EVERY = 120

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
        self._last_today = 0.0
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
        from thirties_core.clock import real_times, to_thirties, trim_meridiems
        try:
            now = now or self.now()
            keep = real_times(spans=self.manager().day_plan.spans)
            text = trim_meridiems(to_thirties(text, keep), now)
            telegram_text = trim_meridiems(to_thirties(telegram_text, keep), now) if telegram_text else None
        except Exception:
            pass
        if self.desktop_notify is not None:
            try:
                self.desktop_notify(title, text, kind)
            except Exception as e:
                logger.warning("Desktop notification failed: %s", e)
        if self.bridge is not None:
            self.bridge.broadcast(telegram_text or text)
        if to_conversation:  # after delivery: this waits for a turn that's under way (Telegram) to finish
            try:
                manager = self.manager()
                with manager.turn_lock:
                    manager.refresh()
                    manager.add_assistant_message(text, kind="notice" if kind == "start" else "")
            except Exception as e:
                logger.warning("Couldn't add the message to the conversation: %s", e)

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

    # --- how much to say ---

    def level(self) -> str:
        level = getattr(self.config.assistant, "message_level", "medium")
        return level if level in LEVELS else "medium"

    def _quiet(self, now: datetime) -> bool:
        """Asleep, or in a calendar event: no messages (except reminders the user asked for)."""
        plan = self.manager().day_plan
        return any(s.start_dt <= now < s.end_dt for s in plan.active_spans() if not s.tentative
                   and (s.category == SpanCategory.SLEEP or (s.category == SpanCategory.CALENDAR and s.is_firm)))

    def _talking(self, now: datetime) -> bool:
        """The user wrote to the assistant in the last half hour: it already knows how things are going."""
        raw = self.scheduler.state_db.get_sync_meta("last_user_message_at")
        try:
            return bool(raw) and now - datetime.fromisoformat(raw) < QUIET_AFTER_TALK
        except ValueError:
            return False

    def _talked_since(self, since: datetime) -> bool:
        raw = self.scheduler.state_db.get_sync_meta("last_user_message_at")
        try:
            return bool(raw) and datetime.fromisoformat(raw) >= since
        except ValueError:
            return False

    def _unreported(self, span) -> bool:
        return self._plannable(span) and span.status == SpanStatus.PLANNED and not span.tentative

    def heads_up(self, now: datetime) -> None:
        """Light and up: fifteen minutes before a calendar event, a heads-up, naming anything planned before it
        that isn't reported yet."""
        if self.level() == "off":
            return
        plan = self.manager().day_plan
        told = self._remembered("heads_up")
        soon = [s for s in plan.active_spans() if s.category == SpanCategory.CALENDAR and not s.tentative
                and s.id not in told and now < s.start_dt <= now + timedelta(minutes=15)]
        for event in soon:
            told[event.id] = now.date().isoformat()
            behind = [s.title for s in plan.active_spans() if self._unreported(s)
                      and event.start_dt - timedelta(hours=2) <= s.start_dt and s.end_dt <= event.start_dt]
            on_thirty = event.start_dt == thirty_of(event.start_dt)
            text = (f"⏰ {event.title} at {format_clock(event.start_dt)}." if on_thirty else  # its real time, and block
                    f"⏰ {event.title} at {format_clock(event.start_dt)}, in the "
                    f"{format_clock(thirty_of(event.start_dt))} block.")
            if behind:
                text += " (" + (", ".join(behind[:-1]) + " and " + behind[-1] if len(behind) > 1 else behind[0]) + \
                        (" aren't" if len(behind) > 1 else " isn't") + " reported yet.)"
            self.send(text, "heads_up", now=now)
        if soon:
            self._remember("heads_up", told, now)

    def stretches(self, now: datetime) -> None:
        """Medium: at the start of each stretch of the day, what's planned in it; once it's over (plus the grace
        period), one question about whatever in it isn't reported."""
        if self.level() != "medium" or self._quiet(now):
            return
        plan = self.manager().day_plan
        quiet_spans = [s for s in plan.active_spans() if not s.tentative
                       and (s.category == SpanCategory.SLEEP or (s.category == SpanCategory.CALENDAR and s.is_firm))]
        started, asked = self._remembered("stretch_started"), self._remembered("stretch_asked")
        grace = timedelta(minutes=self.config.assistant.checkin_after_minutes)
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        for a, b in day_stretches(plan):
            key = a.isoformat()
            items = sorted((s for s in plan.active_spans() if self._unreported(s) and a <= s.start_dt < b),
                           key=lambda s: s.start_dt)
            if a <= now < a + timedelta(minutes=5) and key not in started and items and not self._talking(now):
                started[key] = now.date().isoformat()
                self._remember("stretch_started", started, now)
                self.send(f"🗓 Until {format_clock(b)}: " + ", then ".join(s.title for s in items) + ".", "start",
                          now=now)
            open_items = [s for s in items if not mutes.is_muted(s.id, now)]
            ask_at = b + grace
            if any(q.start_dt <= ask_at < q.end_dt for q in quiet_spans) and b - timedelta(minutes=15) > a:
                ask_at = b - timedelta(minutes=15)  # it ends at bedtime (or a long event): ask just before
            if ask_at <= now < ask_at + timedelta(hours=2) and key not in asked and open_items and self._talked_since(a):
                asked[key] = now.date().isoformat()  # they've been talking to the assistant: it knows; don't ask
                self._remember("stretch_asked", asked, now)
                continue
            if (ask_at <= now < ask_at + timedelta(hours=2) and key not in asked and open_items
                    and not self._talking(now)):
                asked[key] = now.date().isoformat()
                self._remember("stretch_asked", asked, now)
                titles = [s.title for s in open_items]
                listed = titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + f" and {titles[-1]}"
                self.send(f"👋 Since {format_clock(a)}: did you get to {listed}?", "checkin", now=now)

    def wrap_up(self, now: datetime) -> None:
        """Medium: once a day, 45 minutes before bed, one question about whatever from today still isn't reported,
        so the record stays good without asking all day."""
        if self.level() != "medium" or self._quiet(now) or self._talking(now):
            return
        plan = self.manager().day_plan
        bed = min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start),
                  default=None)
        key = f"wrap_up:{plan.target_date.isoformat()}"
        if bed is None or not (bed - timedelta(minutes=45) <= now < bed) or self.scheduler.state_db.get_sync_meta(key):
            return
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        open_items = [s.title for s in sorted(plan.active_spans(), key=lambda s: s.start_dt)
                      if self._unreported(s) and s.end_dt <= now and not mutes.is_muted(s.id, now)]
        self.scheduler.state_db.set_sync_meta(key, "1")
        if open_items:
            listed = open_items[0] if len(open_items) == 1 else ", ".join(open_items[:-1]) + f" and {open_items[-1]}"
            self.send(f"🌙 Before bed: did {listed} happen? Just say which (or none).", "checkin", now=now)

    def check_ins(self, now: datetime) -> None:
        """Heavy: once a thirty is over (plus the grace period), ask in one message about the items in it that
        have no outcome: never twice about an item, never about one already reported or muted."""
        cfg = self.config.assistant
        if self.level() != "heavy" or self._quiet(now) or self._talking(now):
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
        """Heavy: at the start of a thirty, one message for what's planned to start in it; each item is
        announced at most once (moving it doesn't announce it again), and times are told in thirties."""
        if self.level() != "heavy" or self._quiet(now):
            return
        plan = self.manager().day_plan
        block = thirty_of(now)
        if now - block > timedelta(minutes=5):
            return  # only right at the start of a thirty
        announced = self._remembered("starts_announced")
        by_id = {s.id: s for s in plan.spans}

        def told_already(s) -> bool:
            """Announced before, as this item or as an earlier copy of it: re-planning makes a new item (a new
            id), and "Wedding thank-yous" was announced four times on 10-21. A second one after the first is
            done (teeth at night) is new."""
            if s.id in announced:
                return True
            earlier = [by_id.get(i) for i in ids_titled(s)]
            return any(e is None or e.status not in (SpanStatus.DONE, SpanStatus.SKIPPED) for e in earlier)

        def ids_titled(s) -> List[str]:  # stored as "date|id id ...", so old days expire like the rest
            value = announced.get("title:" + s.title.strip().lower(), "")
            return value.split("|", 1)[1].split() if value.startswith(now.date().isoformat()) else []

        starting = [s for s in plan.active_spans()
                    if thirty_of(s.start_dt) == block and not told_already(s) and not s.tentative
                    and s.status == SpanStatus.PLANNED
                    and s.category in (SpanCategory.ACTIVITY, SpanCategory.TASK, SpanCategory.CALENDAR, SpanCategory.SLEEP)]
        if not starting:
            return
        for s in starting:
            announced[s.id] = now.date().isoformat()
            announced["title:" + s.title.strip().lower()] = f"{now.date().isoformat()}|" + " ".join(ids_titled(s) + [s.id])
        self._remember("starts_announced", announced, now)
        sleep = [s for s in starting if s.category == SpanCategory.SLEEP]
        rest = sorted((s for s in starting if s.category != SpanCategory.SLEEP), key=lambda s: s.start_dt)
        if rest:
            self.send(f"🔔 {format_clock(block)}: " + ", ".join(s.title for s in rest), "start", now=now)
        if sleep:
            self.send(f"🌙 Bedtime ({format_clock(block)}). Wind down and rest well!", "start", now=now)

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
        if manager.tools.assistant is not None:
            started = manager.tools.assistant.start_the_day()
            if started:
                logger.info("Morning: %s.", started)
        text = manager.execute_tool("rollover_proposal", {})
        if text.startswith("Nothing from yesterday") or text.startswith("NOT_DONE"):
            return
        self.send("☀️ Good morning! " + text, "rollover", now=now)

    def today_sync(self, now: datetime) -> None:
        """Every two minutes: catch up with hand edits to the Today note (AssistantTools.sync_today)."""
        if time.time() - self._last_today < self.TODAY_EVERY:
            return
        self._last_today = time.time()
        assistant = self.manager().tools.assistant
        if assistant is not None and assistant.morning_pending() and now.hour < 12:
            assistant.start_the_day()  # Joplin was closed at wake-up; it may be open now
        for message in assistant.sync_today() if assistant is not None else []:
            self.send("📝 " + message, "today", now=now)

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
                counts = sort_lists(ListCatalog(self.scheduler.state_db), notes, tasks, sorter, self.today(),
                                    max_notes=10)  # a few notes each half hour, not the whole backlog at once
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
        for duty in (self.rollover, self.reminders, self.heads_up, self.stretches, self.wrap_up, self.check_ins,
                     self.starts, self.today_sync):
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
