"""On-device inference engine using Google LiteRT-LM.

Runs quantized Gemma models (.litertlm) directly on-device with GPU acceleration
and automatic CPU fallback. Strictly self-contained without external daemons.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

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
    """LiteRT-LM on-device inference engine leveraging local GPU acceleration."""

    def __init__(self, config: Optional[ThirtiesConfig] = None) -> None:
        self.config = config or ThirtiesConfig()
        self.inf_cfg: InferenceConfig = self.config.inference
        self.temperature = self.inf_cfg.temperature
        self._engine_instance: Any = None
        self._conv_session: Any = None
        self._is_initialized = False

    def _find_model_path(self) -> Optional[Path]:
        """Check standard paths for available Gemma .litertlm models."""
        models_dir = Path.home() / ".local" / "share" / "thirties" / "models"
        candidates = [
            Path(os.path.expanduser(self.inf_cfg.litert_model_path)),
            models_dir / "gemma-4-e4b.litertlm",
            models_dir / "gemma-4-E4B-it-gpu.litertlm",
            models_dir / "gemma-4-e2b.litertlm",
            models_dir / "gemma-4-E4B-it.litertlm",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def is_available(self) -> bool:
        """Return True if a model bundle exists and LiteRT-LM is importable."""
        if not self._find_model_path():
            return False
        try:
            import litert_lm  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_initialized(self) -> None:
        if self._is_initialized and self._engine_instance is not None:
            return

        model_path = self._find_model_path()
        if not model_path:
            raise FileNotFoundError(
                "LiteRT-LM model bundle not found. Download a Gemma model into ~/.local/share/thirties/models/"
            )

        import litert_lm

        logger.info("Initializing LiteRT-LM from %s", model_path)
        try:
            # Try GPU acceleration first (e.g. Vulkan / NVIDIA RTX / AMD / Intel)
            self._engine_instance = litert_lm.Engine(
                str(model_path),
                backend=litert_lm.Backend.GPU(),
            )
            logger.info("LiteRT-LM initialized successfully on GPU.")
        except Exception as gpu_err:
            logger.warning("GPU acceleration unavailable (%s); falling back to CPU.", gpu_err)
            self._engine_instance = litert_lm.Engine(
                str(model_path),
                backend=litert_lm.Backend.CPU(),
            )
            logger.info("LiteRT-LM initialized on CPU.")

        self._conv_session = self._engine_instance.create_conversation()
        self._is_initialized = True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute chat turn against local LiteRT-LM model."""
        self._ensure_initialized()

        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break

        if not last_user_msg:
            return {"role": "assistant", "content": "How can I help you plan your Thirties today?", "tool_calls": []}

        # Send to conversational session
        try:
            raw_reply = self._conv_session.send_message(last_user_msg)
        except Exception as e:
            logger.error("Inference generation error: %s", e)
            return {"role": "assistant", "content": f"Inference notice: {e}", "tool_calls": []}

        # Parse potential intent/tool calls from user prompt or model text
        tool_calls: List[Dict[str, Any]] = []

        # Intent: allocate block index (e.g. "allocate 32 to Dorico" or "put Dorico at 32")
        alloc_match = re.search(r"(?:allocate|put|assign|schedule|set)\s+(?:block\s+)?(\d{1,2})\s+(?:to|for|with)\s+(.+)", last_user_msg, re.IGNORECASE)
        if not alloc_match:
            alloc_match = re.search(r"(?:allocate|put|assign|schedule|set)\s+(.+?)\s+(?:to|at|in|for)\s+(?:block\s+)?(\d{1,2})", last_user_msg, re.IGNORECASE)
            if alloc_match:
                label_text = alloc_match.group(1).strip()
                block_idx = int(alloc_match.group(2))
                tool_calls.append({
                    "id": "alloc_call",
                    "name": "allocate_thirty_block",
                    "arguments": {"block_index": block_idx, "custom_label": label_text},
                })
        else:
            block_idx = int(alloc_match.group(1))
            label_text = alloc_match.group(2).strip()
            tool_calls.append({
                "id": "alloc_call",
                "name": "allocate_thirty_block",
                "arguments": {"block_index": block_idx, "custom_label": label_text},
            })

        # Intent: confirm attendance
        if re.search(r"\b(yes|attending|confirm)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "attend_call",
                "name": "resolve_calendar_event",
                "arguments": {"event_id": "unconfirmed", "attending": True},
            })
        elif re.search(r"\b(decline|skip|no|not attending)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "decline_call",
                "name": "resolve_calendar_event",
                "arguments": {"event_id": "unconfirmed", "attending": False},
            })

        # Intent: finalize plan
        if re.search(r"\b(finalize|lock in|looks good|done)\b", last_user_msg, re.IGNORECASE):
            tool_calls.append({
                "id": "finalize_call",
                "name": "finalize_day_plan",
                "arguments": {"notes": "Plan agreed in chat."},
            })

        return {
            "role": "assistant",
            "content": str(raw_reply).strip(),
            "tool_calls": tool_calls,
        }


class MockInferenceEngine:
    """Mock inference engine for deterministic unit testing."""

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
