"""Fitting the on-device model to the device.

- **No GPU:** the generic Gemma 4 E4B build runs on the CPU (runtime.cpu_build); it's downloaded beside the GPU one.
- **Under 8 GB of RAM:** Preferences offers the smaller model, Gemma 4 E2B (faster, noticeably less accurate).
- **When the bigger model doesn't fit:** Thirties *offers* to fetch E2B and remove E4B. It never switches by itself.
  Offered after one confirmed out-of-memory failure, or after two unexplained deaths while the model was working;
  at most once a week if declined.
- **Swapping safely:** E2B is downloaded and checksum-verified, then passes a short test prompt, and only then is
  E4B removed. Switching back downloads E4B the same way.

How "doesn't fit" is told apart:
- **Before loading:** the model needs about 1.2 times its file size, plus the context. If that's plainly more than
  the memory available, it's offered before trying, not after a failure.
- **A marker around model use:** written before loading or generating, cleared afterwards. A marker still there at
  the next start means the last run died while the model was working (kills leave no other trace).
- **A runner process's end** (the model in its own process): an allocation failure, or a SIGKILL while memory was
  nearly exhausted, is out of memory; a segfault or a Python error is an ordinary crash (logged as a bug, no offer).
"""

from __future__ import annotations

import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SMALL_RAM = 8 * 1024 ** 3       # under this, Preferences offers the smaller model
OVERHEAD = 1.2                  # memory needed per byte of model file
CONTEXT_BYTES_PER_TOKEN = 64 * 1024
LOW_AVAILABLE = 0.10            # "nearly exhausted": under this share of memory available...
HIGH_PRESSURE = 20.0            # ...or memory pressure (some avg10, %) at or above this
OFFER_EVERY = timedelta(days=7)
FIT_KEY = "slm_fit"             # local setting: {"memory": n, "unexplained": n, "crash": n, "offered_at", "declined_at"}
SMALL_MODEL = "gemma-4-e2b"

OFFER_TEXT = ("Gemma's bigger model doesn't fit in this device's memory. Get the smaller one (2.6 GB, a bit less "
              "accurate) and remove the big one?")
OFFER_WHERE = "(Preferences → Assistant → Smaller model)"
ALLOC_WORDS = ("bad_alloc", "failed to allocate", "out of memory", "cannot allocate memory", "oom", "allocation failed",
               "resource_exhausted")


# --- memory ---

def meminfo(path: Path = Path("/proc/meminfo")) -> Dict[str, int]:
    """/proc/meminfo in bytes ({} where it can't be read)."""
    out: Dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    except OSError:
        pass
    return out


def pressure(path: Path = Path("/proc/pressure/memory")) -> float:
    """Memory pressure: the share of the last 10 s some task waited for memory (%), 0 if unknown."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith("some"):
                return float(dict(p.split("=") for p in line.split()[1:]).get("avg10", 0))
    except (OSError, ValueError):
        pass
    return 0.0


def total_ram(info: Optional[Dict[str, int]] = None) -> int:
    return (info if info is not None else meminfo()).get("MemTotal", 0)


def small_device(info: Optional[Dict[str, int]] = None) -> bool:
    total = total_ram(info)
    return 0 < total < SMALL_RAM


def need_bytes(model_path: Path, context_tokens: int = 8192) -> int:
    try:
        size = Path(model_path).stat().st_size
    except OSError:
        return 0
    return int(size * OVERHEAD) + context_tokens * CONTEXT_BYTES_PER_TOKEN


def preflight(model_path: Path, context_tokens: int = 8192, info: Optional[Dict[str, int]] = None) -> Optional[str]:
    """Why the model plainly won't fit in the memory available now, or None."""
    info = info if info is not None else meminfo()
    available = info.get("MemAvailable", 0)
    need = need_bytes(model_path, context_tokens)
    if available and need and need > available:
        return (f"{Path(model_path).name} needs about {need / 1024 ** 3:.1f} GB; "
                f"{available / 1024 ** 3:.1f} GB is available")
    return None


