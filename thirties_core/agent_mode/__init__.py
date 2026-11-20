"""Agent mode: a developer switch that hands the conversation to Gemini, running on its own.

While it's on, the window and the Telegram bridge are only a front-end: each user turn goes to one agy Gemini run
(session.py), not to Thirties' own assistant (no prompt rules, no turn guards, no reply checks). Gemini acts on
the lists and the day plan through plain tools (tools.py), reached over a small MCP server that agy starts
(tools_server.py), and keeps its own notes in its workspace (session.agent_dir). Every write it makes is
journaled, and the undo command (UNDO_COMMAND) takes the last one back. Before each turn the quota guard (guard.py) may hand the
turn to the usual assistant instead.

Off by default. On with ``[assistant] agent_mode = true``, the switch in Developer options (a device setting), or
THIRTIES_AGENT_MODE=1. Turning it off returns to the usual assistant; what Gemini left stays as it is.
"""

from __future__ import annotations

import os
from typing import Any, Optional

AGENT_MODE_ENV = "THIRTIES_AGENT_MODE"
SETTING = "assistant.agent_mode"
CALLER = "agent-mode"  # the agy ledger's caller column for every call Agent mode makes
UNDO_COMMAND = "/agent-undo"  # takes Gemini's last change back (the app's chat and Telegram)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def env_on() -> bool:
    return _truthy(os.environ.get(AGENT_MODE_ENV, ""))


_CHOICE_TTL = 3.0  # seconds a device switch reading is reused: every message asks, and a database read each time
_choices: dict = {}  # would slow messages that arrive together enough to change the order they're taken in


def _device_choice(state_db: Any) -> Any:
    """This device's switch from Developer options (None when never set), read at most every few seconds."""
    import time
    key = id(state_db)
    hit = _choices.get(key)
    if hit and hit[0] is state_db and time.monotonic() - hit[1] < _CHOICE_TTL:
        return hit[2]
    try:
        from thirties_core.settings import Settings
        chosen = Settings(state_db).get(SETTING)
    except Exception:
        chosen = None
    _choices[key] = (state_db, time.monotonic(), chosen)
    return chosen


def forget_choice() -> None:
    """Drop the cached switch reading (the switch was just changed)."""
    _choices.clear()


def is_on(config: Any = None, state_db: Any = None) -> bool:
    """Whether turns go to Gemini: the env var, else this device's switch (Developer options), else config."""
    if env_on():
        return True
    if state_db is not None:
        chosen = _device_choice(state_db)
        if chosen is not None:
            return _truthy(chosen)
    assistant = getattr(config, "assistant", None)
    return _truthy(assistant.agent_mode) if hasattr(assistant, SETTING.split(".")[1]) else False


def offered(config: Any = None, state_db: Any = None) -> bool:
    """Whether Developer options shows the switch: only with the developer switch on (the one that offers the
    on-device model), or while Agent mode is on, so it can be turned off where it was turned on."""
    from thirties_core.brains import gemma_offered
    return gemma_offered(config) or is_on(config, state_db)


def for_manager(manager: Any) -> bool:
    """is_on for a ConversationManager (its scheduler's config and state database)."""
    scheduler = getattr(manager, "scheduler", None)
    if scheduler is None:
        return env_on()
    return is_on(getattr(scheduler, "config", None), getattr(scheduler, "state_db", None))


def take_turn(manager: Any, batch: list, via: str) -> "Optional[tuple]":
    """Run one turn in Agent mode (route.py). None when the turn belongs to the usual assistant."""
    from thirties_core.agent_mode.route import take_turn as run
    return run(manager, batch, via)


def undo_last(manager: Any) -> str:
    from thirties_core.agent_mode.route import undo_last as run
    return run(manager)
