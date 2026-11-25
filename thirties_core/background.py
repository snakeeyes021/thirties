"""What Thirties does while no window is open: messages, Telegram, Joplin delivery, sync.

The desktop app runs this in-process and keeps running in the background (with
the user's permission, through the Background portal), so it works with the
window closed. Every few seconds it:

- at heavy, asks about each planned item with no outcome as the thirty it ends in is over (once each, unless
  muted, and counted as asked only once the question went out),
- delivers reminders the user asked for,
- rings a bell at every thirty where a planned item starts, naming what starts in it (each item once, and only
  once its bell went out; not at all with telegram.notify_block_transitions off: fewer_messages.py),
- ends a start notice or a check-in with what's next, when that says something new (whats_next.SentSummaries),
- asks about each item once, whichever question asks, never lists what's not marked yet under a notice, never
  sends the same words twice in a day, and after two check-ins with no reply sends no more until the user
  writes again (away.py),
- tells the phone what was ticked in the app's day panel, with what's next,
- asks once an evening, in one question, about what's still not marked (catch_up.py),
- holds every message while the user asked for quiet, until they say it's over (holds.py),
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
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.backlog import load_backlog
from thirties_core.clock import format_clock, thirty_ceil, thirty_of
from thirties_core.conversation import ConversationManager
from thirties_core.models import SpanCategory, SpanStatus

logger = logging.getLogger(__name__)

DesktopNotify = Callable[[str, str, str], None]  # (title, body, kind) -> None


LEVELS = ("off", "light", "medium", "heavy")
def phone_text(text: str, emphasis: Sequence[str] = ()) -> str:
    """What the phone gets: the same words, with ``emphasis`` in bold (the first time each appears)."""
    for word in emphasis:
        if word and word in text:
            text = text.replace(word, f"*{word}*", 1)
    return text


from thirties_core.holds import PASSES as HOLD_PASSES  # noqa: E402 (what a hold lets through)
YES_KINDS = ("rollover",)  # messages that ask for a yes: a 👍 reaction on Telegram answers them
STRETCH_MAX = timedelta(hours=3)
QUIET_AFTER_TALK = timedelta(minutes=30)
BELL_WAITING = "bell_waiting"  # the thirty whose start notice waits for the assistant's answer to finish
CHECKIN_WINDOW = timedelta(minutes=10)  # a check-in goes out within this of its time, or not at all (11-06)
CHECKIN_WAITING = "checkin_waiting"  # when the check-in that waits for the assistant's answer was due
REMINDER_STALE = timedelta(hours=2)  # a reminder Telegram couldn't take within this is dropped, not sent late
PANEL_QUIET = 90  # seconds with no more ticks in the app's day panel before the phone is told (one message a burst)


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


def back_to_back(spans) -> dict:
    """Each item's run of back-to-back items (one starting by the time the one before ends), as {id: the run's
    end}. The evening catch-up waits until an item's run is over."""
    out: dict = {}
    run: list = []
    end = None
    for s in sorted(spans, key=lambda s: (s.start_dt, s.end_dt)):
        if run and s.start_dt > end:
            out.update({x.id: end for x in run})
            run = []
        run.append(s)
        end = s.end_dt if len(run) == 1 else max(end, s.end_dt)
    out.update({x.id: end for x in run})
    return out


