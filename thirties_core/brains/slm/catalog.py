"""Which small model the SLM brain runs on: the candidates for the model spike, and how to load one.

The brain only sees the runtime interface (``json_call``, ``text_call``, ``parser``; see runtime.py), so swapping
the model is a matter of picking an entry here. Gemma 4 E4B is the default and the only one the app downloads.
The others are for the spike (ROADMAP "After 0.1", item 3): their files are placed in the models folder by hand,
nothing here downloads them, and they run through llama.cpp, the fallback where LiteRT-LM can't run a model.

The choice, first that's set: the ``model`` argument; the ``THIRTIES_SLM_MODEL`` environment variable (for evals);
``config.assistant.slm_model`` (not a setting yet: read with getattr, so the config needn't change for the spike);
else the default.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.brains.slm.runtime import MODEL_FILENAMES, SlmUnavailable, find_model

DEFAULT_MODEL = "gemma-4-e4b"
ENV_VAR = "THIRTIES_SLM_MODEL"


@dataclass(frozen=True)
class SlmModel:
    name: str
    label: str
    backend: str                 # "litert" or "llama.cpp"
    filenames: Tuple[str, ...]   # looked for in the models folder, first found wins
    params: str                  # size, for the comparison table
    licence: str                 # as understood when listed; the spike verifies it before anything ships
    context_tokens: int = 8192
    notes: str = ""
    runtime_args: Dict[str, Any] = field(default_factory=dict)


MODELS: Dict[str, SlmModel] = {m.name: m for m in [
    SlmModel("gemma-4-e4b", "Gemma 4 E4B", "litert", tuple(MODEL_FILENAMES), "4B effective (8B with embeddings)",
             "Gemma terms of use (verify for Gemma 4)",
             notes="The shipped model: built for on-device use, takes audio, runs in the Flatpak on the GPU."),
    SlmModel("gemma-4-e2b", "Gemma 4 E2B", "litert", ("gemma-4-E2B-it-gpu.litertlm", "gemma-4-E2B-it.litertlm"),
             "2B effective", "Gemma terms of use (verify for Gemma 4)",
             notes="Faster; the question is how much parsing accuracy it costs."),
    SlmModel("phi-4-mini", "Phi-4 Mini", "llama.cpp", ("Phi-4-mini-instruct-Q4_K_M.gguf",), "3.8B",
             "MIT (verify)", notes="GGUF at Q4_K_M, about 2.5 GB."),
    SlmModel("qwen3-4b", "Qwen3 4B Instruct", "llama.cpp", ("Qwen3-4B-Instruct-2507-Q4_K_M.gguf",), "4B",
             "Apache-2.0 (verify)", notes="The non-thinking instruct build; GGUF at Q4_K_M, about 2.5 GB."),
    SlmModel("qwen3-8b", "Qwen3 8B", "llama.cpp", ("Qwen3-8B-Q4_K_M.gguf",), "8B", "Apache-2.0 (verify)",
             notes="Only where memory allows (about 5 GB at Q4). A thinking model: prompts need /no_think."),
]}


def selected_model(config: Any = None, model: Optional[str] = None) -> str:
    name = model or os.environ.get(ENV_VAR) or getattr(getattr(config, "assistant", None), "slm_model", "") or ""
    return name.strip().lower() or DEFAULT_MODEL


def _spec(name: str) -> SlmModel:
    spec = MODELS.get(name)
    if spec is None:
        raise SlmUnavailable(f"unknown model '{name}'. Known: {', '.join(MODELS)}.")
    return spec


def _backend_importable(backend: str) -> bool:
    module = {"litert": "litert_lm", "llama.cpp": "llama_cpp"}.get(backend, "")
    return bool(module) and importlib.util.find_spec(module) is not None


def model_file(name: str, configured: str = "") -> Optional[Path]:
    spec = _spec(name)
    # The configured path is the app's Gemma setting; it only counts for the shipped model.
    return find_model(configured if name == DEFAULT_MODEL else "", list(spec.filenames))


def model_unavailable_reason(name: str, configured: str = "") -> Optional[str]:
    """Why a model can't run here, or None if it can (without loading it)."""
    try:
        spec = _spec(name)
    except SlmUnavailable as e:
        return str(e)
    if not _backend_importable(spec.backend):
        return {"litert": "the LiteRT-LM runtime isn't installed.",
                "llama.cpp": "llama-cpp-python isn't installed."}.get(spec.backend, f"no {spec.backend} runtime.")
    if model_file(name, configured) is None:
        if name == DEFAULT_MODEL:
            return "the Gemma model hasn't been downloaded yet (Preferences → Assistant)."
        return f"{spec.label} isn't in the models folder (expected {spec.filenames[0]})."
    return None


def available_models(configured: str = "") -> List[str]:
    return [name for name in MODELS if model_unavailable_reason(name, configured) is None]


_loaded: Dict[str, Any] = {}
_loaded_lock = threading.Lock()


def load_runtime(config: Any = None, model: Optional[str] = None) -> Any:
    """The runtime for the chosen model, loaded once per process. Raises SlmUnavailable when it can't run."""
    name = selected_model(config, model)
    configured = getattr(getattr(config, "assistant", None), "slm_model_path", "") or ""
    spec = _spec(name)
    if name == DEFAULT_MODEL:  # exactly as before the spike: the shared Gemma runtime
        from thirties_core.brains.slm.runtime import GemmaRuntime
        return GemmaRuntime.shared(configured)
    with _loaded_lock:
        if name not in _loaded:
            reason = model_unavailable_reason(name)
            if reason:
                raise SlmUnavailable(reason)
            path = model_file(name)
            if spec.backend == "litert":
                from thirties_core.brains.slm.runtime import LiteRtRuntime
                _loaded[name] = LiteRtRuntime(path, max_tokens=spec.context_tokens)
            else:
                from thirties_core.brains.slm.runtime import LlamaCppRuntime
                _loaded[name] = LlamaCppRuntime(path, context_tokens=spec.context_tokens, **spec.runtime_args)
        return _loaded[name]
