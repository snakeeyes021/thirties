"""Client-facing entry point for conversational planning.

``ConversationManager`` is what the GTK app, the Telegram bridge and the eval
harnesses talk to. It wires the shared ``PlanTools`` (schedule state and
invariants) to a brain (model-specific prompting and turn logic), and handles
developer slash commands that never reach a model.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.clock import EXACT_ASKED_RE, real_times, thirty_of, to_thirties
from thirties_core.astronomy import get_current_time, set_debug_time
from thirties_core.assistant_tools import Services
from thirties_core.brains import BRAIN_ALIASES, BRAINS, Brain, brain_choice, make_brain
from thirties_core.calendar_engine import CalendarEvent
from thirties_core.inference import InferenceEngine, MockInferenceEngine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.memory import Conversation
from thirties_core.models import DayPlan, TaskItem
from thirties_core.plan_tools import PlanTools
from thirties_core.scheduler import DeterministicScheduler
from thirties_core.turns import (LOST_RETRY_MINUTES, LOST_GIVE_UP_HOURS, LOST_TURN_REPLY, BrainUnavailable, Incoming,
                                 RetryingEngine, gate_for, mirror_turn)

logger = logging.getLogger(__name__)


def _undo_command() -> str:
    from thirties_core.agent_mode import UNDO_COMMAND
    return UNDO_COMMAND


class ConversationManager:
    """Pairs a day's PlanTools with a brain, routes user turns to it, and keeps the conversation.

    Conversations persist per diurnal day: opening a day continues its most recent
    conversation (the same one the Telegram bridge uses), and ``new_conversation``
    starts a fresh one that becomes the current one everywhere.
    """

    def __init__(
        self,
        day_plan: DayPlan,
        tasks: List[TaskItem],
        ambiguous_events: List[CalendarEvent],
        scheduler: DeterministicScheduler,
        joplin_engine: Optional[JoplinEngine] = None,
        inference_engine: Optional[InferenceEngine] = None,
        active_notes: Optional[List[Dict[str, Any]]] = None,
        brain: Optional[Brain] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.joplin_engine = joplin_engine
        # One turn at a time per day's conversation, across every manager in this process (the window's and the
        # background service's, which runs Telegram), and an inbox for messages that arrive meanwhile (turns.py).
        self.gate = gate_for(getattr(scheduler.state_db, "db_path", None) if scheduler else None,
                             day_plan.target_date.isoformat())
        self.turn_lock = self.gate.lock
        self._backlog_stamp = None
        inference_engine = RetryingEngine.wrap(inference_engine)  # timeouts are retried, then reported (T1)  # when Joplin's data was last read (its file's mtime)
        if active_notes is None:
            active_notes = self.joplin_engine.fetch_active_notes() if self.joplin_engine else []
        services = Services.for_db(scheduler.state_db, tasks, joplin_engine) if scheduler else None
        self.tools = PlanTools(day_plan, ambiguous_events, scheduler, services=services)
        config = scheduler.config if scheduler else None
        state_db = scheduler.state_db if scheduler else None
        # Without an explicit brain, follow this device's choice (the app's menu or /brain), even mid-day.
        self._brain_args = None if brain else (config, inference_engine or MockInferenceEngine(), tasks, active_notes)
        self.brain_name = brain_choice(config, state_db) if not brain else \
            ("slm" if type(brain).__name__ == "SlmBrain" else "frontier")
        self.brain: Brain = brain or make_brain(config, self.tools, self._brain_args[1], tasks=tasks,
                                                active_notes=active_notes, choice=self.brain_name)
        if brain is not None and getattr(brain, "inference_engine", None) is not None:
            brain.inference_engine = RetryingEngine.wrap(brain.inference_engine)
        self.conversation: Optional[Conversation] = None
        self._followed_latest: Optional[str] = None
        if services:
            conv = services.conversations.get(conversation_id) if conversation_id else None
            self._load(conv or services.conversations.latest_for_day(day_plan.target_date))

    # --- choosing the brain ---

    def set_brain(self, choice: str) -> str:
        """Switch this device to another brain, keeping the conversation. Returns the brain's name."""
        from thirties_core.brains import offered_brains
        offered = offered_brains(self.scheduler.config if self.scheduler else None)
        name = BRAIN_ALIASES.get(str(choice).strip().lower())
        if name is None or name not in offered:
            from thirties_core.brains import BRAIN_WORDS
            raise ValueError(f"Unknown assistant '{choice}'. Choose one of: "
                             f"{', '.join(BRAIN_WORDS.get(n, n) for n in offered)}.")
        if self.scheduler is not None:
            from thirties_core.settings import Settings
            Settings(self.scheduler.state_db).set("assistant.brain", name)
        if name != self.brain_name:
            self._use_brain(name)
        return name

    def _use_brain(self, name: str) -> None:
        if self._brain_args is None:
            return  # a brain given explicitly (tests, evals) stays
        config, engine, tasks, notes = self._brain_args
        self.brain = make_brain(config, self.tools, engine, tasks=tasks, active_notes=notes, choice=name)
        self.brain_name = name
        self._load(self.conversation)
        self.warm_in_background()

    # --- conversation persistence ---

    @property
    def _store(self):
        return self.tools.services.conversations if self.tools.services else None

    def _load(self, conv: Optional[Conversation]) -> None:
        self.conversation = conv
        self.brain.reset()
        if conv:
            self.brain.messages = [self.brain.messages[0]] + list(conv.messages)
            for m in conv.messages:  # stored before messages carried a time: don't give them today's
                m.setdefault("at", "")
        self._note_if_latest()

    def _note_if_latest(self) -> None:
        """Remember whether we're on the day's newest conversation (refresh then follows newer ones)."""
        if self._store is None or self.conversation is None:
            return
        latest = self._store.latest_for_day(self.day_plan.target_date)
        if latest and latest.id == self.conversation.id:
            self._followed_latest = latest.id

    def _save(self) -> None:
        if self._store is None:
            return
        if self.conversation is None:
            self.conversation = self._store.create(self.day_plan.target_date)
        self._stamp()
        self.conversation.messages = [m for m in self.brain.messages if m.get("role") != "system"]
        self._store.save(self.conversation)
        self._note_if_latest()

    def reload_backlog(self) -> None:
        """Re-read the lists when Joplin's data changed (by hand, or by Thirties' own writes), so nobody plans from
        an old copy: the window kept one from startup across days and offered a done item again (10-24)."""
        if self.scheduler is None or self.joplin_engine is None:
            return
        try:
            path = self.joplin_engine.config.resolve_joplin_db_path()
            stamp = path.stat().st_mtime if path else None
        except (OSError, AttributeError):
            stamp = None
        if stamp is not None:  # and the done records: one taken back brings its item back (backlog.done_stamp)
            from thirties_core.backlog import done_stamp
            stamp = (stamp, done_stamp(self.scheduler))
        if stamp is None or stamp == self._backlog_stamp:
            return
        from thirties_core.backlog import load_backlog
        media = [t for t in (self.tools.services.tasks if self.tools.services else []) if t.id.startswith("media:")]
        try:
            tasks, notes = load_backlog(self.scheduler.config, self.joplin_engine, self.scheduler, media=media)
        except Exception as e:
            logger.warning("Re-reading the lists failed: %s", e)
            return
        self._backlog_stamp = stamp
        if self.tools.services is not None:
            self.tools.services.tasks = list(tasks)
        if hasattr(self.brain, "tasks"):
            self.brain.tasks = list(tasks)
        if hasattr(self.brain, "active_notes"):
            self.brain.active_notes = list(notes)
        if self._brain_args is not None:
            self._brain_args = (*self._brain_args[:2], tasks, notes)

    def _stamp(self, via: str = "") -> None:
        """When each message was stored, and by which build (and, for a turn, which channel it came through)."""
        from thirties_core import build
        now = get_current_time(self.day_plan.day_start.tzinfo).isoformat(timespec="seconds")
        for m in self.brain.messages:
            if m.get("role") != "system" and "at" not in m:
                m["at"], m["build"] = now, build.BUILD
                if via:
                    m["via"] = via

    def history(self, times: bool = False) -> List[Dict[str, str]]:
        """The visible transcript of the current conversation: user and assistant text only. With ``times``, each
        message also has "at" (when it arrived or was said; "" for old messages) for the app's chat."""
        return [{"role": m["role"], "content": m.get("content", ""),
                 **({"at": m.get("at", ""), **({"undelivered": True} if m.get("undelivered") else {})}
                    if times else {})}
                for m in self.brain.messages
                if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
                and not m.get("hidden") and not str(m.get("content", "")).startswith("[System check")]

    def conversations_for_day(self) -> List[Conversation]:
        return self._store.for_day(self.day_plan.target_date) if self._store else []

    def new_conversation(self) -> None:
        """Start a fresh conversation for this day (it becomes the current one)."""
        if self.conversation is not None and not self.conversation.messages:
            return  # the current one is already empty
        if self._store and self.conversation is not None:
            from thirties_core.memory import summarize_conversation
            self.conversation.summary = summarize_conversation(self.conversation)
            self._store.save(self.conversation)
        self._load(self._store.create(self.day_plan.target_date) if self._store else None)

    def switch_conversation(self, conversation_id: str) -> bool:
        conv = self._store.get(conversation_id) if self._store else None
        if conv is None:
            return False
        self._load(conv)
        latest = self._store.latest_for_day(self.day_plan.target_date)
        self._followed_latest = conv.id if latest and latest.id == conv.id else None
        return True

    def reload_plan(self) -> None:
        """Reload only the day from storage: another client may have changed it (a tick in the app's Agenda)."""
        if self.scheduler is None:
            return
        plan, pending = self.scheduler.build_day_plan(self.day_plan.target_date)
        self.tools.day_plan = plan
        self.tools.ambiguous_events = list(pending)
        self.tools._other_days.clear()

    def refresh(self) -> None:
        """Reload the day and the current conversation from storage.

        Another client (the desktop app or the Telegram bridge) may have changed the
        schedule or continued the conversation since this one last looked.
        """
        if self.scheduler is None:
            return
        self.reload_backlog()
        wanted = brain_choice(self.scheduler.config, self.scheduler.state_db)
        if wanted != self.brain_name:
            self._use_brain(wanted)  # switched on another client (the app's menu, or /brain in Telegram)
        plan, pending = self.scheduler.build_day_plan(self.day_plan.target_date)
        self.tools.day_plan = plan
        self.tools.ambiguous_events = list(pending)
        self.tools._other_days.clear()
        if self._store:
            day_convs = self._store.for_day(plan.target_date)
            latest = day_convs[-1] if day_convs else None
            current = next((c for c in day_convs if self.conversation and c.id == self.conversation.id), None)
            if current is None or (self.conversation.id == self._followed_latest
                                   and latest and latest.id != current.id):
                # Follow the day's newest conversation (e.g. /new from Telegram), unless the user
                # deliberately switched to an older one here.
                if latest:
                    self._load(latest)
            elif current.updated_at > self.conversation.updated_at:
                self._load(current)  # same conversation, continued elsewhere
            self._note_if_latest()

    def warm_in_background(self) -> None:
        """Let a brain that needs it (the on-device SLM) load before the first message."""
        warm = getattr(self.brain, "warm", None)
        if warm is None:
            return
        import threading

        def run() -> None:
            try:
                warm()
            except Exception as e:
                logger.warning("Brain warm-up failed: %s", e)
        threading.Thread(target=run, daemon=True, name="BrainWarmup").start()

    def add_assistant_message(self, text: str, kind: str = "", undelivered: bool = False) -> None:
        """Record something the assistant said on its own (e.g. a Telegram check-in, or a start-of-block notice:
        kind "notice"). ``undelivered``: it didn't reach the phone (the app shows a quiet mark)."""
        message = {"role": "assistant", "content": text}
        if kind:
            message["kind"] = kind
        if undelivered:
            message["undelivered"] = True
        with self.turn_lock:
            self._catch_up()
            self.brain.messages.append(message)
            self._stamp(via="background")
            self._save()

    def mark_delivery(self, words: str, undelivered: bool) -> bool:
        """Set or clear the quiet "not delivered to your phone" mark on the latest message with these words."""
        from thirties_core.memory import mark_delivery
        with self.turn_lock:
            self._catch_up()
            if not mark_delivery(self.brain.messages, words, undelivered):
                return False
            self._save()
            return True

    def record_exchange(self, said: str, reply: str, via: str = "telegram", undelivered: bool = False) -> None:
        """Something the user said and its answer that took no turn (a Telegram command, an edit after the
        reply): both in the conversation, as on the phone. One conversation, wherever it was said."""
        with self.turn_lock:
            self._catch_up()
            self.brain.messages.append({"role": "user", "content": said, "kind": "command"})
            self._stamp(via=via)
            answer = {"role": "assistant", "content": reply, "kind": "command"}
            if undelivered:
                answer["undelivered"] = True
            self.brain.messages.append(answer)
            self._stamp(via=via)
            self._save()

    def assistant_turn(self, instruction: str) -> str:
        """A turn the assistant starts itself (the morning proposal, day_proposal.py): the instruction is for the
        model only and stays out of the visible history; the reply is stored as the assistant's message."""
        with self.turn_lock:
            reply = self.brain.respond(instruction)
            for m in self.brain.messages:
                if m.get("role") == "user" and m.get("content") == instruction:
                    m["hidden"] = True
            self._stamp(via="background")
            self._save()
            return reply

    @property
    def day_plan(self) -> DayPlan:
        return self.tools.day_plan

    @property
    def ambiguous_events(self) -> List[CalendarEvent]:
        return self.tools.ambiguous_events

    @property
    def scheduler(self) -> Optional[DeterministicScheduler]:
        return self.tools.scheduler

    @property
    def messages(self) -> List[Dict[str, Any]]:
        return self.brain.messages

    @messages.setter
    def messages(self, value: List[Dict[str, Any]]) -> None:
        self.brain.messages = value

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Run a schedule tool directly, bypassing the model: the app's own controls, so the user did it. Python's
        lines for the user (a reminder to export a finished book) come after the result, for the Day page to show."""
        self.tools.by_hand = True
        try:
            result = self.tools.execute(name, arguments)
        finally:
            self.tools.by_hand = False
        notes = self._user_notes()
        return result + "".join(f"\n{n}" for n in notes)

    def _user_notes(self) -> List[str]:
        assistant = getattr(self.tools, "assistant", None)
        return assistant.take_user_notes() if assistant is not None and hasattr(assistant, "take_user_notes") else []

    def _now_iso(self) -> str:
        return get_current_time(self.day_plan.day_start.tzinfo).isoformat(timespec="seconds")

    def _catch_up(self) -> None:
        """Another manager (the window's, or the background service's for Telegram) may have added to this
        conversation since this one last looked: continue from the stored copy, never write over it."""
        if self._store is None:
            return
        if self.conversation is None:  # the day had none when this one started; another client may have begun it
            latest = self._store.latest_for_day(self.day_plan.target_date)
            if latest is not None:
                self._load(latest)
            return
        stored = self._store.get(self.conversation.id)
        if stored is not None and stored.updated_at > self.conversation.updated_at:
            self._load(stored)

    def enqueue(self, user_text: str, via: str = "app") -> Incoming:
        """Add a message to the day's inbox without taking a turn: the next turn answers it with the rest (the
        Telegram bridge uses this for a burst that arrived together)."""
        return self.gate.enqueue(user_text, via, self._now_iso())

    def send_user_message(self, user_text: str, via: str = "app", quiet: float = 0.0) -> str:
        """Handle one user message: developer slash commands here, everything else via the brain. `via` is the
        channel ("app", "telegram"), stored with the turn's messages.

        Messages that arrive while a turn is under way are answered together by the next turn, so a burst gets
        one reply (A8); with ``quiet``, this also waits that many seconds of quiet before starting. A caller whose
        message went into another caller's turn gets "" back: the reply is already in the conversation (and was
        sent where that turn came from)."""
        if user_text.strip().startswith("/"):
            with self.turn_lock:
                self._catch_up()
                return self._command(user_text, via)
        # a short answer to "Done with work?", when it's the last thing asked: recorded, with what's left of the day
        from thirties_core import work_end_answer
        from thirties_core import agent_mode  # Agent mode: every message goes to Gemini as is (agent_mode/)
        try:
            ended = "" if agent_mode.for_manager(self) else work_end_answer.answer(self, user_text, via)
        except Exception as e:
            logger.warning("Answering the end-of-work question failed: %s", e)
            ended = ""
        if ended:
            return ended
        mine = self.enqueue(user_text, via)  # first: messages keep the order they came in
        self.gate.wait_quiet(quiet)
        with self.turn_lock:
            if mine.taken:
                return ""
            batch = self.gate.take() or [mine]
            reply, _ = self._turn(batch, via)
            return reply

    def _turn(self, batch: List[Incoming], via: str, attempts: int = 0) -> tuple:
        # --- developer Agent mode: the turn goes to one full Gemini run, not this assistant (agent_mode/) ---
        from thirties_core import agent_mode
        if agent_mode.for_manager(self):
            done = agent_mode.take_turn(self, batch, via)
            if done is not None:
                return done
            from thirties_core.agent_mode.route import with_notice
            reply, answered = self._usual_turn(batch, via, attempts)
            return with_notice(self, reply), answered
        # --- end Agent mode ---
        return self._usual_turn(batch, via, attempts)

    def _usual_turn(self, batch: List[Incoming], via: str, attempts: int = 0) -> tuple:
        # --- the usage limit: a simpler assistant answers, or the messages wait until it resets (fallback.py) ---
        from thirties_core import fallback
        held = fallback.before_turn(self, batch, via)
        if held is not None:
            return held, False
        # --- end usage limit ---
        from thirties_core.joplin_writeback import current_turn
        from thirties_core.engineer_notes import without_note  # each Joplin write's cause: no engineer note
        token = current_turn.set(f"[{via}] " + " / ".join(without_note(i.text) for i in batch))
        try:
            return self._run_turn(batch, via, attempts)
        finally:
            current_turn.reset(token)
            self.tools.user_message = None  # a drag in the Agenda later isn't judged by this message
            self.tools.prior_exchange = None
            from thirties_core.reopen import reply_before
            reply_before.set("")
            self.tools.pending_dones = None
            self.tools.done_asks = None
            self.tools.picked_dones = None
            self.tools.recent_said = None

    def _command(self, user_text: str, via: str) -> str:
        """Developer / runtime simulation slash commands: never reach a model."""
        self._stamp()
        clean_text = user_text.strip()
        cmd = clean_text.lower()
        if cmd == "/night":
            set_debug_time("22:30")
            reply = "Simulated time set to 10:30 PM (Nighttime Mode). The Solar Arc has shifted to the Nocturnal Lunar Arc."
        elif cmd == "/midnight":
            set_debug_time("00:00")
            reply = "Simulated time set to 12:00 AM (Midnight Mode)."
        elif cmd in ("/day", "/midday"):
            day_sec = (self.day_plan.sunset - self.day_plan.sunrise).total_seconds()
            midday_dt = self.day_plan.sunrise + timedelta(seconds=day_sec / 2.0)
            set_debug_time(midday_dt.strftime("%H:%M"))
            reply = f"Simulated time set to daytime solar midday ({midday_dt.strftime('%I:%M %p').lstrip('0')})."
        elif cmd == "/noon":
            set_debug_time("12:00")
            reply = "Simulated time set to 12:00 PM (Clock Noon Mode)."
        elif cmd in ("/morning", "/sunrise"):
            set_debug_time(self.day_plan.sunrise.strftime("%H:%M"))
            reply = f"Simulated time set to sunrise ({self.day_plan.sunrise.strftime('%I:%M %p').lstrip('0')})."
        elif cmd in ("/reset", "/now"):
            set_debug_time(None)
            reply = "Simulated time cleared. Real-world system clock restored."
        elif cmd.split()[0] == "/brain":
            arg = clean_text.split(maxsplit=1)[1] if len(clean_text.split()) > 1 else ""
            if not arg:
                from thirties_core.brains import offered_brains
                others = [n for n in offered_brains(self.scheduler.config if self.scheduler else None)
                          if n != self.brain_name]
                from thirties_core.brains import BRAIN_WORDS
                reply = f"Using {BRAINS[self.brain_name]}." + (
                    " Switch with " + " or ".join(f"/brain {BRAIN_WORDS.get(n, n)}" for n in others) + "."
                    if others else "")
            else:
                try:
                    reply = f"Now using {BRAINS[self.set_brain(arg)]}. This conversation continues."
                except ValueError as e:
                    reply = str(e)
        elif cmd == _undo_command():  # Agent mode: Gemini's last change taken back (agent_mode/)
            from thirties_core import agent_mode
            reply = agent_mode.undo_last(self)
        elif cmd.startswith("/time"):
            val = clean_text[5:].strip()
            set_debug_time(val)
            reply = f"Simulated time set to {val}."
        else:
            reply = f"Unknown command: {clean_text}. Available: /brain, /night, /midnight, /day, /midday, /noon, /sunrise, /reset, /time HH:MM"
        self.messages.append({"role": "user", "content": user_text})
        self.messages.append({"role": "assistant", "content": reply})
        self._stamp(via)
        self._save()
        return reply

    def _morning_in_turn(self) -> bool:
        """The user's first message of the day (the morning not done yet): build the day now and tell the model
        to make its reply the morning's one message. True when that's this turn's job."""
        from thirties_core import morning
        db = self.scheduler.state_db if self.scheduler is not None else None
        self._morning_facts = ""
        if db is None or not self.tools.is_current(self.day_plan) or morning.is_done(db, self.day_plan.target_date):
            return False
        try:
            level = getattr(self.scheduler.config.assistant, "message_level", "medium")
            facts, brief = morning.prepare(self, level)
        except Exception as e:  # the background tries again later
            logger.warning("Preparing the morning failed: %s", e)
            return False
        if self.brain_name != "frontier":  # the pipeline: the day is built now, and its facts go after the reply
            self._morning_facts = facts
            return True
        self.brain.messages.append({"role": "user", "content": morning.in_turn_instruction(facts, brief),
                                    "hidden": True})
        return True

    def _run_turn(self, batch: List[Incoming], via: str, attempts: int = 0) -> tuple:
        """One turn for everything in ``batch``. Returns (reply, answered); answered is False when the model
        couldn't be reached and the messages were kept for later."""
        self._catch_up()
        merged = "\n".join(i.text for i in batch)
        # the user's words about their plan, for every check this turn: never a note to the engineers
        from thirties_core.engineer_notes import as_said, without_note
        words, earlier = without_note(merged), as_said(self.brain.messages)
        self.tools.user_message = words
        from thirties_core.done_reference import prior_exchange  # what "that" or "both" can point back to
        self.tools.prior_exchange = prior_exchange(earlier)
        from thirties_core.reopen import reply_before  # what a bare no answers, for the check before Joplin
        reply_before.set(self.tools.prior_exchange[1])
        from thirties_core.done_words import pending_dones  # a done asked for before and refused (done_words.py)
        self.tools.pending_dones = pending_dones(earlier)
        from thirties_core.plain_done import done_asks  # dones refused earlier, said again (plain_done.py)
        self.tools.done_asks = done_asks(earlier)
        self.tools.picked_dones = set()  # dones the model picked this turn: the reply names them (reply_checks.py)
        from thirties_core.change_asks import recent_said  # a removal or move asked for just before (change_asks.py)
        self.tools.recent_said = recent_said(earlier)
        from thirties_core.routine_times import earlier_today  # times given earlier today (routine_times.py)
        self.tools.said_earlier = earlier_today(earlier)
        self.tools.shown_schedule = None
        from thirties_core import whats_next
        self._plan_before = whats_next.snapshot(self.day_plan)  # what the turn moves, for what's next
        self._stamp()
        self.reload_backlog()  # anything older is from before this turn
        if self.scheduler is not None:  # check-ins hold off while the user is talking to the assistant
            self.scheduler.state_db.set_sync_meta("last_user_message_at",
                                                  get_current_time(self.day_plan.day_start.tzinfo).isoformat())
        for m in self.brain.messages:  # a message kept from a lost turn is answered by this one
            for key in ("unanswered", "retry_after", "attempts"):
                m.pop(key, None)
        if self.tools.assistant is not None:  # the day's routines and anchors are on before the model looks (R1)
            try:
                self.tools.assistant.routines.set_up_current()
            except Exception as e:
                logger.warning("Setting up the day failed: %s", e)
        before = len(self.brain.messages)  # first: a turn that fails takes the morning instruction back out too
        # --- notes to the engineers: kept for review, never acted on (engineer_notes.py) ---
        from thirties_core import engineer_notes
        kept, rest = engineer_notes.take(self.scheduler.state_db if self.scheduler else None, merged, via)
        engineer_notes.start_turn(self.tools, merged, via, kept)  # what the model's own call and the reply check see
        if kept is not None and engineer_notes.only_a_note(rest):
            return self._only_for_engineers(batch, via, before), True
        if kept is not None:
            self.brain.messages.append({"role": "user", "content": engineer_notes.hidden_instruction(kept),
                                        "hidden": True})
        # --- end notes to the engineers ---
        # --- on-site work today, from the user's words: what's inside work moves out first (on_site.py) ---
        from thirties_core import on_site
        moved_out = on_site.at_turn_start(self.tools, words)
        if moved_out:
            self.brain.messages.append({"role": "user", "content": moved_out, "hidden": True})
        # --- end on-site work ---
        # --- an ask for quiet, from the user's words: kept by Python before the model replies (quiet_asks.py) ---
        from thirties_core import quiet_asks
        quiet = quiet_asks.at_turn_start(self.scheduler.state_db if self.scheduler else None, words,
                                         get_current_time(self.day_plan.day_start.tzinfo), self.day_plan)
        if quiet is not None:
            self.brain.messages.append({"role": "user", "hidden": True, "content": (
                "(The user's message asks for quiet, and Thirties has already set it: " + quiet_asks.describe(quiet)
                + " No call is needed for it.)")})
        # --- end ask for quiet ---
        # --- asked why about an item: where its record is, for the model's one answer (why_notes.py) ---
        from thirties_core import why_notes
        why = why_notes.note(self.tools, words)
        if why:
            self.brain.messages.append({"role": "user", "content": why, "hidden": True})
        # --- end asked why ---
        morning_now = self._morning_in_turn()  # the first message of the day: its reply is the morning (A1)
        changes_before = self.tools.successful_changes
        try:
            if self.brain_name in ("slm", "pipeline"):  # its parse reads one message: give it the burst as one
                reply = self.brain.respond(merged)
            else:  # the model sees each message as the user sent it, and answers them together
                for i in batch[:-1]:
                    self.brain.messages.append({"role": "user", "content": i.text})
                reply = self.brain.respond(batch[-1].text)
        except BrainUnavailable as e:
            logger.warning("The model couldn't be reached: %s", e)
            # --- the usage limit, mid-turn: the on-device brain takes the turn if it can (fallback.py) ---
            from thirties_core import fallback
            if self.tools.successful_changes == changes_before and fallback.after_lost(self, e):
                return self._run_turn(batch, via, attempts)
            # --- end usage limit ---
            return self._lost(batch, before, changes_before, e, via, attempts), False
        if morning_now and getattr(self, "_morning_facts", "") and isinstance(reply, str) \
                and not reply.startswith(("Error contacting model", "Sorry, I couldn't reach")):
            reply = f"{reply}\n\n{self._morning_facts}"  # the pipeline's morning: what the day holds, from Python
        if morning_now:  # it went out with this reply: the background doesn't send a second one
            from thirties_core import morning
            morning.mark_done(self.scheduler.state_db, self.day_plan.target_date,
                              get_current_time(self.day_plan.day_start.tzinfo), "in_conversation")
        self._stamp_arrivals(batch, before)

        self.last_raw_reply = reply  # before the AM/PM pass (for tests of the brains themselves)
        final = reply
        if self.tools.shown_schedule:
            from thirties_core.messages import place_schedule
            final = place_schedule(final, self.tools.shown_schedule)  # Python's layout, whatever the model made
            from thirties_core import one_schedule  # and no listing of the model's own beside it
            final = one_schedule.around(final, self.tools.shown_schedule, self.day_plan)
        said = [without_note(str(m.get("content") or "")) for m in self.messages if m.get("role") == "user"  # all day
                and not m.get("hidden") and not str(m.get("content") or "").startswith("[System check")]  # not Python's
        keep = real_times(*said, words, spans=self.day_plan.spans)  # real item times and the user's
        now_times = real_times(now=get_current_time(self.day_plan.day_start.tzinfo))  # said as itself, never a row's
        from thirties_core.under_way import unsaid  # when an item under way began: by its block (under_way.py)
        keep, now_times = unsaid(keep, now_times, self.day_plan.spans, words)
        # else blocks; every time keeps its AM/PM (a schedule with some times trimmed and some not was hard to read)
        final = final if EXACT_ASKED_RE.search(words) else to_thirties(final, keep, now=now_times)
        final = self._no_repeat_checkins(final)
        final = self.tools.shorten(final)  # never a whole raw item line in a reply (11-07)
        final = self._status_as_wanted(final, words, before)
        from thirties_core.extras import after_reply  # a suggestion says where it's from (extras.py)
        final = after_reply(self.tools, final)
        from thirties_core import others_events  # what's on other people's calendars, said once (others_events.py)
        final = others_events.after_reply(self.tools, final)
        notes = self._user_notes()
        if notes:  # Python's own lines for the user (a reminder to export a finished book), once, under the reply
            from thirties_core.messages import join_sections
            final = join_sections(final.rstrip(), "\n".join(notes))
        # --- the morning's reply lists the day itself: no what's-next slice under it (morning_reply.py) ---
        from thirties_core import morning_reply
        final = morning_reply.without_slice(self, final) if morning_now else \
            self._with_whats_next(final, words, before)
        # --- end morning reply ---
        # --- a promise of quiet only when quiet is kept (quiet_asks.py) ---
        if self.scheduler is not None:
            from thirties_core.memory import CheckinMutes
            now_q = get_current_time(self.day_plan.day_start.tzinfo)
            final = quiet_asks.without_false_quiet(
                final, self.scheduler.state_db, now_q,
                mutes_on=CheckinMutes(self.scheduler.state_db).paused_until(now_q) is not None)
        # --- end promise of quiet ---
        # --- a note said to be kept for the engineers only when one was (engineer_notes.py) ---
        final = engineer_notes.without_false_kept(self.tools, final)
        # --- end note said to be kept ---
        from thirties_core.messages import clean_message  # every reply: no done rows, names not ids, plain lines
        final = clean_message(final, self.tools, words)
        from thirties_core import repeat_lines  # an item's time said once a day, and never after "stop" (repeat_lines.py)
        final = repeat_lines.after_reply(self, final, words, before)
        from thirties_core import fallback  # said once: a simpler assistant is answering, or the usual one is back
        final = fallback.with_notice(self, final)
        if final != reply:
            for m in reversed(self.brain.messages):
                if m.get("role") == "assistant":
                    if m.get("content") == reply:
                        m["content"] = final
                    break
        self._stamp(via)
        self._save()
        mirror_turn(batch, final, via)
        return final, True

    def _only_for_engineers(self, batch: List[Incoming], via: str, before: int) -> str:
        """A message that's only a note to the engineers: kept, acknowledged, and never shown to the model as
        something to act on (engineer_notes.py)."""
        from thirties_core.engineer_notes import ACK
        for i in batch:
            self.brain.messages.append({"role": "user", "content": i.text})
        self.brain.messages.append({"role": "assistant", "content": ACK})
        self._stamp_arrivals(batch, before)
        self._stamp(via)
        self._save()
        mirror_turn(batch, ACK, via)
        return ACK

    def _stamp_arrivals(self, batch: List[Incoming], before: int) -> None:
        """Each user message is stamped when it arrived, not when the reply was saved (soak friction 2)."""
        users = [m for m in self.brain.messages[before:] if m.get("role") == "user" and "at" not in m
                 and not str(m.get("content", "")).startswith("[System check")]
        from thirties_core import build
        if self.brain_name in ("slm", "pipeline") and users:
            users[0]["at"], users[0]["via"], users[0]["build"] = batch[0].at, batch[0].via, build.BUILD
            return
        for item, m in zip(batch, users):
            if item.at:
                m["at"], m["via"], m["build"] = item.at, item.via, build.BUILD

    def _no_repeat_checkins(self, reply: str) -> str:
        """Don't ask again about missed items the user was already shown or asked about (C5), and count what
        this reply shows or asks as shown."""
        if self.scheduler is None or not self.tools.is_current(self.day_plan):
            return reply
        from thirties_core.messages import MISSED_HEADING, MissedTracker, drop_repeat_checkins, missed_for
        now = get_current_time(self.day_plan.day_start.tzinfo)
        if not (self.day_plan.day_start <= now < self.day_plan.day_end):
            return reply
        missed = missed_for(self.tools, now)
        if not missed:
            return reply
        tracker = MissedTracker(self.scheduler.state_db)
        out, asked = drop_repeat_checkins(reply, missed, tracker.titles_shown(now))
        listed = [s for s in missed if MISSED_HEADING in out and f"- {s.title}" in out]
        tracker.mark(now, listed, asked)
        return out

    def _context(self) -> tuple:
        """(config, state db) for what's laid out as the person wants it (reply_extras.py)."""
        if self.scheduler is None:
            return None, None
        return getattr(self.scheduler, "config", None), self.scheduler.state_db

    def _status_as_wanted(self, reply: str, said: str, before: int) -> str:
        """The "You're in the … block" line once per block: left out when the block hasn't changed since a reply
        last said it, unless the person asks where they are. Whether it's wanted at all is messages.status_wanted;
        this also catches the line when the model wrote it itself."""
        from thirties_core import reply_extras
        from thirties_core.messages import WHERE_ASKED_RE, status_wanted
        if not reply_extras.status_shown_in(reply) or WHERE_ASKED_RE.search(said or ""):
            return reply
        if not status_wanted(*self._context(), self.day_plan.target_date, said):
            return reply_extras.without_status(reply)
        now = get_current_time(self.day_plan.day_start.tzinfo)
        last = reply_extras.status_block_last_shown(self.brain.messages[:before])  # the replies before this turn
        if last is not None and last == thirty_of(now):
            return reply_extras.without_status(reply)
        return reply

    def _with_whats_next(self, reply: str, said: str, before: int) -> str:
        """After a turn that changed something, tried to, or talked the plan over (whats_next.follows_turn), what's
        next, from the plan as it now stands (whats_next.py), at the end of the reply, as the person wants it
        (reply_extras.py). Never with a schedule already in the reply, when the user asked for it, or in a turn
        about what replies show."""
        from thirties_core import whats_next
        from thirties_core.plan_tools import NOT_DONE, READ_ONLY_TOOLS
        now = get_current_time(self.day_plan.day_start.tzinfo)
        db = self._context()[1]
        if self.tools.shown_schedule and self.tools.is_current(self.day_plan):
            whats_next.SentSummaries(db).mark(self.day_plan, now, whole_day=True)  # the user has seen the day
        if self.tools.shown_schedule or whats_next.SCHEDULE_ASKED_RE.search(said or "") \
                or any(m.get("name") == "set_reply_extras" for m in self.brain.messages[before:]):
            return reply
        if not whats_next.follows_turn(self.brain.messages[before:], reply, said, self.day_plan, READ_ONLY_TOOLS,
                                       NOT_DONE):
            return reply
        from thirties_core.messages import MISSED_HEADING, MissedTracker, missed_for
        moved = whats_next.moved_since(getattr(self, "_plan_before", {}), self.day_plan)
        # A status view: worked out fresh from the plan for every slice, so what's still unmarked shows again in the
        # next one. "Shown" only keeps the separate notices from listing them again; it never hides them here.
        # never the list twice; muted or not (a mute quiets check-ins, never where things stand)
        unmarked = [] if MISSED_HEADING in reply else missed_for(self.tools, now, muted_too=True)
        part = whats_next.summary(self.tools, now, None, moved, said, unmarked)  # as the person wants it
        if part and self.tools.is_current(self.day_plan):
            whats_next.SentSummaries(db).mark(self.day_plan, now)
        if part and unmarked and MISSED_HEADING in part and self.scheduler is not None:
            MissedTracker(self.scheduler.state_db).mark(now, unmarked)  # every one of them is listed
            from thirties_core.away import LastSentList  # the list as last sent (away.py)
            LastSentList(self.scheduler.state_db).mark(now, unmarked)
        from thirties_core.messages import join_sections
        if part:  # one schedule per reply: Python's, never a listing of the model's own too (one_schedule.py)
            from thirties_core import one_schedule
            reply = one_schedule.tidy(reply, part, self.day_plan)
        return join_sections(reply, part) if part else reply

    def _lost(self, batch: List[Incoming], before: int, changes_before: int, error: BrainUnavailable, via: str,
              attempts: int) -> str:
        """The model couldn't be reached. If something was already changed this turn, say plainly what; otherwise
        keep the user's messages, marked unanswered, so they're answered later (never silently dropped, T1)."""
        if self.tools.successful_changes > changes_before:
            from thirties_core.plan_tools import NOT_DONE, READ_ONLY_TOOLS
            done = [str(m.get("content") or "").splitlines()[0] for m in self.brain.messages[before:]
                    if m.get("role") == "tool" and m.get("name") not in READ_ONLY_TOOLS
                    and str(m.get("content") or "").strip() and not str(m.get("content")).startswith(NOT_DONE)]
            done = [re.sub(r"\s*\[?\bid[= ][0-9a-z:_-]+\]?", "", d).strip() for d in done]
            for m in self.brain.messages[before:]:
                if m.get("role") == "assistant":
                    m["hidden"] = True
            reply = ("The connection to the model dropped partway through, so I couldn't finish my reply. "
                     "This went through:\n" + "\n".join(f"- {d}" for d in done if d))
            self.brain.messages.append({"role": "assistant", "content": reply, "display_only": True})
            self._stamp_arrivals(batch, before)
            self._stamp(via)
            self._save()
            return reply
        del self.brain.messages[before:]
        attempts += 1
        wait = LOST_RETRY_MINUTES[min(attempts - 1, len(LOST_RETRY_MINUTES) - 1)]
        retry_after = (get_current_time(self.day_plan.day_start.tzinfo) + timedelta(minutes=wait)).isoformat()
        note = LOST_TURN_REPLY.format(why=error.why)
        from thirties_core import fallback  # no assistant has quota left: wait for its reset time, and say until when
        retry_after, note = fallback.lost_wait(self, error, retry_after, note)
        for i in batch:
            self.brain.messages.append({"role": "user", "content": i.text, "at": i.at, "via": i.via,
                                        "unanswered": True, "attempts": attempts, "retry_after": retry_after})
        if attempts == 1:  # said once; a retry that fails again stays quiet
            self.brain.messages.append({"role": "assistant", "content": note, "display_only": True,
                                        "kind": "status"})
        self._stamp(via)
        self._save()
        return note

    def unanswered(self) -> List[Dict[str, Any]]:
        """The user's messages at the end of the conversation that a lost turn kept for later."""
        out = []
        for m in reversed(self.brain.messages[1:]):
            if m.get("role") == "assistant" and m.get("kind") == "status":
                continue
            if m.get("role") == "user" and m.get("unanswered"):
                out.append(m)
                continue
            break
        return list(reversed(out))

    def answer_unanswered(self, now: Optional[datetime] = None) -> Optional[str]:
        """Try a lost turn again once its wait is over (2, 5, 10, 20, then every 30 minutes; not after six hours).
        Returns the reply when it got through, for the caller to deliver; None otherwise."""
        with self.turn_lock:
            self._catch_up()
            waiting = self.unanswered()
            if not waiting:
                return None
            now = now or get_current_time(self.day_plan.day_start.tzinfo)
            first = waiting[0]
            try:
                if first.get("retry_after") and now < datetime.fromisoformat(first["retry_after"]):
                    return None
                if first.get("at") and now - datetime.fromisoformat(first["at"]) > timedelta(hours=LOST_GIVE_UP_HOURS):
                    for m in waiting:  # too old to answer now; it stays in the conversation as said
                        for key in ("unanswered", "retry_after"):
                            m.pop(key, None)
                    self._save()
                    return None
            except (TypeError, ValueError):
                pass
            attempts = max(int(m.get("attempts") or 1) for m in waiting)
            start = self.brain.messages.index(first)
            kept = [m for m in self.brain.messages[start:] if m.get("kind") == "status"]
            del self.brain.messages[start:]
            batch = [Incoming(m["content"], m.get("via", "app"), m.get("at", "")) for m in waiting]
            reply, answered = self._turn(batch, "retry", attempts)
            if kept:  # "I've kept your message" stays where it was said: right after the user's messages
                last = max(i for i, m in enumerate(self.brain.messages) if m.get("role") == "user"
                           and not str(m.get("content", "")).startswith("[System check"))
                self.brain.messages[last + 1:last + 1] = kept
                self._save()
            return reply if answered else None
