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


__all__ = ["Brain", "FrontierBrain"]
