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


BRAINS = {"slm": "Gemma (on this device)", "frontier": "Gemini (cloud, for developers)",
          "pipeline": "Gemini on the shared pipeline (being tested)"}
BRAIN_ALIASES = {"frontier": "frontier", "gemini": "frontier", "cloud": "frontier",
                 "slm": "slm", "gemma": "slm", "local": "slm", "device": "slm", "on-device": "slm",
                 "pipeline": "pipeline", "gemini-pipeline": "pipeline"}


GEMMA_FLAG_ENV = "THIRTIES_OFFER_GEMMA"


def gemma_offered(config=None) -> bool:
    """Whether the on-device model may be offered or chosen at all: a developer switch (``[assistant]
    offer_gemma``, or THIRTIES_OFFER_GEMMA=1) until a study shows it's no worse than the cloud brain. Everything
    behind it stays built and tested; it's only never shown or picked without it."""
    import os
    if os.environ.get(GEMMA_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(getattr(getattr(config, "assistant", None), "offer_gemma", False))


def offered_brains(config=None) -> Dict[str, str]:
    """The brains a user can pick on this device."""
    return {k: v for k, v in BRAINS.items() if k != "slm" or gemma_offered(config)}


def brain_choice(config, state_db=None) -> str:
    """The brain to use: this device's choice from the app (if any), else ``[assistant] brain`` in config. Gemma
    only while it's offered (gemma_offered); a device that chose it earlier uses the cloud brain meanwhile."""
    offered = offered_brains(config)
    if state_db is not None:
        from thirties_core.settings import Settings
        chosen = Settings(state_db).get("assistant.brain")
        if chosen in offered:
            return chosen
    configured = (getattr(getattr(config, "assistant", None), "brain", "frontier") or "frontier").lower()
    name = BRAIN_ALIASES.get(configured, "frontier")
    return name if name in offered else "frontier"


def brain_unavailable_reason(name: str, config) -> "str | None":
    """Why a brain can't be chosen on this device, or None if it can."""
    if name == "slm":
        from thirties_core.brains.slm.runtime import slm_unavailable_reason
        return slm_unavailable_reason(config)  # the pipeline brain needs Gemini, like the frontier one, below
    from thirties_core.inference import GeminiSourcesEngine
    if not GeminiSourcesEngine(config).is_available():
        return "no Gemini source is set up: add an API key, or install the Antigravity CLI as `agy` and sign in."
    return None


def make_brain(config, tools, inference_engine, tasks=None, active_notes=None, choice=None) -> Brain:
    """A brain: ``choice`` ("frontier" or "slm"), defaulting to ``[assistant] brain`` in config."""
    if choice is None:
        choice = brain_choice(config)
    if choice == "slm":
        from thirties_core.brains.slm import SlmBrain
        return SlmBrain(tools, tasks=tasks, config=config)
    if choice == "pipeline":  # the same pipeline as Gemma's, with Gemini making the small decisions
        from thirties_core.brains.slm import SlmBrain
        from thirties_core.brains.slm.cloud_runtime import CloudRuntime
        return SlmBrain(tools, tasks=tasks, config=config, runtime=CloudRuntime(inference_engine))
    return FrontierBrain(tools, inference_engine, tasks=tasks, active_notes=active_notes)


__all__ = ["BRAINS", "BRAIN_ALIASES", "Brain", "FrontierBrain", "brain_choice", "brain_unavailable_reason", "make_brain",
           "gemma_offered", "offered_brains", "GEMMA_FLAG_ENV"]
