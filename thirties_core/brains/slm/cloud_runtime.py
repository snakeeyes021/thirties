"""Gemini on the shared pipeline: the on-device brain's runtime interface, served by a cloud model.

The pipeline (understand → decide and act → say) is the same for both models: the model makes small decisions
(JSON actions for one request, a short phrasing), and Python does the rest through the shared tools. With this
runtime, Gemini makes those decisions instead of Gemma, so the two can be compared on the same days and the
pipeline can replace the old one-big-prompt brain once it's no worse.

Every call goes through the inference engine, so the sensitive-data filter applies (privacy.py).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from thirties_core.brains.slm.runtime import ConversationRuntime


class CloudRuntime(ConversationRuntime):
    backend = "cloud"
    context_tokens = 32768

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine

    def _ask(self, messages: List[Dict[str, Any]]) -> str:
        reply = self.engine.chat(messages)
        text = str(reply.get("content") or "")
        from thirties_core.inference import TRANSPORT_FAILURE_PREFIXES
        if text.startswith(TRANSPORT_FAILURE_PREFIXES):
            raise RuntimeError(text[:300])
        return text

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        start = time.time()
        prompt = (f"{user}\n\nReturn ONLY one JSON object matching this JSON schema, nothing else:\n"
                  f"{json.dumps(schema, separators=(',', ':'))}")
        with self._lock:
            raw = self._ask([{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        self._log("json", start, len(system) + len(prompt))
        return self._loads(raw, "Gemini")

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        start = time.time()
        with self._lock:
            out = self._ask([{"role": "system", "content": system}, {"role": "user", "content": user}]).strip()
        self._log("text", start, len(system) + len(user))
        return out

    # a conversation is its message list; each send is one call with the whole of it
    def open_conversation(self, system: str) -> Any:
        return [{"role": "system", "content": system}]

    def send(self, conversation: Any, user: str) -> str:
        conversation.append({"role": "user", "content": user})
        out = self._ask(conversation).strip()
        conversation.append({"role": "assistant", "content": out})
        return out

    def close_conversation(self, conversation: Any) -> None:
        conversation.clear()


__all__ = ["CloudRuntime"]