class BackgroundService:
    TICK_SECONDS = 15
    OUTBOX_EVERY = 60
    SYNC_EVERY = 300
    LISTS_EVERY = 1800
    TODAY_EVERY = 120
    MOOT_EVERY = 60
    PRESENCE_EVERY = 600  # how often the Joplin app's being open is sampled (for offering the sync peer)

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
        if hasattr(self.joplin_engine, "use_ids"):
            self.joplin_engine.use_ids(self.scheduler.state_db)  # item ids that follow edits and moves
        if inference_engine is None:  # the headless service builds its own: with the fallback, as the app's has
            from thirties_core.fallback import with_fallback
            inference_engine = with_fallback(get_inference_engine(config), config, self.scheduler.state_db,
                                             self.scheduler.tz)
        self.inference_engine = inference_engine
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
        self._last_moot = 0.0
        self._last_presence = 0.0
        self.peer = None  # the headless Joplin sync peer's duty, when it's on (joplin_peer.py)
        self._sorting = False
        self.sort_lists_enabled = not os.environ.get("THIRTIES_OFFLINE")  # off in tests and evals
        self._threads: List[threading.Thread] = []
        self.running = False
        self._answering = False
        self._fixed_now = False      # tests pass the time to each duty
        self._waiting: Dict[str, tuple] = {}  # notices queued for the phone: (text, kind, day) by words
        self._marked: Dict[str, Optional[date]] = {}  # stored with the mark, still queued: their day
        self.run_in_threads = True   # tests run the background work inline

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
                if not getattr(self, "_titles_repaired", False):  # once per run: records with a raw id as a title
                    self._titles_repaired = True                     # get their real title back (11-06)
                    try:
                        from thirties_core.titles import repair_raw_titles
                        fixed = repair_raw_titles(self.scheduler.state_db, tasks)
                        if fixed:
                            logger.info("Gave %d item(s) their title back (they showed an id).", fixed)
                            plan, pending = self.scheduler.build_day_plan(today)
                    except Exception as e:
                        logger.warning("Repairing titles failed: %s", e)
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

    def send(self, text: str, kind: str, title: str = "Thirties", emphasis: Sequence[str] = (),
             to_conversation: bool = True, now: Optional[datetime] = None, expires: Optional[datetime] = None,
             day: Optional[date] = None, phone: bool = True, desktop: bool = True,
             phone_chats: Optional[Sequence[Any]] = None, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        """The one way out for what the assistant says by itself: the desktop, the phone (if one is paired here)
        and the conversation of the ``day`` it's about (today's by default; the next day's morning goes in the next
        day's), so the app and Telegram are one conversation. The phone gets the same words, with ``emphasis``
        (words shown in bold there) as its only difference.

        A notice about a moment ``expires``: if the phone can't take it before then it's dropped rather than sent
        stale, and it's kept in the conversation only once it really got there. Anything else that doesn't
        reach the phone is kept with a quiet "not delivered to your phone" mark. ``phone=False``: the phone already
        has it (a mirrored turn). ``desktop=False``: no desktop notification (what the user just did at the
        desktop). ``phone_chats``: only these chats get it (with ``reply_markup``, which only the one-time removal
        of an old keyboard uses)."""
        from thirties_core.clock import real_times, to_thirties
        if kind not in HOLD_PASSES and self._held(now or self.now(), kind):
            logger.info("Held (the user asked for quiet): %r", text[:80])
            return
        # --- the same words never twice in a day (fewer_messages.py) ---
        from thirties_core.fewer_messages import UNASKED_KINDS, Repeats
        said, said_on = text, day or (now or self.now()).date()
        if kind in UNASKED_KINDS and Repeats(self.scheduler.state_db).seen(said, said_on):
            logger.info("Not sent (said already today): %r", text[:80])
            return
        # --- end the same words never twice ---
        # --- quiet while the user is away (away.py): two check-ins with no reply, then none until they write ---
        if kind == "checkin":
            from thirties_core.away import UnansweredCheckins
            unanswered = UnansweredCheckins(self.scheduler.state_db)
            if unanswered.paused():
                logger.info("Not sent (the last check-ins went unanswered): %r", text[:80])
                return
            # counted once it reaches the user (below, or delivered_late): one dropped unseen was never asked
        # --- end quiet while the user is away ---
        if kind in ("start", "checkin"):  # a gap now: the notice may name one thing kept for if there's time
            try:
                from thirties_core.if_time import gap_line
                assistant = self.manager().tools.assistant
                line = gap_line(assistant, now or self.now()) if assistant is not None else ""
                if line:
                    from thirties_core.messages import join_sections
                    text = join_sections(text, line) if "\n\n" in text else text + "\n" + line
            except Exception as e:
                logger.warning("Couldn't add the If there's time line: %s", e)
        try:  # what the user reads: no done rows, names not ids, plain lines (a turn's reply is cleaned already)
            from thirties_core.messages import clean_message
            text = clean_message(text, self.manager().tools)
        except Exception as e:
            logger.warning("Couldn't tidy a message: %s", e)
        try:  # a message a turn already stored goes out as it was stored: the phone reads what the chat shows
            now = now or self.now()
            if to_conversation:
                spans = self.manager().day_plan.spans  # "it's 8:41" stays 8:41, and a fixed item's own time
                fixed = self.manager().tools._is_fixed  # (the bell says it: "Vet at 4:45")
                keep = real_times(*(format_clock(s.start_dt) for s in spans if not s.is_envelope and fixed(s)),
                                  spans=spans)
                from thirties_core.under_way import unsaid  # when an item under way began: by its block
                keep, now_kept = unsaid(keep, real_times(now=now), spans)
                text = to_thirties(self.manager().tools.shorten(text), keep,  # short titles, AM/PM kept
                                   now=now_kept)  # "it's 8:41", never as a row's start
        except Exception:
            pass
        if self.desktop_notify is not None and desktop:
            try:
                self.desktop_notify(title, text, kind)
            except Exception as e:
                logger.warning("Desktop notification failed: %s", e)
        delivered, waiting = True, False
        if self.bridge is not None and phone:
            rendered = phone_text(text, emphasis)
            client = self.bridge.client
            before = len(client.sent_ids)
            extra = {k: v for k, v in (("chats", phone_chats), ("reply_markup", reply_markup)) if v}
            delivered = self.bridge.broadcast(rendered, expires=expires, **extra) if expires is not None \
                else self.bridge.broadcast(rendered, **extra)
            delivered = delivered is not False  # an older or test bridge that says nothing: as sent
            if kind in YES_KINDS:  # a 👍 on this message is a yes (telegram_bridge.handle_updates)
                from thirties_core.telegram_bridge import YES_TARGETS_KEY
                ids = [list(x) for x in client.sent_ids[before:]]
                self.scheduler.state_db.set_sync_meta(YES_TARGETS_KEY, json.dumps(ids))
            if not delivered and expires is not None and to_conversation:  # kept for the conversation once it
                from thirties_core.telegram_bridge import plain_lists        # really reaches the phone
                with self._lock:
                    self._waiting[plain_lists(rendered)] = (text, kind, day)
                waiting = True
        if kind == "checkin" and delivered:  # it reached the user (or there's no phone to wait for): asked
            self._count_checkin()
        if kind in UNASKED_KINDS and delivered:  # said: never these words again today (one that didn't get
            Repeats(self.scheduler.state_db).mark(said, said_on)  # through may go out again)
        if to_conversation and not waiting:  # after delivery: this waits for a turn under way (Telegram) to finish
            self._keep(text, kind, day, undelivered=not delivered)
            if not delivered:  # its mark comes off if it gets there late
                from thirties_core.telegram_bridge import plain_lists
                with self._lock:
                    self._marked[plain_lists(rendered)] = day

    def _held(self, now: datetime, kind: str = "") -> bool:
        """Whether a hold keeps this kind of message back now (any kind, when none is given)."""
        from thirties_core import holds
        try:
            data = holds.active(self.scheduler.state_db, now)
            return data is not None and (not kind or not holds.lets_through(data, kind))
        except Exception as e:
            logger.warning("Couldn't read the hold on messages: %s", e)
            return False

    def hold_check(self, now: datetime) -> None:
        """A hold on messages ends once what it waits for has happened (holds.py): the item it waits for has an
        outcome, the user said the words, or its time came. Then one line says messages are back on, with what's
        next when that says something new; reminders held meanwhile go out on their own."""
        from thirties_core import holds
        if not holds._load(self.scheduler.state_db).get("state") == "on":
            return
        manager = self.manager()
        with manager.turn_lock:
            manager._catch_up()  # what was said in the app or on the phone since
        why = holds.check(self.scheduler.state_db, manager.day_plan, manager.messages, now)
        if why in ("done", "said") or (why == "time" and holds._load(self.scheduler.state_db).get("clock")):
            self.send(self._with_next("Messages are back on.", now), "hold", now=now)

    def _keep(self, text: str, kind: str, day: Optional[date], undelivered: bool = False) -> None:
        """Stored under its own day."""
        try:
            manager = self.manager()
            if day is not None and day != manager.day_plan.target_date:  # the coming day's (its morning)
                from thirties_core import build
                from thirties_core.memory import ConversationStore
                message = {"role": "assistant", "content": text, "at": self.now().isoformat(timespec="seconds"),
                           "build": build.BUILD, "via": "background"}
                if kind == "start":
                    message["kind"] = "notice"
                if undelivered:
                    message["undelivered"] = True
                ConversationStore(self.scheduler.state_db).append(day, message)
                return
            with manager.turn_lock:
                manager.refresh()
                manager.add_assistant_message(text, kind="notice" if kind == "start" else "", undelivered=undelivered)
        except Exception as e:
            logger.warning("Couldn't add the message to the conversation: %s", e)

    def delivered_late(self, chunk: str) -> bool:
        """Something of ours the phone took late (``chunk``: its words, or part of them). A notice that waited is
        kept in the conversation now; anything else loses its "not delivered" mark. True when it was ours."""
        with self._lock:
            waited = next((k for k in self._waiting if chunk in k), None)
            found = self._waiting.pop(waited) if waited is not None else None
            marked = next((k for k in self._marked if chunk in k), None)
            day = self._marked.pop(marked) if marked is not None else None
        if found is not None:
            if found[1] == "checkin":  # it reached the user after all: asked (away.py)
                self._count_checkin()
            self._keep(*found)
            return True
        if marked is None:
            return False
        try:
            manager = self.manager()
            if day is not None and day != manager.day_plan.target_date:
                from thirties_core.memory import ConversationStore
                ConversationStore(self.scheduler.state_db).mark_delivery(day, chunk, undelivered=False)
            else:
                manager.mark_delivery(chunk, undelivered=False)
        except Exception as e:
            logger.warning("Couldn't take the not-delivered mark off: %s", e)
        return True

    def dropped_stale(self, chunk: str) -> bool:
        """A notice that never got through in time: dropped from the phone, so never in the conversation."""
        with self._lock:
            waited = next((k for k in self._waiting if chunk in k), None)
            if waited is not None:
                self._waiting.pop(waited)
            marked = next((k for k in self._marked if chunk in k), None)
            if marked is not None:
                self._marked.pop(marked)  # it keeps its mark: it never got there
        return waited is not None or marked is not None

    # --- duties ---

    def _remembered(self, key: str) -> Dict[str, str]:
        raw = self.scheduler.state_db.get_sync_meta(key)
        return json.loads(raw) if raw else {}

    def _remember(self, key: str, done: Dict[str, str], now: datetime) -> None:
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        self.scheduler.state_db.set_sync_meta(key, json.dumps({k: v for k, v in done.items() if v >= cutoff}))

    def _with_missed(self, status: str, now: datetime) -> str:
        """A notice with what's not marked yet right under it (messages.py): one per line, never twice, never the
        block the user is still in. Added only when something new fell behind since the user was last shown the
        list (in a notice or a reply): being behind is common, silence about it isn't help, and the same list every
        half hour would be nagging (C2)."""
        from thirties_core.messages import MissedTracker, missed_for, with_missed
        from thirties_core.reply_extras import effective
        if not effective(self.config, self.scheduler.state_db, self.manager().day_plan.target_date)["not_marked"]:
            return status  # the person asked for the Not marked yet list to stop (reply_extras.py)
        from thirties_core.away import LastSentList, UnansweredCheckins
        if UnansweredCheckins(self.scheduler.state_db).paused():
            return status  # nobody is answering: the notice alone (away.py)
        missed = missed_for(self.manager().tools, now)
        tracker = MissedTracker(self.scheduler.state_db)
        if not missed or not tracker.anything_new(now, missed) or LastSentList(self.scheduler.state_db).same(now, missed):
            return status
        tracker.mark(now, missed)
        LastSentList(self.scheduler.state_db).mark(now, missed)
        from thirties_core.item_path import labelled, tasks_of  # a nested item with its parents
        return with_missed(status, labelled(missed, tasks_of(self.manager().tools)))

    def _with_next(self, status: str, now: datetime, with_now: bool = True, asked: Sequence[str] = ()) -> str:
        """A notice that ends with what's next (whats_next.py), when that says something new since the user was
        last shown it (whats_next.SentSummaries: nothing shown yet today, the plan changed, the last one ran out,
        or two hours passed): a bell or a check-in then says where the day stands, so coming back to the phone
        needs no "what's on now?". Otherwise the notice alone."""
        from thirties_core import whats_next
        from thirties_core.messages import join_sections
        if self._held(now):
            return status  # it won't go out: nothing counts as shown
        from thirties_core.away import UnansweredCheckins
        if UnansweredCheckins(self.scheduler.state_db).paused():
            return status  # nobody is answering: the notice alone, short (away.py)
        manager = self.manager()
        plan = manager.day_plan
        sent = whats_next.SentSummaries(self.scheduler.state_db)
        if not (plan.day_start <= now < plan.day_end) or not sent.due(plan, now):
            return status
        # No Not marked yet list under a notice: each item is asked about once, in its own check-in, and a list
        # that grows block after block is nagging (fewer_messages.py). A reply the user asks for still shows it.
        part = whats_next.summary(manager.tools, now, unmarked=[], with_now=with_now)
        if not part:
            return status
        sent.mark(plan, now)
        return join_sections(status, part)

    def _count_checkin(self) -> None:
        """A check-in reached the user: one more with no reply yet (away.py)."""
        from thirties_core.away import UnansweredCheckins
        try:
            UnansweredCheckins(self.scheduler.state_db).sent()
        except Exception as e:
            logger.warning("Couldn't count the check-in: %s", e)

    def _checkins_paused(self) -> bool:
        """The last check-ins went unanswered: none until the user writes again (away.py)."""
        from thirties_core.away import UnansweredCheckins
        return UnansweredCheckins(self.scheduler.state_db).paused()

    def _name(self, span) -> str:
        """How a notice names a plan entry: a nested list item with its parents (item_path.py), else its title."""
        try:
            from thirties_core.item_path import span_label, tasks_of
            return span_label(span, tasks_of(self.manager().tools))
        except Exception:
            return span.title

    def _asked(self, now: datetime, titles: List[str]) -> None:
        """A check-in asked about these: a reply needn't ask again (C5)."""
        from thirties_core.messages import MissedTracker
        MissedTracker(self.scheduler.state_db).mark(now, titles=titles)

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
        return any(s.covers(now) for s in plan.active_spans() if not s.tentative  # covers: the instant (DST)
                   and (s.category == SpanCategory.SLEEP or (s.category == SpanCategory.CALENDAR and s.is_firm)))

    def _talking(self, now: datetime) -> bool:
        """The user wrote to the assistant in the last half hour: it already knows how things are going."""
        raw = self.scheduler.state_db.get_sync_meta("last_user_message_at")
        try:
            return bool(raw) and now - datetime.fromisoformat(raw) < QUIET_AFTER_TALK
        except ValueError:
            return False

    def _up(self, now: datetime) -> bool:
        """The user has started the day (wake.user_is_up): until then nothing is set up or sent, apart from the
        first message of the day (morning_hello), reminders they asked for, and sunrise/sunset if switched on."""
        from thirties_core.wake import user_is_up
        return user_is_up(self.scheduler.state_db, self.manager().day_plan, now)

    def _turn_running(self) -> bool:
        lock = self.manager().turn_lock
        if lock.acquire(blocking=False):
            lock.release()
            return False
        return True

    def _unreported(self, span) -> bool:
        return self._plannable(span) and span.status == SpanStatus.PLANNED and not span.tentative

    def heads_up(self, now: datetime) -> None:
        """Light and up: fifteen minutes before a calendar event, a heads-up, naming anything planned before it
        that isn't reported yet. During a quiet the user asked for it waits, and counts as told only once it went
        out (it may still go once the quiet ends, before the event starts)."""
        if self.level() == "off" or self._held(now, "heads_up"):
            return
        plan = self.manager().day_plan
        told = self._remembered("heads_up")
        soon = [s for s in plan.active_spans() if s.category == SpanCategory.CALENDAR and not s.tentative
                and s.id not in told and now < s.start_dt <= now + timedelta(minutes=15)]
        for event in soon:
            told[event.id] = now.date().isoformat()
            behind = [self._name(s) for s in plan.active_spans() if self._unreported(s)
                      and event.start_dt - timedelta(hours=2) <= s.start_dt and s.end_dt <= event.start_dt]
            on_thirty = event.start_dt == thirty_of(event.start_dt)
            text = (f"⏰ {event.title} at {format_clock(event.start_dt)}." if on_thirty else  # its real time, and block
                    f"⏰ {event.title} at {format_clock(event.start_dt)}, in the "
                    f"{format_clock(thirty_of(event.start_dt))} block.")
            if behind:
                text += " (" + (", ".join(behind[:-1]) + " and " + behind[-1] if len(behind) > 1 else behind[0]) + \
                        (" aren't" if len(behind) > 1 else " isn't") + " reported yet.)"
            self.send(text, "heads_up", now=now, expires=event.start_dt)
        if soon:
            self._remember("heads_up", told, now)

    def model_fit(self, now: datetime) -> None:
        """Once per start: record a death while the on-device model was working (its marker left behind), and, if
        the bigger model doesn't fit this device, offer the smaller one (at most once a week; never switched by
        itself). Only for someone using the on-device model."""
        if getattr(self, "_fit_checked", False):
            return
        self._fit_checked = True
        from thirties_core.brains.slm import fit
        record = fit.FitRecord(self.scheduler.state_db)
        record.check_startup()
        from thirties_core.brains import gemma_offered
        if (self.config.assistant.brain or "") != "slm" or not gemma_offered(self.config):
            return
        if record.should_offer(current_model=self.config.assistant.slm_model or ""):
            record.offered()
            self.send(fit.OFFER_TEXT + " " + fit.OFFER_WHERE, "notice", now=now)

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
        from thirties_core.fewer_messages import AskedOnce, block_notices_on, is_routine_step
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        once = AskedOnce(self.scheduler.state_db)
        for a, b in day_stretches(plan):
            key = a.isoformat()
            items = sorted((s for s in plan.active_spans() if self._unreported(s) and a <= s.start_dt < b),
                           key=lambda s: s.start_dt)
            if self._bell_open(a, now) and key not in started and items:
                # only when start notices are on, and not for a stretch of routine steps alone (fewer_messages.py)
                say = block_notices_on(self.config) and not all(is_routine_step(s) for s in items)
                if say and self._turn_running():  # the assistant is answering: it waits for the answer
                    self.scheduler.state_db.set_sync_meta(BELL_WAITING, a.isoformat())
                elif not (say and self._held(now, "start")):  # a quiet the user asked for: not told yet
                    if say:
                        self.send(f"🗓 Until {format_clock(b)}: " + ", then ".join(self._name(s) for s in items) + ".",
                                  "start", now=now, expires=a + timedelta(minutes=30))
                    started[key] = now.date().isoformat()  # told (or nothing to tell): once a stretch
                    self._remember("stretch_started", started, now)
            open_items = [s for s in items if not mutes.is_muted(s.id, now) and not once.asked(s.id)]
            ask_at = b + grace
            if any(q.start_dt <= ask_at < q.end_dt for q in quiet_spans) and b - timedelta(minutes=15) > a:
                ask_at = b - timedelta(minutes=15)  # it ends at bedtime (or a long event): ask just before
            # what the user reported meanwhile has its outcome and isn't open; while the assistant is answering, or
            # during a quiet the user asked for, the question waits (it isn't counted as asked until it goes out)
            if (ask_at <= now < ask_at + timedelta(hours=2) and key not in asked and open_items
                    and not self._checkins_paused() and not self._turn_running() and not self._held(now, "checkin")):
                asked[key] = now.date().isoformat()
                self._remember("stretch_asked", asked, now)
                once.mark((s.id for s in open_items), now)  # asked: no other duty asks about these again today
                titles = [s.title for s in open_items]
                names = [self._name(s) for s in open_items]
                listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
                self._asked(now, titles)
                self.send(self._with_next(f"👋 Since {format_clock(a)}: did you get to {listed}?", now,
                                          asked=titles), "checkin", now=now, expires=now + timedelta(minutes=30))

    def wrap_up(self, now: datetime) -> None:
        """Medium: once a day, 45 minutes before bed, one question about whatever from today still isn't reported,
        so the record stays good without asking all day."""
        if self.level() != "medium" or self._quiet(now) or self._checkins_paused() or self._turn_running() \
                or self._held(now, "checkin"):  # waits for an answer under way, or a quiet the user asked for
            return
        plan = self.manager().day_plan
        bed = min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start),
                  default=None)
        key = f"wrap_up:{plan.target_date.isoformat()}"
        if bed is None or not (bed - timedelta(minutes=45) <= now < bed) or self.scheduler.state_db.get_sync_meta(key):
            return
        from thirties_core.fewer_messages import AskedOnce
        from thirties_core.memory import CheckinMutes
        mutes = CheckinMutes(self.scheduler.state_db)
        once = AskedOnce(self.scheduler.state_db)
        # everything left before bed, asked about once (the last stretch's own question would come minutes later)
        spans = [s for s in sorted(plan.active_spans(), key=lambda s: s.start_dt)
                 if self._unreported(s) and s.end_dt <= bed and not mutes.is_muted(s.id, now) and not once.asked(s.id)]
        open_items = [s.title for s in spans]
        names = [self._name(s) for s in spans]
        self.scheduler.state_db.set_sync_meta(key, "1")
        if open_items:
            once.mark((s.id for s in spans), now)
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
            self._asked(now, open_items)
            self.send(f"🌙 Before bed: did {listed} happen? Just say which (or none).", "checkin", now=now,
                      expires=bed)

    def _built_late(self, span, plan) -> bool:
        """The day was built (the user's first message) when this item's thirty was already half over: no
        check-in about it (11-06: built at 9:25, the 9:00 block's check-in came at 9:57)."""
        raw = self.scheduler.state_db.get_sync_meta(f"day_built_at:{plan.target_date.isoformat()}")
        try:
            built = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return False
        return built is not None and thirty_of(span.start_dt) + timedelta(minutes=15) <= built

    def check_ins(self, now: datetime) -> None:
        """Heavy: once the thirty an item ends in is over (plus the grace period), ask about it, in one message
        with any other item that ended in that thirty: never twice about an item, never about one already reported
        or muted. Each goes out at its own time (CHECKIN_WINDOW) or not at all, and a late day build never pushes
        check-ins later or sends one for a block that was half over by then. While the assistant is answering, the
        check-in waits and goes out once the answer is done, within the half hour after its time. One held back
        (asleep, a firm event, a quiet the user asked for) is never counted as asked: the evening catch-up
        (catch_up.py) carries what was really missed."""
        cfg = self.config.assistant
        if self.level() != "heavy" or self._quiet(now) or self._checkins_paused():
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
        db = self.scheduler.state_db
        waited = db.get_sync_meta(CHECKIN_WAITING) or ""

        def on_time(span):  # at its own time, or, once it waited for an answer to finish, within the half hour
            due_at = thirty_end(span) + grace
            return now < due_at + CHECKIN_WINDOW or (waited == due_at.isoformat() and now < due_at + timedelta(minutes=30))
        due = [s for s in plan.active_spans()
               if self._plannable(s) and s.status == SpanStatus.PLANNED and s.id not in asked
               and thirty_end(s) + grace <= now and on_time(s) and not mutes.is_muted(s.id, now)
               and not self._built_late(s, plan)]
        if not due or self._held(now, "checkin"):  # a quiet the user asked for: not asked
            return
        if self._turn_running():  # the assistant is answering: the check-in waits for it
            db.set_sync_meta(CHECKIN_WAITING, (min(thirty_end(s) for s in due) + grace).isoformat())
            return
        for s in due:
            asked[s.id] = now.date().isoformat()
        self._remember("checkins_asked", asked, now)
        titles = [s.title for s in sorted(due, key=lambda s: s.start_dt)]
        names = [self._name(s) for s in sorted(due, key=lambda s: s.start_dt)]  # what the user reads
        current = self._on_now(plan, now, {s.id for s in due})
        if len(titles) == 1 and current:  # asked the way the user wants it: the next one started, or still on this one?
            question = f"👋 Just checking in: have you started {current} yet, or are you still on {names[0]}?"
            bold = [current, names[0]]
        elif len(titles) == 1:
            question = f"👋 Just checking in: did you get to {names[0]}, or are you still on it?"
            bold = [names[0]]  # in bold on the phone: the same words
        else:
            listed = ", ".join(names[:-1]) + f" and {names[-1]}"
            question = f"👋 How did it go? Did you get to {listed}?" + (
                f" And have you started {current} yet?" if current else "")
            bold = []
        self._asked(now, titles)
        self.send(self._with_next(question, now, with_now=not current, asked=titles), "checkin", emphasis=bold, now=now,
                  expires=now + timedelta(minutes=30))
        if waited:
            db.set_sync_meta(CHECKIN_WAITING, "")

    def _on_now(self, plan, now: datetime, skip: set) -> str:
        """What's planned to be under way now (started in this thirty or before, not over): the next thing a
        check-in can ask about ("have you started X yet?"). One item, by its title; "" when none or several."""
        block = thirty_of(now)
        on = [s for s in plan.active_spans() if self._unreported(s) and s.id not in skip and not s.is_envelope
              and s.start_dt < block + timedelta(minutes=30) and s.start_dt <= now < s.end_dt]
        titles = list(dict.fromkeys(self._name(s) for s in sorted(on, key=lambda s: s.start_dt)))
        return titles[0] if len(titles) == 1 else ""

    def reminders(self, now: datetime) -> None:
        from thirties_core.memory import ReminderStore
        if self._held(now, "reminder"):
            return  # kept: they go out once the hold ends
        store = ReminderStore(self.scheduler.state_db)
        announced = self._remembered("starts_announced")
        for rem in store.due(now):
            span = None
            if rem.span_id:
                span = next((s for s in self.manager().day_plan.spans if s.id == rem.span_id), None)
                if span is not None and span.status == SpanStatus.PLANNED and span.id not in announced \
                        and self._bell_to_come(span, now):
                    continue  # at heavy its bell is its ping: left due until the bell has gone out
            store.set_state(rem.id, "sent")  # first, so a crash can't repeat it forever
            if rem.span_id:  # about a planned item: not if it's done, gone, or its bell already went out
                if span is None or span.status != SpanStatus.PLANNED or span.id in announced:
                    continue
            if now - rem.due > timedelta(hours=12):
                text = f"⏰ Reminder (from {rem.due.astimezone(now.tzinfo).strftime('%a')} " \
                       f"{format_clock(rem.due.astimezone(now.tzinfo))}): {rem.text}"
            else:
                text = f"⏰ Reminder: {rem.text}"
            if span is not None and rem.text.strip().lower() == span.title.strip().lower():
                text = text[:len(text) - len(rem.text)] + self._name(span)  # a nested item with its parents
            details = self._details(span.title if span is not None else rem.text,
                                    span.task_id if span is not None else "")
            if details:  # what the reminder was given to say (its details)
                text += "\n" + details
            self.send(text, "reminder", now=now, expires=now + REMINDER_STALE)

    def _bell_open(self, block: datetime, now: datetime) -> bool:
        """Whether a start notice for the thirty starting at ``block`` may still go out now: in its first five
        minutes, or, once it waited for the assistant's answer to finish, until the thirty is over."""
        if block <= now <= block + timedelta(minutes=5):
            return True
        return block <= now < block + timedelta(minutes=30) and \
            self.scheduler.state_db.get_sync_meta(BELL_WAITING) == block.isoformat()

    def _bell_to_come(self, span, now: datetime) -> bool:
        """At heavy with start notices on: this item's bell is still to ring (its thirty is ahead, or it's now and
        the bell can still go out)."""
        from thirties_core.fewer_messages import block_notices_on
        if self.level() != "heavy" or not block_notices_on(self.config) or self._held(now, "start") \
                or span.tentative or not self._bell_kind(span):
            return False
        block, own = thirty_of(now), thirty_of(span.start_dt)
        return own > block or (own == block and self._bell_open(block, now) and not self._quiet(now))

    @staticmethod
    def _bell_kind(span) -> bool:
        return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK, SpanCategory.CALENDAR, SpanCategory.SLEEP)

    def _details(self, title: str, task_id: str = "") -> str:
        """An item's description (what a reminder was given to say, or the user's note on it)."""
        try:
            from thirties_core.titles import ItemNotes
            return ItemNotes(self.scheduler.state_db).get(task_id or "", title)
        except Exception:
            return ""

    def starts(self, now: datetime) -> None:
        """Heavy: a bell at every thirty where a planned item starts, naming what starts in it (routine steps
        too); times are told in thirties. Each item is announced at most once (moving it doesn't announce it
        again), and it counts as announced only once its bell has gone out: a bell held back leaves it untold.

        Held: while asleep (apart from the bedtime bell itself) or in a firm calendar event, during a quiet the
        user asked for, right after the morning message (it just said what's on), and when start notices are
        switched off (fewer_messages.py). While the assistant is answering, the bell waits and goes out once the
        answer is done, if its thirty isn't over."""
        from thirties_core.fewer_messages import block_notices_on
        if self.level() != "heavy" or not block_notices_on(self.config):
            return
        plan = self.manager().day_plan
        block = thirty_of(now)
        quiet = self._quiet(now)  # asleep or in a firm event: only the bedtime bell, as the night's sleep begins
        if quiet and not any(s.category == SpanCategory.SLEEP and not s.tentative and thirty_of(s.start_dt) == block
                             for s in plan.active_spans()):
            return
        db = self.scheduler.state_db
        if not self._bell_open(block, now):
            return  # right at the start of a thirty (or once the answer it waited for is done)
        if self._held(now, "start"):
            return  # a quiet the user asked for
        announced = self._remembered("starts_announced")
        by_id = {s.id: s for s in plan.spans}

        def told_already(s) -> bool:
            """Announced before, as this item or as an earlier copy of it: re-planning makes a new item (a new
            id), and "Birthday cards" was announced four times on 10-21. A second one after the first is
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
                    and s.status == SpanStatus.PLANNED and self._bell_kind(s)
                    and (not quiet or s.category == SpanCategory.SLEEP)]
        if not starting:
            return
        try:  # the morning message just said what's on: no block notice on top of it (11-02 replay)
            morning = db.get_sync_meta("morning_sent_at")
            if morning and timedelta(0) <= now - datetime.fromisoformat(morning) < timedelta(minutes=10):
                return
        except ValueError:
            pass
        if self._turn_running():  # the assistant is answering: the bell waits for it
            db.set_sync_meta(BELL_WAITING, block.isoformat())
            return
        sleep = [s for s in starting if s.category == SpanCategory.SLEEP]
        rest = sorted((s for s in starting if s.category != SpanCategory.SLEEP), key=lambda s: s.start_dt)
        if rest:
            from thirties_core.memory import ReminderStore
            pinged = {r["span_id"] for r in ReminderStore(db).all_records() if r.get("span_id")}
            fixed = self.manager().tools._is_fixed  # a fixed thing is told by its real time; the rest by block
            said = [f"{self._name(s)} at {format_clock(s.start_dt)}" if s.start_dt != block and fixed(s)
                    else self._name(s) for s in rest]
            bell = f"🔔 {format_clock(block)}: " + ", ".join(said)
            details = [(self._name(s), self._details(s.title, s.task_id or "")) for s in rest if s.id in pinged]
            details = [(t, d) for t, d in details if d]
            if details:  # a reminder's details: at heavy the bell is its ping
                bell += "\n" + "\n".join(d if len(rest) == 1 else f"{t}: {d}" for t, d in details)
            self.send(self._with_next(bell, now, with_now=False),
                      "start", now=now, expires=block + timedelta(minutes=30))  # what's next under it, when new
        if sleep:
            self.send(f"🌙 Bedtime ({format_clock(block)}). Wind down and rest well!", "start", now=now,
                      expires=block + timedelta(minutes=30))
        for s in starting:  # told: never announced again
            announced[s.id] = now.date().isoformat()
            announced["title:" + s.title.strip().lower()] = f"{now.date().isoformat()}|" + " ".join(ids_titled(s) + [s.id])
        self._remember("starts_announced", announced, now)
        if db.get_sync_meta(BELL_WAITING) == block.isoformat():
            db.set_sync_meta(BELL_WAITING, "")

    def panel_ticks(self, now: datetime) -> None:
        """What was ticked done or skipped in the app's day panel reaches the phone, with what's next: one message
        once the ticking stops for a bit (PANEL_QUIET), never one per tick (10-25: ticks in the app, and the phone
        went on listing them as not marked). Only with a phone paired: the app shows the tick itself. It goes in
        the conversation too (one conversation), never as a desktop notification."""
        if self.bridge is None or self.level() == "off":
            return
        from thirties_core import plan_log
        db = self.scheduler.state_db
        with db._get_connection() as conn:
            plan_log._ensure(db, conn)
            raw = db.get_sync_meta("panel_ticks_seen")
            if raw is None or not raw.isdigit():  # first look: only what's ticked from now on
                top = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM plan_changes").fetchone()["n"]
                db.set_sync_meta("panel_ticks_seen", str(top))
                return
            rows = [dict(r) for r in conn.execute(
                "SELECT id, at, span_id, change FROM plan_changes WHERE id > ? AND via = 'Day page' ORDER BY id",
                (int(raw),)).fetchall()]
        ticks = [r for r in rows if "marked done" in r["change"] or "marked skipped" in r["change"]]
        if not rows:
            return
        try:
            newest = datetime.fromisoformat(rows[-1]["at"]).timestamp()
        except ValueError:
            newest = 0.0
        if ticks and time.time() - newest < PANEL_QUIET:
            return  # still ticking
        db.set_sync_meta("panel_ticks_seen", str(rows[-1]["id"]))
        if not ticks:
            return
        manager = self.manager()
        with manager.turn_lock:
            manager.reload_plan()
        plan = manager.day_plan
        if not (plan.day_start <= now < plan.day_end):
            return
        by_id = {s.id: s for s in plan.spans}
        done, skipped, seen = [], [], set()
        for r in ticks:
            span = by_id.get(r["span_id"])
            if span is None or span.id in seen:
                continue
            seen.add(span.id)
            if span.status == SpanStatus.DONE:
                done.append(self._name(span))
            elif span.status == SpanStatus.SKIPPED:
                skipped.append(self._name(span))
        if not done and not skipped:
            return  # ticked and unticked again
        def listed(titles):
            return titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + f" and {titles[-1]}"
        said = " ".join(x for x in (f"Marked done in the app: {listed(done)}." if done else "",
                                    f"Skipped in the app: {listed(skipped)}." if skipped else "") if x)
        from thirties_core import whats_next
        from thirties_core.messages import join_sections
        part = whats_next.summary(manager.tools, now)  # as the person wants it (reply_extras.py)
        if part:
            whats_next.SentSummaries(db).mark(plan, now)
        self.send(join_sections(said, part), "panel", now=now, desktop=False)

    def evening_catch_up(self, now: datetime) -> None:
        """Once an evening: what's still unmarked from today and nobody has asked about yet goes into the one
        batched status question, worded like a check-in (catch_up.py). Nothing moves by itself: unmarked is
        unknown, and the user's answer is acted on as their report."""
        if self.level() in ("off", "light") or self._quiet(now) or self._talking(now) or self._turn_running() \
                or self._held(now) or self._checkins_paused():  # a question: none while nobody answers
            return
        from thirties_core import catch_up
        from thirties_core.messages import missed_for
        tools = self.manager().tools
        asked = self._remembered("checkins_asked")
        grace = timedelta(minutes=self.config.assistant.checkin_after_minutes)
        runs = back_to_back(s for s in tools.day_plan.active_spans() if self._plannable(s) and not s.tentative)
        items = [s for s in catch_up.behind(tools, missed_for(tools, now))  # never meals, routines or appointments
                 if s.id not in asked  # a check-in asked about it already: once
                 and runs.get(s.id, s.end_dt) + grace <= now]  # its run's own question is still to come
        if not catch_up.due(tools, now, items):
            return
        catch_up.mark(tools, now, items)
        for s in items:
            asked[s.id] = now.date().isoformat()
        self._remember("checkins_asked", asked, now)
        titles = [s.title for s in sorted(items, key=lambda s: s.start_dt)]
        names = [self._name(s) for s in sorted(items, key=lambda s: s.start_dt)]
        self._asked(now, titles)
        from thirties_core.away import LastSentList  # the list it carried counts as sent (away.py)
        LastSentList(self.scheduler.state_db).mark(now, items)
        self.send(catch_up.question(names), "checkin", now=now, expires=now + timedelta(minutes=30))

    # --- the people and tickets duties. Each runs inside the tick's own try/except, so one failing
    # never stops the service. ---

    def people_nudge(self, now: datetime) -> None:
        from thirties_core import people
        people.background_duty(self, now)

    def rush_tickets(self, now: datetime) -> None:
        from thirties_core import tickets
        tickets.background_duty(self, now)

    # --- end of the extra duties ---

    def contextual(self, now: datetime) -> None:
        """"Have you left yet?", "are you on your way?": only for the items that matter (contextual.py), once
        each, at light and up, never while asleep. During a quiet the user asked for they wait, and count as sent
        only once they went out."""
        if self._held(now, "contextual"):
            return
        from thirties_core.contextual import due
        plan = self.manager().day_plan
        asleep = any(s.start_dt <= now < s.end_dt for s in plan.active_spans()
                     if s.category == SpanCategory.SLEEP and not s.tentative)
        if asleep:
            return
        for moment in due(plan, now, self.manager().tools._is_fixed, self.scheduler.state_db, self.level()):
            self.send(moment.text, "contextual", now=now, expires=moment.until)

    def answer_lost_turns(self, now: datetime) -> None:
        """A turn the model never answered (it timed out, even after retries) is tried again in the background,
        and the reply is sent to the desktop and the phone (T1: a message from the user never got an answer, 10-17)."""
        if self._answering:
            return
        manager = self.manager()
        with manager.turn_lock:
            manager._catch_up()  # the window's manager may have kept it: it's in the stored conversation
        if not manager.unanswered():
            return
        self._answering = True
        # messages typed in the app go to the phone with their reply through the mirror (mirror_turn): never twice
        mirrored = self.bridge is not None and getattr(self.config.telegram, "mirror_desktop", True) and \
            any(m.get("via") == "app" for m in manager.unanswered())

        def run() -> None:
            try:
                reply = self.manager().answer_unanswered(now if self._fixed_now else None)
                if reply:
                    self.send(reply, "reply", to_conversation=False, phone=not mirrored)  # the turn stored it
            except Exception as e:
                logger.warning("Answering a lost turn failed: %s", e)
            finally:
                self._answering = False
        if self.run_in_threads:
            threading.Thread(target=run, daemon=True, name="LostTurn").start()
        else:
            run()

    def _mirror(self, texts: List[str], reply: str, reply_sent: bool) -> None:
        """The desktop chat on the phone too (T2, 10-28 13:02): what the user typed in the app, marked as from the
        desktop, then the reply (unless the turn was Telegram's, which sends the reply itself)."""
        if self.bridge is None or not getattr(self.config.telegram, "mirror_desktop", True):
            return
        self.bridge.broadcast("🖥 You, on the desktop:\n" + "\n".join(texts), parse_mode=None)
        if not reply_sent and reply:
            self.bridge.broadcast(reply)

    def moot(self, now: datetime) -> None:
        """Before anything asks: what has gone moot with nothing said (lunch once dinner's begun, yesterday's
        breakfast) is stored as unknown, so no check-in, wrap-up or morning offer brings it up (kinds.py; 10-23,
        10-29)."""
        if time.time() - self._last_moot < self.MOOT_EVERY:
            return
        self._last_moot = time.time()
        manager = self.manager()
        if manager.tools.assistant is not None:
            with manager.turn_lock:
                manager.tools.assistant.settle_moot(now=now)

    def morning_hello(self, now: datetime) -> None:
        """The first message of the day, once, at sunrise or the scheduled wake-up (assistant.morning_message_at):
        "let me know when you're up", with the day's items and no times if assistant.morning_message_items is on.
        Nothing is built until the user replies (wake.py)."""
        from thirties_core.wake import RECENT_KEY, hello_text, hello_time, user_is_up, write_hello
        if self.level() == "off":
            return
        db = self.scheduler.state_db
        manager = self.manager()
        plan = manager.day_plan
        if now >= plan.day_end - timedelta(hours=3):  # before sunrise: the coming day's wake-up may be due already
            plan = manager.tools.plan_for(plan.target_date + timedelta(days=1))  # (11-05: the wake-up time mode
            # is its own choice, not "sunrise or later")
        if user_is_up(db, plan, now):
            return
        key = f"morning_hello:{plan.target_date.isoformat()}"
        if db.get_sync_meta(key) or db.get_sync_meta(f"rollover_offered:{plan.target_date.isoformat()}"):
            return
        history = db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
        at = hello_time(self.config, plan, history)
        bed = min((s.start_dt for s in plan.spans if s.category == SpanCategory.SLEEP and s.start_dt > plan.day_start),
                  default=plan.day_end)
        if at is None or not at <= now < bed - timedelta(hours=2):  # never a "good morning" at bedtime
            return
        db.set_sync_meta(key, now.isoformat())
        titles = self._plate(plan) if getattr(self.config.assistant, "morning_message_items", True) else []
        mode = "sunrise" if getattr(self.config.assistant, "morning_message_at", "wake") == "sunrise" else "wake"
        try:
            recent = json.loads(db.get_sync_meta(RECENT_KEY) or "[]")
        except ValueError:
            recent = []
        from thirties_core.brains import brain_choice  # the brain actually in use (Gemma only behind its switch)
        engine = self.inference_engine if brain_choice(self.config, db) == "frontier" else None
        line = write_hello(engine, mode, at, recent, now)  # the model writes it fresh each morning (11-05)
        db.set_sync_meta(RECENT_KEY, json.dumps((recent + [line])[-5:]))
        self.send(hello_text(line, titles), "morning", now=now, day=plan.target_date)  # in that day's conversation

    def _plate(self, plan) -> List[str]:
        """What's on the plate today, without times and without changing anything, every item however many: first
        what the user put on the day (fixed calendar events and things planned ahead on today's plan, what they put
        on the day's list without a time, list items due today), then what Python set aside for the day by itself
        (a skip's later day, not_today.py). Never routines, meals or anchors, and never yesterday's leftovers: they
        aren't today's by default (carry.py)."""
        from thirties_core.assistant_tools import _short
        assistant = self.manager().tools.assistant
        daily = assistant.routines.daily_titles() if assistant is not None else set()
        from thirties_core.item_path import label, span_label  # a nested item with its parents
        tasks = list(assistant.s.tasks) if assistant is not None else []

        def name(s) -> str:
            said = span_label(s, tasks)
            return said if said != s.title else _short(s.title)
        out = [name(s) for s in sorted(plan.active_spans(), key=lambda s: s.start_dt)
               if not s.is_envelope and s.status == SpanStatus.PLANNED and s.title.strip().lower() not in daily
               and s.source != "routine"
               and s.category in (SpanCategory.CALENDAR, SpanCategory.ACTIVITY, SpanCategory.TASK)]
        picked: List[str] = []
        if assistant is not None and assistant._db is not None:
            # --- the day's list kept ahead (day_lists.AheadLists): the user's lines, and Python's set-asides last ---
            from thirties_core.carry import OpenPool
            from thirties_core.day_lists import AheadLists
            pool = OpenPool(assistant._db).all()
            day = plan.target_date.isoformat()
            for r in AheadLists(assistant._db).for_day(plan.target_date):
                rec = pool.get(r["title"].strip().lower()) or {}
                by_python = bool(rec.get("set_aside")) and not rec.get("named_day") and rec.get("start") == day
                (picked if by_python else out).append(_short(r["title"]))
            # --- end the day's list kept ahead ---
        if assistant is not None:
            day = plan.target_date.isoformat()
            from thirties_core.list_reading import offerable
            out += [label(t, tasks) for t in assistant.s.tasks if (t.due or "")[:10] == day and not t.someday
                    and t.title.strip().lower() not in daily and offerable(t)]
        return out + picked

    def rollover(self, now: datetime) -> None:
        """The morning, if no turn did it (morning.py): normally the user's first message of the day builds the
        day in its reply. This is the fallback (the model couldn't be reached, or the on-device brain). Marked
        done only after it has gone out."""
        from thirties_core import morning
        db = self.scheduler.state_db
        manager = self.manager()
        day = manager.day_plan.target_date
        if morning.is_done(db, day):
            return
        if not self._up(now) or self._turn_running() or self._quiet(now):  # never while asleep or mid-turn
            return
        facts, brief = morning.prepare(manager, self.level())
        self.propose_the_day(now, facts, brief)
        morning.mark_done(db, day, now)

    def propose_the_day(self, now: datetime, facts: str = "", brief: str = "") -> None:
        """The morning's one message (10-31: three long texts at sunrise were too many): what's
        already planned, yesterday's leftovers and the holiday note (``facts``), with one short proposal for the
        free time (day_proposal.py), and one question."""
        from thirties_core.day_proposal import morning_proposal
        manager = self.manager()
        try:
            text, stored = morning_proposal(manager, facts, brief)
        except Exception as e:
            logger.warning("The morning proposal failed: %s", e)
            text, stored = "", False
        if not text and not facts:
            return
        if not text:  # nothing to propose: the facts alone
            text, stored = facts, False
        elif not stored and facts:
            text = facts + "\n\n" + text
        if not stored and not text.startswith("☀️"):  # the model's own message starts so (MORNING_FACTS)
            text = "☀️ Good morning! " + text
        self.send(text, "rollover", now=now, to_conversation=not stored)

    def day_times(self, now: datetime) -> None:
        """B6: once a day each, "when did you wake up / start work?" and "done with work?" (day_times.due_question),
        unless the answer is already on the record."""
        from thirties_core.day_times import ASK_AT_LEVELS, due_question
        if self.level() not in ASK_AT_LEVELS or self._quiet(now) or self._talking(now) or self._checkins_paused() \
                or self._held(now, "checkin"):  # held back: not asked yet
            return
        plan = self.manager().day_plan
        history = self.scheduler.state_db.get_spans_between(plan.day_start - timedelta(hours=18), plan.day_start)
        asked = self._remembered("day_times_asked")
        due = due_question(plan, now, asked, history)
        if due:
            asked[due[0]] = now.date().isoformat()
            self._remember("day_times_asked", asked, now)
            self.send(due[1], "checkin", now=now)

    def sun(self, now: datetime) -> None:
        """Sunrise and sunset, if the user switched them on (sun_notices.py): asked for, so always sent."""
        from thirties_core.sun_notices import TEXT, WINDOW, due
        sent = self._remembered("sun_sent")
        key = due(self.config, now, sent)
        if key:
            sent[key] = now.date().isoformat()
            self._remember("sun_sent", sent, now)
            self.send(TEXT[key], "sun", now=now, expires=now + WINDOW * 6)  # in the chat too: the app and the
            # phone show one conversation (the user's parity rule, 10-30)

    def horizon_review(self, now: datetime) -> None:
        """Keep the longer plans current, quietly: when something that matters changes, and lightly each week or
        month, pull work forward where a plan has room (horizons.HorizonPlanner.review). The next morning says
        what moved."""
        manager = self.manager()
        if manager.tools.assistant is None or manager.scheduler is None:
            return
        from thirties_core.horizons import HorizonPlanner
        with manager.turn_lock:
            pulled = HorizonPlanner(manager.tools).review()
        if pulled:
            logger.info("Pulled forward into the longer plans: %d item(s)", len(pulled))

    def today_sync(self, now: datetime) -> None:
        """Every two minutes: catch up with hand edits to the Today note (AssistantTools.sync_today)."""
        if time.time() - self._last_today < self.TODAY_EVERY:
            return
        self._last_today = time.time()
        assistant = self.manager().tools.assistant
        if assistant is not None and assistant.morning_pending() and now.hour < 12:
            assistant.start_the_day()  # Joplin was closed at wake-up; it may be open now
        said = [m for m in (assistant.sync_today() if assistant is not None else []) if m]
        if said:  # at most one message a sync, however many lines it has (repairs say nothing: full_day.py)
            self.send("📝 " + "\n".join(said), "today", now=now)
        if assistant is not None:  # done items move to the month's Done note (done_log.py)
            from thirties_core.done_log import DoneLog
            filed = DoneLog(self.scheduler.state_db).sweep(assistant)
            if filed:
                logger.info("Done log: %s.", "; ".join(filed))

    def dropbox(self, now: datetime) -> None:
        """Now and then, in the afternoon: offer to empty the drop box, a home proposed for each item (soak 5)."""
        from thirties_core.placement_lists import DROPBOX_OFFER_HOURS
        if not DROPBOX_OFFER_HOURS[0] <= now.hour < DROPBOX_OFFER_HOURS[1] or \
                time.time() - getattr(self, "_last_dropbox", 0.0) < self.LISTS_EVERY:
            return  # looked at twice an hour at most: it reads the notes
        self._last_dropbox = time.time()
        if self.level() == "off" or self._quiet(now) or self._talking(now):
            return
        assistant = self.manager().tools.assistant
        offer = assistant.lists.dropbox_offer(now) if assistant is not None else ""
        if offer:
            self.send(offer, "dropbox", now=now)

    def joplin_presence(self, now: datetime) -> None:
        """While write-back is on and the sync peer is off: note every ten minutes whether the Joplin app is open,
        and offer the peer once if it's often closed (changes wait for it then)."""
        cfg = self.config.joplin
        if not cfg.write_back or cfg.peer_enabled or time.time() - self._last_presence < self.PRESENCE_EVERY:
            return
        self._last_presence = time.time()
        from thirties_core.joplin_peer import OFFER_TEXT, Presence
        presence = Presence(self.scheduler.state_db)
        presence.sample(bool(self.joplin_engine.api_client.ping()), now.date())
        if presence.should_offer(self.config):
            presence.offered()
            self.send(OFFER_TEXT, "joplin_peer", now=now)

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
                from thirties_core.backlog import remember_private
                from thirties_core.privacy import SHIELD
                # privacy first: hidden lines are learnt before the sorter reads a single note
                remember_private(self.scheduler, notes, SHIELD)
                sorter = list_sorter_for(self.config, self.scheduler.state_db, self.inference_engine)
                if sorter is None:
                    return
                counts = sort_lists(ListCatalog(self.scheduler.state_db), notes, tasks, sorter, self.today(),
                                    max_notes=10)  # a few notes each half hour, not the whole backlog at once
                from thirties_core.horizons import HorizonCatalog, sort_horizons
                counts.update({f"horizon_{k}": v for k, v in sort_horizons(
                    HorizonCatalog(self.scheduler.state_db), notes, tasks, sorter, self.today(), max_notes=10).items()})
                if any(counts.values()):
                    logger.info("Sorted lists: %d note roles, %d item urgencies, %d note and %d item horizons guessed.",
                                counts["roles"], counts["items"], counts["horizon_notes"], counts["horizon_items"])
                    with self._lock:
                        self._manager = None  # the next turn sees them
            except Exception as e:
                logger.warning("Sorting lists failed: %s", e)
            finally:
                self._sorting = False
        threading.Thread(target=run, daemon=True, name="ListSorting").start()

    def tick(self, now: Optional[datetime] = None) -> None:
        self._fixed_now = now is not None
        now = now or self.now()
        try:
            manager = self.manager()
            with manager.turn_lock:  # each duty sees the day as stored: 10-25, ticks made in the app's Agenda at
                manager.reload_plan()  # 10:53 were listed as "not marked yet" at 11:00
        except Exception as e:
            logger.warning("Reloading the day failed: %s", e)
        try:
            up = self._up(now)
        except Exception as e:
            logger.warning("Checking whether the user is up failed: %s", e)
            up = True
        waits = {self.model_fit, self.heads_up, self.stretches, self.wrap_up, self.check_ins, self.starts, self.contextual,
                 self.day_times, self.dropbox, self.joplin_presence, self.panel_ticks,
                 self.evening_catch_up, self.people_nudge, self.rush_tickets}  # nothing to say before the user is up
        for duty in (self.hold_check, self.model_fit, self.moot, self.morning_hello, self.rollover,
                     self.horizon_review, self.reminders, self.heads_up, self.stretches,
                     self.wrap_up, self.check_ins, self.starts, self.panel_ticks, self.evening_catch_up,
                     self.contextual, self.day_times, self.sun, self.today_sync, self.people_nudge, self.rush_tickets,
                     self.answer_lost_turns, self.dropbox, self.joplin_presence):
            if not up and duty in waits:
                continue
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
        if self.bridge is not None:
            from thirties_core.turns import add_mirror
            add_mirror(self._mirror)
        self._start_peer()
        t = threading.Thread(target=self._loop, daemon=True, name="ThirtiesBackground")
        t.start()
        self._threads.append(t)
        logger.info("Background service started (Telegram %s).", "on" if self.bridge else "off")

    def _start_peer(self) -> None:
        """The headless Joplin sync peer runs in its own thread (a sync can take minutes); off, its server stops."""
        from thirties_core import joplin_peer
        if not self.config.joplin.peer_enabled:
            joplin_peer.shutdown_all()
            return
        if not self.run_in_threads:
            return  # tests drive the peer themselves
        self.peer = joplin_peer.peer_for(self.config)

        def ready() -> None:
            self._last_outbox = 0.0  # the queued changes go now, not in a minute
        self.peer.on_ready = ready
        self.peer.notify = lambda text: self.send(text, "joplin_peer")
        self.peer.start()

    def _loop(self) -> None:
        while self.running:
            self.tick()
            for _ in range(self.TICK_SECONDS * 2):
                if not self.running:
                    return
                time.sleep(0.5)

    def stop(self) -> None:
        self.running = False
        if self.peer is not None:  # its server stays up across a settings reload; quitting stops it (atexit)
            self.peer.stop()
            self.peer = None
        from thirties_core.turns import remove_mirror
        remove_mirror(self._mirror)
        if self.bridge is not None:
            self.bridge.stop()
        self.bridge = None
        self._threads = []