def nearly_exhausted(info: Optional[Dict[str, int]] = None, pressure_now: Optional[float] = None) -> bool:
    info = info if info is not None else meminfo()
    total, available = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    low = bool(total) and available / total < LOW_AVAILABLE
    return low or (pressure_now if pressure_now is not None else pressure()) >= HIGH_PRESSURE


# --- how a runner process ended ---

def classify_exit(returncode: Optional[int], output: str = "", exhausted: Optional[bool] = None) -> str:
    """"ok", "memory" (out of memory), "unexplained" (killed, memory looked fine) or "crash" (a bug)."""
    if returncode in (0, None):
        return "ok"
    text = (output or "").lower()
    if any(w in text for w in ALLOC_WORDS):
        return "memory"
    killed = returncode in (-signal.SIGKILL, 128 + signal.SIGKILL)
    if killed:
        return "memory" if (nearly_exhausted() if exhausted is None else exhausted) else "unexplained"
    return "crash"


# --- the marker around model use ---

def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "thirties"


class Marker:
    """"The model is working": written before loading or generating, cleared afterwards. Nested uses share it."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self.depth = 0

    @property
    def path(self) -> Path:  # looked up when used, so the state folder can be set after import
        return self._path or state_dir() / "model-in-use.json"

    def begin(self, model: str, phase: str) -> None:
        self.depth += 1
        if self.depth > 1:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"pid": os.getpid(), "model": model, "phase": phase,
                                             "at": datetime.now(timezone.utc).isoformat()}))
        except OSError as e:
            logger.debug("Couldn't write the model marker: %s", e)

    def end(self) -> None:
        self.depth = max(0, self.depth - 1)
        if self.depth == 0:
            try:
                self.path.unlink()
            except OSError:
                pass

    def left_behind(self) -> Optional[Dict[str, Any]]:
        """A marker from a run that died while the model was working (not this process, and that process gone)."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        pid = int(data.get("pid") or 0)
        if pid == os.getpid():
            return None
        try:
            os.kill(pid, 0)
            return None  # still running (another Thirties process using the model now)
        except ProcessLookupError:
            return data
        except (PermissionError, OSError):
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


MARKER = Marker()


class Using:
    """``with Using(model, phase):`` around a load or a generation."""

    def __init__(self, model: str, phase: str, marker: Optional[Marker] = None) -> None:
        self.model, self.phase, self.marker = model, phase, marker or MARKER

    def __enter__(self):
        self.marker.begin(self.model, self.phase)
        return self

    def __exit__(self, *exc):
        self.marker.end()
        return False


def events_path() -> Path:
    return state_dir() / "model-events.jsonl"


def note_event(kind: str, detail: str = "") -> None:
    """For code with no database at hand (the runtime): an event the next check records."""
    try:
        events_path().parent.mkdir(parents=True, exist_ok=True)
        with open(events_path(), "a") as f:
            f.write(json.dumps({"kind": kind, "detail": detail}) + "\n")
    except OSError as e:
        logger.debug("Couldn't note a model event: %s", e)


# --- the record, and the offer ---

