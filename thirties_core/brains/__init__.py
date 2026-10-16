"""Brains: the model-specific layer that turns conversation into tool calls.

A brain owns its prompts, its model-calling strategy and its transcript. It
never touches the DayPlan directly; every change goes through the shared
``PlanTools`` API, which owns all schedule math and invariants.

- ``FrontierBrain``: one rich prompt for a large-context model (Gemini today).
- An SLM brain for on-device Gemma will live alongside it, built as a
  heavily scaffolded pipeline rather than a smaller copy of the frontier prompt.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from thirties_core.brains.frontier import FrontierBrain


class Brain(Protocol):
    """Interface every brain implements so clients can swap them freely."""

    messages: List[Dict[str, Any]]

    def respond(self, user_text: str) -> str:
        """Handle one user turn and return the reply shown to the user."""
        ...

    def reset(self) -> None:
        """Discard the transcript and start a fresh conversation."""
        ...


BRAINS = {"frontier": "Gemini (cloud)", "slm": "Gemma (on this device)"}
BRAIN_ALIASES = {"frontier": "frontier", "gemini": "frontier", "cloud": "frontier",
                 "slm": "slm", "gemma": "slm", "local": "slm", "device": "slm", "on-device": "slm"}


def brain_choice(config, state_db=None) -> str:
    """The brain to use: this device's choice from the app (if any), else ``[assistant] brain`` in config."""
    if state_db is not None:
        from thirties_core.settings import Settings
        chosen = Settings(state_db).get("assistant.brain")
        if chosen in BRAINS:
            return chosen
    configured = (getattr(getattr(config, "assistant", None), "brain", "frontier") or "frontier").lower()
    return BRAIN_ALIASES.get(configured, "frontier")


def make_brain(config, tools, inference_engine, tasks=None, active_notes=None, choice=None) -> Brain:
    """A brain: ``choice`` ("frontier" or "slm"), defaulting to ``[assistant] brain`` in config."""
    if choice is None:
        choice = brain_choice(config)
    if choice == "slm":
        from thirties_core.brains.slm import SlmBrain
        return SlmBrain(tools, tasks=tasks, config=config)
    return FrontierBrain(tools, inference_engine, tasks=tasks, active_notes=active_notes)


__all__ = ["BRAINS", "BRAIN_ALIASES", "Brain", "FrontierBrain", "brain_choice", "make_brain"]
