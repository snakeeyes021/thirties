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

from thirties_core.astronomy import set_debug_time
from thirties_core.assistant_tools import Services
from thirties_core.brains import Brain, make_brain
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
        self.brain: Brain = brain or make_brain(
            scheduler.config if scheduler else None,
            self.tools,
            inference_engine or MockInferenceEngine(),
            tasks=tasks,
            active_notes=active_notes,
        )
        self.conversation: Optional[Conversation] = None
        if services:
            conv = services.conversations.get(conversation_id) if conversation_id else None
            self._load(conv or services.conversations.latest_for_day(day_plan.target_date))

    # --- conversation persistence ---

    @property
    def _store(self):
        return self.tools.services.conversations if self.tools.services else None

    def _load(self, conv: Optional[Conversation]) -> None:
        self.conversation = conv
        self.brain.reset()
        if conv:
            self.brain.messages = [self.brain.messages[0]] + list(conv.messages)

    def _save(self) -> None:
        if self._store is None:
            return
        if self.conversation is None:
            self.conversation = self._store.create(self.day_plan.target_date)
        self.conversation.messages = [m for m in self.brain.messages if m.get("role") != "system"]
        self._store.save(self.conversation)

    def history(self) -> List[Dict[str, str]]:
        """The visible transcript of the current conversation: user and assistant text only."""
        return [{"role": m["role"], "content": m.get("content", "")}
                for m in self.brain.messages
                if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
                and not str(m.get("content", "")).startswith("[System check")]

    def conversations_for_day(self) -> List[Conversation]:
        return self._store.for_day(self.day_plan.target_date) if self._store else []

    def new_conversation(self) -> None:
        """Start a fresh conversation for this day (it becomes the current one)."""
        if self.conversation is not None and not self.conversation.messages:
            return  # the current one is already empty
        self._load(self._store.create(self.day_plan.target_date) if self._store else None)

    def switch_conversation(self, conversation_id: str) -> bool:
        conv = self._store.get(conversation_id) if self._store else None
        if conv is None:
            return False
        self._load(conv)
        return True

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
            elif cmd.startswith("/time"):
                val = clean_text[5:].strip()
                set_debug_time(val)
                reply = f"Simulated time set to {val}."
            else:
                reply = f"Unknown command: {clean_text}. Available: /night, /midnight, /day, /midday, /noon, /sunrise, /reset, /time HH:MM"

            self.messages.append({"role": "user", "content": user_text})
            self.messages.append({"role": "assistant", "content": reply})
            return reply


        return self.brain.respond(user_text)
