"""On-device Gemma runtime (LiteRT-LM): one engine per process, small constrained calls.

The SLM brain only ever asks the model for two things: a JSON object that
matches a schema (constrained decoding guarantees it parses), or a short
free-text sentence for small talk. Each call starts a fresh conversation so no
history leaks between unrelated decisions.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODEL_FILENAMES = [
    "gemma-4-E4B-it-gpu.litertlm",
    "gemma-4-E4B-it.litertlm",
    "gemma-4-E2B-it-gpu.litertlm",
    "gemma-4-E2B-it.litertlm",
]


def find_model(configured: str = "") -> Optional[Path]:
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(os.path.expanduser(configured)))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    candidates += [data_home / "thirties" / "models" / name for name in MODEL_FILENAMES]
    return next((c for c in candidates if c.is_file()), None)


class SlmUnavailable(RuntimeError):
    """The on-device model can't run here (no LiteRT-LM package or no model file)."""


class GemmaRuntime:
    """Loads Gemma once (GPU if possible, else CPU) and serves constrained calls."""

    _shared: Optional["GemmaRuntime"] = None
    _shared_lock = threading.Lock()

    def __init__(self, model_path: Path, max_tokens: int = 8192) -> None:
        try:
            import litert_lm
        except ImportError as e:
            raise SlmUnavailable("The litert-lm-api package is not installed for this Python.") from e
        self._lm = litert_lm
        self.model_path = model_path
        cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thirties" / "litert"
        cache_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        try:
            self.engine = litert_lm.Engine(str(model_path), backend=litert_lm.Backend.GPU(),
                                           max_num_tokens=max_tokens, cache_dir=str(cache_dir))
            self.backend = "gpu"
        except Exception as gpu_error:
            logger.warning("Gemma on GPU failed (%s); using CPU.", gpu_error)
            self.engine = litert_lm.Engine(str(model_path), backend=litert_lm.Backend.CPU(),
                                           max_num_tokens=max_tokens, cache_dir=str(cache_dir))
            self.backend = "cpu"
        self.load_seconds = time.time() - start
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []  # timing log, for evals
        logger.info("Gemma loaded from %s on %s in %.1fs", model_path, self.backend, self.load_seconds)

    @classmethod
    def shared(cls, configured_path: str = "") -> "GemmaRuntime":
        with cls._shared_lock:
            if cls._shared is None:
                path = find_model(configured_path)
                if path is None:
                    raise SlmUnavailable("No Gemma model found in ~/.local/share/thirties/models.")
                cls._shared = cls(path)
            return cls._shared

    def _text(self, message: Any) -> str:
        if isinstance(message, dict):
            parts = message.get("content", [])
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return str(message)

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        lm = self._lm
        with self._lock:
            start = time.time()
            conv = self.engine.create_conversation(
                system_message=system,
                constrained_decoding_config=lm.ConstrainedDecodingConfig(
                    enable=True, provider=lm.LiteRtLmConstraintProviderType.LL_GUIDANCE),
                sampler_config=lm.SamplerConfig(temperature=0.0, top_k=1),
                max_output_tokens=max_output_tokens,
            )
            try:
                raw = self._text(conv.send_message(user, response_format=lm.ResponseFormat.json(schema)))
            finally:
                conv.close()
            elapsed = time.time() - start
        self.calls.append({"kind": "json", "seconds": round(elapsed, 2), "prompt_chars": len(system) + len(user)})
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("Gemma returned unparseable JSON: %r", raw[:300])
            return {}

    def parser(self, system: str) -> "ParserSession":
        return ParserSession(self, system)

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        lm = self._lm
        with self._lock:
            start = time.time()
            conv = self.engine.create_conversation(
                system_message=system,
                sampler_config=lm.SamplerConfig(temperature=0.3, top_k=20),
                max_output_tokens=max_output_tokens,
            )
            try:
                out = self._text(conv.send_message(user)).strip()
            finally:
                conv.close()
            elapsed = time.time() - start
        self.calls.append({"kind": "text", "seconds": round(elapsed, 2), "prompt_chars": len(system) + len(user)})
        return out


class ParserSession:
    """A long-lived conversation that reuses the model's KV cache between messages.

    The instruction and examples are processed once; each later message costs only
    its own tokens (measured: ~12s for the first call, 2-3s after on an iGPU).
    The session restarts before it outgrows the context window.
    """

    # The KV cache holds 8192 tokens (see GemmaRuntime max_tokens). JSON and examples run ~3 characters per token, so
    # budget the instruction plus history at about 3 characters per token, with headroom.
    TOKEN_LIMIT = 8192
    CHARS_PER_TOKEN = 3.0

    def __init__(self, runtime: GemmaRuntime, system: str, budget_chars: Optional[int] = None) -> None:
        self.runtime = runtime
        self.system = system
        usable_tokens = self.TOKEN_LIMIT - 700  # room for the next message and its reply
        self.budget_chars = budget_chars or int(usable_tokens * self.CHARS_PER_TOKEN) - len(system)
        self._conv = None
        self._used = 0

    def _open(self) -> None:
        lm = self.runtime._lm
        if self._conv is not None:
            self._conv.close()
        self._conv = self.runtime.engine.create_conversation(
            system_message=self.system, sampler_config=lm.SamplerConfig(temperature=0.0, top_k=1))
        self._used = 0

    def ask(self, user: str) -> str:
        with self.runtime._lock:
            if self._conv is None or self._used + len(user) + 600 > self.budget_chars:
                self._open()
            start = time.time()
            try:
                out = self.runtime._text(self._conv.send_message(user)).strip()
            except RuntimeError as e:
                logger.warning("Gemma call failed (%s); retrying in a fresh session.", e)
                self._open()
                out = self.runtime._text(self._conv.send_message(user)).strip()
            self._used += len(user) + len(out)
            elapsed = time.time() - start
        kind = "parse" if self.system.startswith("You convert") else ("pick" if self.system.startswith("You help") else "voice")
        self.runtime.calls.append({"kind": kind, "seconds": round(elapsed, 2), "prompt_chars": len(user)})
        return out

    def warm(self) -> None:
        """Process the instruction ahead of the first real message."""
        self.ask('Now: (warm-up).\nMessage: "thanks"')

    def close(self) -> None:
        if self._conv is not None:
            self._conv.close()
            self._conv = None
