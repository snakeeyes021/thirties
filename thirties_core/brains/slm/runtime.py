"""On-device model runtimes: one engine per process, small calls.

The SLM brain uses exactly three things from a runtime, and nothing else (the model-neutral interface, so other
small models can be measured against Gemma; ROADMAP "After 0.1", item 3):

- ``json_call(system, user, schema)``: a JSON object matching a schema (constrained decoding), from a fresh
  conversation, so no history leaks between unrelated decisions;
- ``text_call(system, user)``: a short free-text sentence, for small talk;
- ``parser(system)``: a ``ParserSession``, a long-lived conversation that keeps its instruction in the KV cache.

A backend provides those by subclassing ``ConversationRuntime``: ``json_call``, ``text_call``, and three
conversation primitives (``open_conversation``, ``send``, ``close_conversation``) that ``ParserSession`` drives.
Backends here: LiteRT-LM (Gemma, the shipped one) and llama.cpp (GGUF models: Phi-4 Mini, Qwen; for the model
spike only). Which model runs is chosen in ``catalog.py``.
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


def model_homes() -> List[Path]:
    """Where downloaded models live (the app's data folder, inside or outside the Flatpak)."""
    homes = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")), Path.home() / ".local" / "share"]
    return [home / "thirties" / "models" for home in dict.fromkeys(homes)]


def find_model(configured: str = "", filenames: Optional[List[str]] = None) -> Optional[Path]:
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(os.path.expanduser(configured)))
    candidates += [home / name for home in model_homes() for name in (filenames or MODEL_FILENAMES)]
    return next((c for c in candidates if c.is_file()), None)


def _litert_importable() -> bool:
    import importlib.util
    return importlib.util.find_spec("litert_lm") is not None


def slm_unavailable_reason(config: Any = None) -> Optional[str]:
    """Why the on-device brain can't run here, or None if it can."""
    from thirties_core.brains.slm.catalog import model_unavailable_reason, selected_model
    configured = getattr(getattr(config, "assistant", None), "slm_model_path", "") or ""
    return model_unavailable_reason(selected_model(config), configured)


class SlmUnavailable(RuntimeError):
    """The on-device model can't run here (no runtime package or no model file)."""


class ConversationRuntime:
    """What every backend shares: the lock, the timing log, and parser sessions over its conversations."""

    backend = "?"
    context_tokens = 8192

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []  # timing log, for evals
        self.load_seconds = 0.0

    # --- the interface the brain uses ---

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        raise NotImplementedError

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        raise NotImplementedError

    def parser(self, system: str) -> "ParserSession":
        return ParserSession(self, system)

    # --- long-lived conversations for ParserSession (called with self._lock held) ---

    def open_conversation(self, system: str) -> Any:
        raise NotImplementedError

    def send(self, conversation: Any, user: str) -> str:
        raise NotImplementedError

    def close_conversation(self, conversation: Any) -> None:
        raise NotImplementedError

    def _log(self, kind: str, start: float, prompt_chars: int) -> None:
        self.calls.append({"kind": kind, "seconds": round(time.time() - start, 2), "prompt_chars": prompt_chars})

    @staticmethod
    def _loads(raw: str, who: str) -> Dict[str, Any]:
        try:
            value = json.loads(raw)
        except ValueError:
            from thirties_core.brains.slm.parse import extract_json
            value = extract_json(raw)
            if value is None:
                logger.warning("%s returned unparseable JSON: %r", who, raw[:300])
        return value if isinstance(value, dict) else {}


class LiteRtRuntime(ConversationRuntime):
    """Gemma through LiteRT-LM: loads once (GPU if possible, else CPU) and serves constrained calls."""

    backend = "litert"
    _shared: Optional["LiteRtRuntime"] = None
    _shared_lock = threading.Lock()

    def __init__(self, model_path: Path, max_tokens: int = 8192) -> None:
        super().__init__()
        try:
            import litert_lm
        except ImportError as e:
            raise SlmUnavailable("the LiteRT-LM runtime isn't installed.") from e
        self._lm = litert_lm
        self.model_path = model_path
        self.context_tokens = max_tokens
        cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thirties" / "litert"
        cache_dir.mkdir(parents=True, exist_ok=True)
        start = time.time()
        try:
            self.engine = litert_lm.Engine(str(model_path), backend=litert_lm.Backend.GPU(),
                                           max_num_tokens=max_tokens, cache_dir=str(cache_dir))
            self.device = "gpu"
        except Exception as gpu_error:
            logger.warning("Gemma on GPU failed (%s); using CPU.", gpu_error)
            self.engine = litert_lm.Engine(str(model_path), backend=litert_lm.Backend.CPU(),
                                           max_num_tokens=max_tokens, cache_dir=str(cache_dir))
            self.device = "cpu"
        self.load_seconds = time.time() - start
        logger.info("Gemma loaded from %s on %s in %.1fs", model_path, self.device, self.load_seconds)

    @classmethod
    def shared(cls, configured_path: str = "", filenames: Optional[List[str]] = None) -> "LiteRtRuntime":
        with cls._shared_lock:
            if cls._shared is None:
                path = find_model(configured_path, filenames)
                if path is None:
                    raise SlmUnavailable("the Gemma model hasn't been downloaded yet.")
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
        self._log("json", start, len(system) + len(user))
        return self._loads(raw, "Gemma")

    def open_conversation(self, system: str) -> Any:
        return self.engine.create_conversation(
            system_message=system, sampler_config=self._lm.SamplerConfig(temperature=0.0, top_k=1))

    def send(self, conversation: Any, user: str) -> str:
        return self._text(conversation.send_message(user)).strip()

    def close_conversation(self, conversation: Any) -> None:
        conversation.close()

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
        self._log("text", start, len(system) + len(user))
        return out


