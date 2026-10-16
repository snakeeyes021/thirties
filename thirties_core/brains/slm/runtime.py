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
    homes = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")), Path.home() / ".local" / "share"]
    candidates += [home / "thirties" / "models" / name for home in dict.fromkeys(homes) for name in MODEL_FILENAMES]
    return next((c for c in candidates if c.is_file()), None)


# In the Flatpak there's no LiteRT-LM: Gemma runs on the host, in the LiteRT venv (docs/SLM_BRAIN.md §5).
HOST_VENV = Path.home() / ".local" / "share" / "thirties" / "venv"
SERVER_COPY = Path.home() / ".local" / "share" / "thirties" / "gemma_server.py"


def in_flatpak() -> bool:
    return Path("/.flatpak-info").exists() or bool(os.environ.get("FLATPAK_ID"))


def _litert_importable() -> bool:
    import importlib.util
    return importlib.util.find_spec("litert_lm") is not None


def _use_host() -> bool:
    return os.environ.get("THIRTIES_SLM_HOST") == "1" or (in_flatpak() and not _litert_importable())


def slm_unavailable_reason(config: Any = None) -> Optional[str]:
    """Why the on-device brain can't run here, or None if it can."""
    configured = getattr(getattr(config, "assistant", None), "slm_model_path", "") or ""
    if find_model(configured) is None:
        return "there's no Gemma model in ~/.local/share/thirties/models (see docs/SLM_BRAIN.md §5)."
    if _use_host():
        if not (HOST_VENV / "pyvenv.cfg").exists():
            return "the LiteRT venv isn't set up at ~/.local/share/thirties/venv (see docs/SLM_BRAIN.md §5)."
        return None
    if not _litert_importable():
        return "the litert-lm-api package isn't installed for this Python."
    return None


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
                cls._shared = HostGemmaRuntime(path) if _use_host() else cls(path)
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

    # Long-lived conversations for ParserSession (call with self._lock held).
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
                logger.warning("Gemma call failed (%s); retrying in a fresh session.", e)
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


class HostGemmaRuntime:
    """GemmaRuntime's interface, served by gemma_server.py on the host (for the Flatpak).

    The server is copied to ~/.local/share/thirties (visible to both sides) and run
    with the host's LiteRT venv through ``flatpak-spawn --host``.
    """

    def __init__(self, model_path: Path) -> None:
        import atexit
        import subprocess
        self.model_path = model_path
        self._lock = threading.Lock()      # held by ParserSession around a whole exchange
        self._io_lock = threading.Lock()   # one request at a time on the pipe
        self.calls: List[Dict[str, Any]] = []
        self._next_sid = 0
        source = Path(__file__).with_name("gemma_server.py").read_text()
        SERVER_COPY.parent.mkdir(parents=True, exist_ok=True)
        if not SERVER_COPY.exists() or SERVER_COPY.read_text() != source:
            SERVER_COPY.write_text(source)
        cmd = [str(HOST_VENV / "bin" / "python"), str(SERVER_COPY), str(model_path)]
        if in_flatpak():
            cmd = ["flatpak-spawn", "--host"] + cmd
        log_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thirties"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(log_dir / "gemma_server.log", "w")
        start = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                     text=True, bufsize=1)
        atexit.register(self.close)
        hello = self._read()
        if not hello.get("ready"):
            raise SlmUnavailable(f"Gemma didn't start on the host: {hello.get('error', 'no reply')}")
        self.backend = hello.get("backend", "?")
        self.load_seconds = time.time() - start
        logger.info("Gemma serving from the host on %s in %.1fs", self.backend, self.load_seconds)

    def _read(self) -> Dict[str, Any]:
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        if not line:
            raise SlmUnavailable("The host Gemma process stopped; see ~/.cache/thirties/gemma_server.log.")
        return json.loads(line)

    def _rpc(self, **request: Any) -> Dict[str, Any]:
        with self._io_lock:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            reply = self._read()
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply

    def parser(self, system: str) -> "ParserSession":
        return ParserSession(self, system)

    def open_conversation(self, system: str) -> int:
        self._next_sid += 1
        self._rpc(op="open", sid=self._next_sid, system=system)
        return self._next_sid

    def send(self, conversation: int, user: str) -> str:
        return self._rpc(op="send", sid=conversation, user=user)["out"]

    def close_conversation(self, conversation: int) -> None:
        self._rpc(op="close", sid=conversation)

    def json_call(self, system: str, user: str, schema: Dict[str, Any], max_output_tokens: int = 256) -> Dict[str, Any]:
        start = time.time()
        with self._lock:
            raw = self._rpc(op="json", system=system, user=user, schema=schema, max_output_tokens=max_output_tokens)["raw"]
        self.calls.append({"kind": "json", "seconds": round(time.time() - start, 2), "prompt_chars": len(system) + len(user)})
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("Gemma returned unparseable JSON: %r", raw[:300])
            return {}

    def text_call(self, system: str, user: str, max_output_tokens: int = 120) -> str:
        start = time.time()
        with self._lock:
            out = self._rpc(op="text", system=system, user=user, max_output_tokens=max_output_tokens)["out"]
        self.calls.append({"kind": "text", "seconds": round(time.time() - start, 2), "prompt_chars": len(system) + len(user)})
        return out

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                with self._io_lock:
                    self.proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                    self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

