"""A llama.cpp runtime for the SLM brain: the same calls as GemmaRuntime, over llama-server's HTTP API.

The SLM brain only uses three things from its runtime: ``json_call`` (a JSON object
matching a schema), ``text_call`` (a short free-text reply) and ``parser(system)``
(a long-lived conversation that reuses the KV cache). This class offers exactly
those, so any GGUF model llama.cpp can run (Gemma 4 12B/31B, Qwen, Phi) can stand
in for Gemma E4B under LiteRT-LM. Spike of 2026-10-30 (ROADMAP "After 0.1" item 3);
the harness that measures it is tests/model_spike.py.

Decisions:
- Constrained output uses llama-server's ``json_schema`` (a GBNF grammar built
  from the schema), the counterpart of LiteRT's llguidance constraint.
- Each long-lived session is pinned to its own server slot (``id_slot``) so the
  parser's instruction stays cached while the voice and picker sessions run;
  without that, every switch between sessions would re-read the whole
  instruction (~1.5k tokens). Run the server with ``-np`` at least the number of
  sessions plus one; fresh calls take any free slot.
- Thinking is switched off per request (``enable_thinking: false``): Qwen 3.x
  would otherwise think for hundreds of tokens before every answer.
- Standard library only (urllib), so it can ship in the Flatpak unchanged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LlamaServerError(RuntimeError):
    """llama-server didn't answer, or answered with an error."""


class LlamaServerRuntime:
    """Talks to a running llama-server; one instance per server."""

    backend = "llama.cpp"

    def __init__(self, base_url: str = "http://127.0.0.1:8090", slots: int = 1, ctx_per_slot: int = 4096,
                 timeout: float = 600.0, thinking: bool = False, load_seconds: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.slots = max(1, slots)
        self.ctx_per_slot = ctx_per_slot
        self.timeout = timeout
        self.thinking = thinking
        self.load_seconds = load_seconds
        self._lock = threading.Lock()
        self._next_slot = 0
        self.calls: List[Dict[str, Any]] = []  # timing log, for evals (same shape as GemmaRuntime.calls)

    # --- the runtime interface the SLM brain uses ---

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        start = time.time()
        with self._lock:
            raw, timings = self.chat(messages, max_tokens=max_output_tokens, schema=schema)
        self._log("json", start, len(system) + len(user), timings)
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except ValueError:
            logger.warning("llama.cpp returned unparseable JSON: %r", raw[:300])
            return {}

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        start = time.time()
        with self._lock:
            out, timings = self.chat(messages, max_tokens=max_output_tokens, temperature=0.3, top_k=20)
        self._log("text", start, len(system) + len(user), timings)
        return out.strip()

    def parser(self, system: str) -> "LlamaParserSession":
        return LlamaParserSession(self, system, slot=self._claim_slot())

    # --- plumbing ---

    def _claim_slot(self) -> Optional[int]:
        """A slot of its own for each long-lived session; the last slot stays free for fresh calls."""
        if self.slots < 2:
            return None
        slot = self._next_slot % (self.slots - 1)
        self._next_slot += 1
        return slot

    def _log(self, kind: str, start: float, prompt_chars: int, timings: Dict[str, Any]) -> None:
        self.calls.append({"kind": kind, "seconds": round(time.time() - start, 2), "prompt_chars": prompt_chars,
                           "prompt_tokens": timings.get("prompt_n"), "cached_tokens": timings.get("cache_n"),
                           "output_tokens": timings.get("predicted_n"),
                           "prompt_ms": timings.get("prompt_ms"), "output_ms": timings.get("predicted_ms")})

    def request(self, path: str, body: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read()[:300]
            raise LlamaServerError(f"llama-server {path}: HTTP {e.code} {detail!r}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LlamaServerError(f"llama-server {path}: {e}") from e

    def chat(self, messages: List[Dict[str, str]], max_tokens: int = 256, schema: Optional[Dict[str, Any]] = None,
             temperature: float = 0.0, top_k: int = 1, slot: Optional[int] = None,
             cache_prompt: bool = True) -> Tuple[str, Dict[str, Any]]:
        """One chat completion; returns the text and llama-server's timings (prompt_n, cache_n, predicted_n, ...)."""
        body: Dict[str, Any] = {
            "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "top_k": top_k,
            "cache_prompt": cache_prompt, "chat_template_kwargs": {"enable_thinking": self.thinking},
        }
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
        if slot is not None:
            body["id_slot"] = slot
        reply = self.request("/v1/chat/completions", body)
        try:
            text = reply["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LlamaServerError(f"unexpected reply: {str(reply)[:300]}") from e
        return text, reply.get("timings") or {}

    def healthy(self) -> bool:
        try:
            return self.request("/health", timeout=5).get("status") == "ok"
        except LlamaServerError:
            return False


class LlamaParserSession:
    """The llama.cpp twin of runtime.ParserSession: a growing conversation whose prefix stays cached.

    llama-server keeps the slot's KV cache and only reads the new tokens when the
    next request starts with the same messages. The history restarts before it
    outgrows the slot's context, budgeted at about 3 characters per token like
    ParserSession.
    """

    CHARS_PER_TOKEN = 3.0
    MAX_OUTPUT_TOKENS = 400  # LiteRT's sessions have no cap; five actions of JSON stay well under this

    def __init__(self, runtime: LlamaServerRuntime, system: str, slot: Optional[int] = None,
                 budget_chars: Optional[int] = None) -> None:
        self.runtime = runtime
        self.system = system
        self.slot = slot
        usable_tokens = runtime.ctx_per_slot - 700  # room for the next message and its reply
        self.budget_chars = budget_chars or int(usable_tokens * self.CHARS_PER_TOKEN) - len(system)
        self.history: List[Dict[str, str]] = []
        self._used = 0

    def _messages(self, user: str) -> List[Dict[str, str]]:
        return [{"role": "system", "content": self.system}] + self.history + [{"role": "user", "content": user}]

    def _send(self, user: str):
        return self.runtime.chat(self._messages(user), max_tokens=self.MAX_OUTPUT_TOKENS, slot=self.slot)

    def ask(self, user: str) -> str:
        with self.runtime._lock:
            if self._used + len(user) + 600 > self.budget_chars:
                self.history, self._used = [], 0
            start = time.time()
            try:
                out, timings = self._send(user)
            except LlamaServerError as e:
                logger.warning("llama.cpp call failed (%s); retrying in a fresh session.", e)
                self.history, self._used = [], 0
                out, timings = self._send(user)
            out = out.strip()
            self.history += [{"role": "user", "content": user}, {"role": "assistant", "content": out}]
            self._used += len(user) + len(out)
        kind = "parse" if self.system.startswith("You convert") else ("pick" if self.system.startswith("You help") else "voice")
        self.runtime._log(kind, start, len(user), timings)
        return out

    def warm(self) -> None:
        """Process the instruction ahead of the first real message."""
        self.ask('Now: (warm-up).\nMessage: "thanks"')

    def close(self) -> None:
        self.history, self._used = [], 0
