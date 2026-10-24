"""Client-facing entry point for conversational planning.

``ConversationManager`` is what the GTK app, the Telegram bridge and the eval
harnesses talk to. It wires the shared ``PlanTools`` (schedule state and
invariants) to a brain (model-specific prompting and turn logic), and handles
developer slash commands that never reach a model.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

from thirties_core.clock import EXACT_ASKED_RE, says_minutes, to_thirties, trim_meridiems
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

logger = logging.getLogger(__name__)


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
        self.conversation: Optional[Conversation] = None
        self._followed_latest: Optional[str] = None
        if services:
            conv = services.conversations.get(conversation_id) if conversation_id else None
            self._load(conv or services.conversations.latest_for_day(day_plan.target_date))

    # --- choosing the brain ---

    def set_brain(self, choice: str) -> str:
        """Switch this device to another brain, keeping the conversation. Returns the brain's name."""
        name = BRAIN_ALIASES.get(str(choice).strip().lower())
        if name is None:
            raise ValueError(f"Unknown brain '{choice}'. Choose one of: {', '.join(BRAINS)}.")
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
        self.conversation.messages = [m for m in self.brain.messages if m.get("role") != "system"]
        self._store.save(self.conversation)
        self._note_if_latest()

    def history(self) -> List[Dict[str, str]]:
        """The visible transcript of the current conversation: user and assistant text only."""
        return [{"role": m["role"], "content": m.get("content", "")}
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

    def refresh(self) -> None:
        """Reload the day and the current conversation from storage.

        Another client (the desktop app or the Telegram bridge) may have changed the
        schedule or continued the conversation since this one last looked.
        """
        if self.scheduler is None:
            return
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

    def add_assistant_message(self, text: str) -> None:
        """Record something the assistant said on its own (e.g. a Telegram check-in)."""
        self.brain.messages.append({"role": "assistant", "content": text})
        self._save()

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
        """Run a schedule tool directly, bypassing the model."""
        return self.tools.execute(name, arguments)

    def send_user_message(self, user_text: str) -> str:
        """Handle one user turn: developer slash commands here, everything else via the brain."""
        if self.scheduler is not None:  # check-ins hold off while the user is talking to the assistant
            self.scheduler.state_db.set_sync_meta("last_user_message_at",
                                                  get_current_time(self.day_plan.day_start.tzinfo).isoformat())
        # Developer / Runtime Simulation Slash Commands
        clean_text = user_text.strip()
        if clean_text.startswith("/"):
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
                    reply = (f"Using {BRAINS[self.brain_name]}. Switch with /brain gemini or /brain gemma.")
                else:
                    try:
                        reply = f"Now using {BRAINS[self.set_brain(arg)]}. This conversation continues."
                    except ValueError as e:
                        reply = str(e)
            elif cmd.startswith("/time"):
                val = clean_text[5:].strip()
                set_debug_time(val)
                reply = f"Simulated time set to {val}."
            else:
                reply = f"Unknown command: {clean_text}. Available: /brain, /night, /midnight, /day, /midday, /noon, /sunrise, /reset, /time HH:MM"

            self.messages.append({"role": "user", "content": user_text})
            self.messages.append({"role": "assistant", "content": reply})
            self._save()
            return reply


        reply = self.brain.respond(user_text)
        self.last_raw_reply = reply  # before the AM/PM pass (for tests of the brains themselves)
        minutes_asked = EXACT_ASKED_RE.search(user_text) or says_minutes(user_text)  # "exactly?", or "12:15, not 12"
        shown = reply if minutes_asked else to_thirties(reply)  # :00 and :30 unless asked
        trimmed = trim_meridiems(shown, get_current_time(self.day_plan.day_start.tzinfo)
                                 if self.day_plan.target_date == self.tools.today() else None)
        if trimmed != reply:
            for m in reversed(self.brain.messages):
                if m.get("role") == "assistant":
                    if m.get("content") == reply:
                        m["content"] = trimmed
                    break
        self._save()
        return trimmed
