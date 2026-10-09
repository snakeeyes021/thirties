"""On-device inference engine using Google LiteRT-LM.

Runs quantized Gemma models (.litertlm) directly on-device with GPU acceleration
and automatic CPU fallback. Strictly self-contained without external daemons.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from thirties_core.config import InferenceConfig, ThirtiesConfig

logger = logging.getLogger(__name__)


class InferenceEngine(Protocol):
    """Protocol for LLM inference backends."""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Perform a conversational chat turn with optional tool schemas.
        
        Returns a dict matching:
        {
            "role": "assistant",
            "content": str,
            "tool_calls": List[{"id": str, "name": str, "arguments": Dict[str, Any]}]
        }
        """
        ...


class LiteRTInferenceEngine:
    """LiteRT-LM on-device inference engine."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.inf_cfg: InferenceConfig = self.config.inference
        self.model_path = Path(os.path.expanduser(self.inf_cfg.litert_model_path))
        self.temperature = self.inf_cfg.temperature
        self._engine_instance: Any = None
        self._is_initialized = False

    def is_available(self) -> bool:
        """Return True if model bundle exists and LiteRT-LM can be loaded."""
        if not self.model_path.is_file():
            return False
        try:
            import litert_lm_api  # noqa: F401
            return True
        except ImportError:
            try:
                import litert_lm  # noqa: F401
                return True
            except ImportError:
                return False

    def _ensure_initialized(self) -> None:
        if self._is_initialized and self._engine_instance is not None:
            return

        if not self.model_path.is_file():
            raise FileNotFoundError(
                f"LiteRT-LM model bundle not found at {self.model_path}. "
                "Download a Gemma-4 .litertlm model into ~/.local/share/thirties/models/"
            )

        # Attempt to load litert_lm_api with GPU acceleration then CPU fallback
        litert_mod = None
        try:
            import litert_lm_api as litert_mod
        except ImportError:
            try:
                import litert_lm as litert_mod
            except ImportError:
                raise RuntimeError(
                    "Google LiteRT-LM package (litert-lm-api) is not installed in the environment."
                )

        logger.info("Initializing LiteRT-LM from %s", self.model_path)
        try:
            # Try GPU acceleration first
            self._engine_instance = litert_mod.Engine(
                str(self.model_path),
                backend="gpu",
                temperature=self.temperature,
            )
            logger.info("LiteRT-LM initialized successfully with GPU delegate.")
        except Exception as gpu_err:
            logger.warning("GPU acceleration unavailable (%s); falling back to CPU.", gpu_err)
            self._engine_instance = litert_mod.Engine(
                str(self.model_path),
                backend="cpu",
                temperature=self.temperature,
            )
            logger.info("LiteRT-LM initialized with CPU fallback.")

        self._is_initialized = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute chat turn against local LiteRT-LM model."""
        self._ensure_initialized()

        # Call engine chat
        if hasattr(self._engine_instance, "chat"):
            return self._engine_instance.chat(messages=messages, tools=tools)

        # Fallback raw invocation if engine only exposes generate
        prompt_lines = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            prompt_lines.append(f"<start_of_turn>{role}\n{content}<end_of_turn>")
        prompt_lines.append("<start_of_turn>model\n")
        raw_prompt = "\n".join(prompt_lines)

        raw_output = self._engine_instance.generate(raw_prompt)
        return {
            "role": "assistant",
            "content": raw_output.strip(),
            "tool_calls": [],
        }


class MockInferenceEngine:
    """Mock inference engine for deterministic testing and offline dialog simulation."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self.responses: List[Dict[str, Any]] = list(responses) if responses else []
        self.call_history: List[List[Dict[str, Any]]] = []

    def set_next_response(self, response: Dict[str, Any]) -> None:
        self.responses.append(response)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        self.call_history.append(messages)

        if self.responses:
            return self.responses.pop(0)

        # Default intelligent fallback based on last message
        last_msg = messages[-1]["content"].lower() if messages else ""

        if "dorico" in last_msg and "32" in last_msg:
            return {
                "role": "assistant",
                "content": "I allocated block 32 to Dorico Compose.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "allocate_thirty_block",
                        "arguments": {
                            "block_index": 32,
                            "custom_label": "Dorico Compose",
                        },
                    }
                ],
            }

        if "attending" in last_msg or "yes" in last_msg:
            return {
                "role": "assistant",
                "content": "Marked doctor appointment as attending.",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "name": "resolve_calendar_event",
                        "arguments": {
                            "event_id": "e_doc",
                            "attending": True,
                        },
                    }
                ],
            }

        return {
            "role": "assistant",
            "content": "I'm ready to help you plan your Thirties today.",
            "tool_calls": [],
        }