GemmaRuntime = LiteRtRuntime  # the name the rest of the code (and the evals) know it by


class LlamaCppRuntime(ConversationRuntime):
    """A GGUF model through llama.cpp (llama-cpp-python): the fallback backend for the model spike, for models
    LiteRT-LM can't run (Phi-4 Mini, Qwen). JSON-schema grammars give the constrained decoding; a conversation is
    its message list, and llama.cpp reuses the matching prompt prefix from its KV cache, so a parser session pays
    only for the new message, as with LiteRT. Not shipped in the app (no wheel in the Flatpak); evals only."""

    backend = "llama.cpp"

    def __init__(self, model_path: Path, context_tokens: int = 8192, gpu_layers: int = -1,
                 chat_format: Optional[str] = None) -> None:
        super().__init__()
        try:
            import llama_cpp
        except ImportError as e:
            raise SlmUnavailable("llama-cpp-python isn't installed (pip install llama-cpp-python, built with "
                                 "Vulkan or CUDA for the GPU).") from e
        self.model_path = Path(model_path)
        self.context_tokens = context_tokens
        start = time.time()
        self.llm = llama_cpp.Llama(model_path=str(model_path), n_ctx=context_tokens, n_gpu_layers=gpu_layers,
                                   chat_format=chat_format, verbose=False)
        self.device = "gpu" if gpu_layers else "cpu"
        self.load_seconds = time.time() - start
        logger.info("Loaded %s with llama.cpp in %.1fs", self.model_path.name, self.load_seconds)

    def _complete(self, messages: List[Dict[str, str]], max_tokens: int, temperature: float, top_k: int,
                  schema: Optional[Dict[str, Any]] = None) -> str:
        kwargs: Dict[str, Any] = {"messages": list(messages), "max_tokens": max_tokens, "temperature": temperature,
                                  "top_k": top_k}
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object", "schema": schema}
        out = self.llm.create_chat_completion(**kwargs)
        return str(((out.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        with self._lock:
            start = time.time()
            raw = self._complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                 max_output_tokens, 0.0, 1, schema)
        self._log("json", start, len(system) + len(user))
        return self._loads(raw, self.model_path.name)

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        with self._lock:
            start = time.time()
            out = self._complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                 max_output_tokens, 0.3, 20).strip()
        self._log("text", start, len(system) + len(user))
        return out

    def open_conversation(self, system: str) -> Any:
        return [{"role": "system", "content": system}]

    def send(self, conversation: Any, user: str) -> str:
        conversation.append({"role": "user", "content": user})
        out = self._complete(conversation, 256, 0.0, 1).strip()
        conversation.append({"role": "assistant", "content": out})
        return out

    def close_conversation(self, conversation: Any) -> None:
        conversation.clear()


class ParserSession:
    """A long-lived conversation that reuses the model's KV cache between messages.

    The instruction and examples are processed once; each later message costs only
    its own tokens (measured: ~12s for the first call, 2-3s after on an iGPU).
    The session restarts before it outgrows the context window.
    """

    # JSON and examples run ~3 characters per token, so budget the instruction plus history at about 3 characters
    # per token of the runtime's context window, with headroom.
    TOKEN_LIMIT = 8192
    CHARS_PER_TOKEN = 3.0

    def __init__(self, runtime: ConversationRuntime, system: str, budget_chars: Optional[int] = None) -> None:
        self.runtime = runtime
        self.system = system
        limit = getattr(runtime, "context_tokens", None) or self.TOKEN_LIMIT
        usable_tokens = limit - 700  # room for the next message and its reply
        self.budget_chars = budget_chars or int(usable_tokens * self.CHARS_PER_TOKEN) - len(system)
        self._conv = None
        self._used = 0

    def _open(self) -> None:
        if self._conv is not None:
            self.runtime.close_conversation(self._conv)
        self._conv = self.runtime.open_conversation(self.system)
        self._used = 0

    def ask(self, user: str) -> str:
        with self.runtime._lock:
            if self._conv is None or self._used + len(user) + 600 > self.budget_chars:
                self._open()
            start = time.time()
            try:
                out = self.runtime.send(self._conv, user)
            except RuntimeError as e:
                logger.warning("Model call failed (%s); retrying in a fresh session.", e)
                self._open()
                out = self.runtime.send(self._conv, user)
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
            with self.runtime._lock:
                self.runtime.close_conversation(self._conv)
            self._conv = None
