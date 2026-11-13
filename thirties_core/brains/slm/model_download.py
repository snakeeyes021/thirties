"""Downloading the on-device Gemma model on first use.

The model is ~3 GB, too large to ship in the app, and freely downloadable (not
gated) from Hugging Face. It goes to $XDG_DATA_HOME/thirties/models, resuming
an interrupted download and checking the file's SHA-256 before it's used.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    filename: str
    url: str
    size: int
    sha256: str


# Each file is fetched from a fixed revision of its repository, never "main": the files there get replaced (on
# 2026-11-11 the E4B GPU build on main changed size and checksum), and a download must be exactly the build that
# was measured, or it fails its check.
E4B_REPO = "https://huggingface.co/litert-community/gemma-4-E4B-it-litert-lm/resolve/8b77b27bfeb8d39447ac174c07d5bcde70e45d81"
E2B_REPO = "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/2d74ac41a33d7da82f39f7d5720a04c68514a103"

GEMMA_E4B = ModelSpec(
    filename="gemma-4-E4B-it-gpu.litertlm",
    url=f"{E4B_REPO}/gemma-4-E4B-it-gpu.litertlm",
    size=2_969_059_328,
    sha256="4912bb5a9c30993c51a7711f763212077458529312175df0573a78323a2bb7ff",
)
GEMMA_E4B_CPU = ModelSpec(  # the generic build: runs on the CPU when there's no usable GPU
    filename="gemma-4-E4B-it.litertlm",
    url=f"{E4B_REPO}/gemma-4-E4B-it.litertlm",
    size=3_659_530_240,
    sha256="0b2a8980ce155fd97673d8e820b4d29d9c7d99b8fa6806f425d969b145bd52e0",
)
GEMMA_E2B = ModelSpec(  # the smaller model, for devices where E4B doesn't fit
    filename="gemma-4-E2B-it-gpu.litertlm",
    url=f"{E2B_REPO}/gemma-4-E2B-it-gpu.litertlm",
    size=2_340_870_772,
    sha256="d3cfec6ae2e027a994e51366d44dbf522ec7531a4a35fbbfcd3e9cc9b927afc1",
)
GEMMA_E2B_CPU = ModelSpec(
    filename="gemma-4-E2B-it.litertlm",
    url=f"{E2B_REPO}/gemma-4-E2B-it.litertlm",
    size=2_588_147_712,
    sha256="181938105e0eefd105961417e8da75903eacda102c4fce9ce90f50b97139a63c",
)

CHUNK = 1 << 20


def models_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "thirties" / "models"


class DownloadCancelled(Exception):
    pass


def download_model(spec: ModelSpec = GEMMA_E4B,
                   progress: Optional[Callable[[int, int], None]] = None,
                   cancel: Optional[threading.Event] = None,
                   dest_dir: Optional[Path] = None) -> Path:
    """Download (or resume) the model; returns its path. Call off the UI thread.

    ``progress(done_bytes, total_bytes)`` is called as it goes; setting ``cancel``
    stops it (the partial file is kept, so the next attempt resumes).
    """
    dest_dir = dest_dir or models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / spec.filename
    if final.is_file() and final.stat().st_size == spec.size:
        return final
    part = dest_dir / (spec.filename + ".part")
    done = part.stat().st_size if part.exists() else 0
    if done > spec.size:
        part.unlink()
        done = 0
    if done < spec.size:
        req = urllib.request.Request(spec.url, headers={"Range": f"bytes={done}-"} if done else {})
        with urllib.request.urlopen(req, timeout=60) as resp:
            if done and resp.status != 206:  # the server ignored the range: start over
                done = 0
            with open(part, "ab" if done else "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, spec.size)
    if done != spec.size:
        raise IOError(f"The download stopped early ({done} of {spec.size} bytes); try again to resume.")
    digest = hashlib.sha256()
    with open(part, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK * 8), b""):
            digest.update(chunk)
    if digest.hexdigest() != spec.sha256:
        part.unlink()
        raise IOError("The downloaded model was corrupted; it has been removed. Please try again.")
    part.rename(final)
    logger.info("Gemma model downloaded to %s", final)
    return final
