"""Agent mode inside a conversation turn: what ConversationManager calls while the mode is on.

A turn in Agent mode is the person's messages, Gemini's reply, and whatever it changed through its tools.
None of the usual turn's work runs: no hidden instructions, no checks on the reply. The messages are stored in the
day's conversation like any other turn, so the app and Telegram show the same thing.

When the guard says the quota is low, or Gemini can't be reached, the usual assistant takes the turn, and the
reply says so in one line, once until Gemini answers again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# What the user reads (tests/test_user_text.py scans these)
QUOTA_LINE = "Agent mode is paused while the Gemini quota is low, so the usual assistant answered."
FAILED_LINE = "Gemini couldn't answer this time, so the usual assistant did."
CHANGED_LINE = "Gemini stopped partway through after changing something. Ask again to see where things stand."
EMPTY_REPLY = "Done."
NOTICE_KEY = "_agent_notice"


def _session(manager: Any):
    from thirties_core.agent_mode.session import DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_TIMEOUT, AgentSession
    config = getattr(manager.scheduler, "config", None) if manager.scheduler is not None else None
    assistant = getattr(config, "assistant", None)
    inference = getattr(config, "inference", None)
    return AgentSession(model=getattr(assistant, "agent_model", "") or DEFAULT_MODEL,
                        effort=getattr(assistant, "agent_effort", DEFAULT_EFFORT),
                        timeout=float(getattr(assistant, "agent_timeout_seconds", 0) or DEFAULT_TIMEOUT),
                        agy_path=getattr(inference, "agy_path", None))


def tools_for(manager: Any, workspace: Path):
    from thirties_core.agent_mode.tools import AgentTools, Journal
    scheduler = manager.scheduler
    joplin = manager.joplin_engine

    def writer():
        api = getattr(joplin, "api_client", None) if joplin is not None else None
        if scheduler is None or api is None:
            return None
        from thirties_core.joplin_writeback import JoplinWriter
        return JoplinWriter(scheduler.config, scheduler.state_db, api)

    return AgentTools(scheduler, joplin, writer, Journal(workspace / "journal.jsonl"),
                      day=lambda: manager.day_plan.target_date)


# --- "said once" ---

def _state_path(ws: Path) -> Path:
    return ws / "mode.json"


def _told(ws: Path) -> bool:
    try:
        return bool(json.loads(_state_path(ws).read_text()).get("fallback_told"))
    except (OSError, ValueError, AttributeError):
        return False


def _set_told(ws: Path, told: bool) -> None:
    from thirties_core.agent_mode.session import _write_if_changed
    _write_if_changed(_state_path(ws), json.dumps({"fallback_told": told}) + "\n")


def _hand_over(manager: Any, ws: Path, line: str, why: str) -> None:
    logger.info("Agent mode: the usual assistant takes this turn (%s).", why)
    if not _told(ws):
        setattr(manager, NOTICE_KEY, line)
        _set_told(ws, True)


# --- the turn ---

def take_turn(manager: Any, batch: list, via: str) -> Optional[tuple]:
    """(reply, True) when Gemini answered; None when the usual assistant takes the turn."""
    from thirties_core.agent_mode import guard
    from thirties_core.agent_mode.bridge import ToolBridge
    from thirties_core.agent_mode.session import agy_prefix
    setattr(manager, NOTICE_KEY, "")
    session = _session(manager)
    ws = session.ws
    agy_bin = session.agy_binary()
    verdict = guard.check(five_hour=guard.reading_from(agy_prefix(agy_bin, ws) if agy_bin else None, ws))
    if not verdict.go:
        _hand_over(manager, ws, QUOTA_LINE, verdict.why)
        return None
    manager._catch_up()
    before = len(manager.brain.messages)
    for item in batch:
        manager.brain.messages.append({"role": "user", "content": item.text})
    tools = tools_for(manager, ws)
    merged = "\n".join(i.text for i in batch)
    try:
        with ToolBridge(session.socket_path, tools.call):
            result = session.run(merged, manager.day_plan.target_date)
    except Exception as e:
        logger.exception("The Agent mode turn failed")
        from thirties_core.agent_mode.session import TurnResult
        result = TurnResult(False, error=str(e))
    if tools.writes:
        _refresh(manager)
    if not result.ok:
        del manager.brain.messages[before:]
        if tools.writes:  # it changed things and then failed: say so, never hand the same words to another brain
            for item in batch:
                manager.brain.messages.append({"role": "user", "content": item.text})
            return _store(manager, batch, before, CHANGED_LINE, via), True
        _hand_over(manager, ws, QUOTA_LINE if result.quota else FAILED_LINE, result.error[:200])
        return None
    _set_told(ws, False)
    return _store(manager, batch, before, result.reply or EMPTY_REPLY, via), True


def _store(manager: Any, batch: list, before: int, reply: str, via: str) -> str:
    from thirties_core.turns import mirror_turn
    manager.brain.messages.append({"role": "assistant", "content": reply, "kind": "gemini_run"})
    manager._stamp_arrivals(batch, before)
    manager._stamp(via)
    manager._save()
    mirror_turn(batch, reply, via)
    return reply


def _refresh(manager: Any) -> None:
    try:
        manager.reload_plan()
        manager.reload_backlog()
    except Exception as e:
        logger.warning("Refreshing after Gemini's changes failed: %s", e)


def with_notice(manager: Any, reply: str) -> str:
    """The usual assistant's reply to a turn Agent mode handed over: the one line first, also in the stored copy."""
    line = getattr(manager, NOTICE_KEY, "") or ""
    if not line or not isinstance(reply, str):
        return reply
    setattr(manager, NOTICE_KEY, "")
    final = f"{line}\n\n{reply}" if reply.strip() else line
    for m in reversed(manager.brain.messages):
        if m.get("role") == "assistant":
            if m.get("content") == reply:
                m["content"] = final
                manager._save()
            break
    return final


def undo_last(manager: Any) -> str:
    """The undo command: Gemini's last write taken back."""
    from thirties_core.agent_mode.session import agent_dir
    tools = tools_for(manager, agent_dir())
    ok, said = tools.undo_last()
    if ok:
        _refresh(manager)
    return said