class FitRecord:
    """What's happened to the model on this device, and whether to offer the smaller one (a device setting)."""

    def __init__(self, db) -> None:
        self.db = db

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.db.get_setting(FIT_KEY) or "{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.db.set_setting(FIT_KEY, json.dumps(data, sort_keys=True))

    def record(self, kind: str, detail: str = "") -> None:
        """"memory", "unexplained" or "crash" (a crash is logged as a bug, and never leads to the offer)."""
        data = self._load()
        data[kind] = int(data.get(kind, 0)) + 1
        self._save(data)
        log = logger.error if kind == "crash" else logger.warning
        log("The on-device model %s%s", {"memory": "ran out of memory", "unexplained": "died while working",
                                         "crash": "crashed"}.get(kind, kind), f": {detail}" if detail else "")

    def check_startup(self, marker: Optional[Marker] = None) -> Optional[str]:
        """At start: a marker left behind is a death during model use, and events noted without a database get
        recorded. Returns the kind of the last thing recorded, if any."""
        marker = marker or MARKER
        last = None
        try:
            lines = events_path().read_text().splitlines()
            events_path().unlink()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.record(event.get("kind", "unexplained"), event.get("detail", ""))
            last = event.get("kind")
        left = marker.left_behind()
        if left is None:
            return last
        marker.clear()
        kind = "memory" if nearly_exhausted() else "unexplained"
        self.record(kind, f"{left.get('phase', '')} of {left.get('model', '')} at {left.get('at', '')}")
        return kind

    def should_offer(self, now: Optional[datetime] = None, current_model: str = "") -> bool:
        if current_model == SMALL_MODEL:
            return False
        data = self._load()
        if not (int(data.get("memory", 0)) >= 1 or int(data.get("unexplained", 0)) >= 2):
            return False
        now = now or datetime.now(timezone.utc)
        for key in ("offered_at", "declined_at"):
            if data.get(key):
                try:
                    if now - datetime.fromisoformat(data[key]) < OFFER_EVERY:
                        return False
                except ValueError:
                    pass
        return True

    def offered(self, now: Optional[datetime] = None) -> None:
        data = self._load()
        data["offered_at"] = (now or datetime.now(timezone.utc)).isoformat()
        self._save(data)

    def declined(self, now: Optional[datetime] = None) -> None:
        data = self._load()
        data["declined_at"] = (now or datetime.now(timezone.utc)).isoformat()
        self._save(data)


# --- swapping models ---

def swap(to_small: bool, settings_set: Callable[[str, str], None], gpu: bool = True,
         download: Optional[Callable[..., Path]] = None, test: Optional[Callable[[Path], bool]] = None,
         dest_dir: Optional[Path] = None, progress=None, cancel=None) -> str:
    """Switch to the smaller model (or back): download and verify the new one, run a short test prompt on it, and
    only then remove the old one and change the setting. Returns what happened, in a sentence."""
    from thirties_core.brains.slm import model_download as md
    download = download or md.download_model
    test = test or smoke_test
    if to_small:
        new_specs = [md.GEMMA_E2B if gpu else md.GEMMA_E2B_CPU]
        old_names = [md.GEMMA_E4B.filename, md.GEMMA_E4B_CPU.filename]
        model, label = SMALL_MODEL, "Gemma 4 E2B"
    else:
        new_specs = [md.GEMMA_E4B, md.GEMMA_E4B_CPU] if gpu else [md.GEMMA_E4B_CPU]
        old_names = [md.GEMMA_E2B.filename, md.GEMMA_E2B_CPU.filename]
        model, label = "", "Gemma 4 E4B"
    paths = [download(spec, progress=progress, cancel=cancel, dest_dir=dest_dir) for spec in new_specs]
    if not test(paths[0]):
        return f"{label} downloaded but didn't pass its test, so nothing was changed."
    folder = dest_dir or md.models_dir()
    removed = 0
    for name in old_names:
        p = Path(folder) / name
        if p.is_file() and p not in paths:
            p.unlink()
            removed += 1
    settings_set("assistant.slm_model", model)
    return f"Now using {label}" + (f"; the other model was removed." if removed else ".")


def smoke_test(path: Path) -> bool:
    """A short test prompt on a freshly downloaded model: it loads and answers in JSON."""
    try:
        from thirties_core.brains.slm.runtime import LiteRtRuntime
        runtime = LiteRtRuntime(path, max_tokens=1024, cache=False)
        out = runtime.json_call("Answer in JSON.", 'Say hello as {"hello": "<word>"}.',
                                {"type": "object", "properties": {"hello": {"type": "string"}}, "required": ["hello"]},
                                max_output_tokens=32)
        return bool(out.get("hello"))
    except Exception as e:
        logger.warning("The new model failed its test: %s", e)
        return False


__all__ = ["preflight", "need_bytes", "small_device", "classify_exit", "Marker", "MARKER", "Using", "FitRecord",
           "swap", "smoke_test", "OFFER_TEXT", "SMALL_MODEL"]
