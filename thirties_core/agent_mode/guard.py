"""Before each Agent mode turn: is there room in the usage limit for it? If not, the usual assistant takes the turn.

An Agent mode turn costs many times what a turn of the usual assistant does, from the same subscription. agy's own
5-hour reading for the Gemini models decides: below 35% left hands the turn over. It's read with agy's /quota command,
which doesn't count against the limit, at most every few minutes.

A reading that can't be had doesn't hand over: the turn runs, and the log says the reading was missing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MIN_FIVE_HOUR_LEFT = 0.35
GEMINI_GROUP = "Gemini Models"
READING_TTL_S = 5 * 60  # a reading is reused this long: a burst of messages asks agy once


@dataclass
class Verdict:
    go: bool
    why: str = ""  # for the log: why the turn was handed over


def parse_agy_quota(stdout: str, group: str = GEMINI_GROUP) -> Optional[float]:
    """The 5-hour fraction left for ``group`` from `agy -p /quota --output-format json`."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    for g in ((data.get("command") or {}).get("data") or {}).get("groups", []):
        if g.get("name") != group:
            continue
        for bucket in g.get("buckets", []):
            if bucket.get("window") == "5h":
                try:
                    return float(bucket["remaining_fraction"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def five_hour_from_agy(agy_cmd: list, cwd: Path, timeout: float = 30.0) -> Optional[float]:
    """agy's /quota command (no Agent mode turn, nothing spent)."""
    try:
        res = subprocess.run(agy_cmd + ["-p", "/quota", "--output-format", "json"], capture_output=True,
                             text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("Couldn't read agy's usage limit: %s", e)
        return None
    return parse_agy_quota(res.stdout)


def check(five_hour: Optional[Callable[[], Optional[float]]] = None) -> Verdict:
    """Whether this turn may go to Gemini."""
    left = five_hour() if five_hour is not None else None
    if left is None:
        logger.info("Agent mode: no 5-hour reading; the turn runs.")
        return Verdict(True, "no reading")
    if left < MIN_FIVE_HOUR_LEFT:
        return Verdict(False, f"{left:.0%} of the 5-hour limit left")
    return Verdict(True)


_last: dict = {}


def reading_from(agy_cmd: Optional[list], cwd: Path) -> Callable[[], Optional[float]]:
    """The 5-hour reading: agy's /quota, reused for a few minutes."""
    def read() -> Optional[float]:
        if not agy_cmd or os.environ.get("THIRTIES_AGENT_NO_QUOTA_CALL"):
            return None
        hit = _last.get("reading")
        if hit and time.monotonic() - hit[0] < READING_TTL_S:
            return hit[1]
        left = five_hour_from_agy(agy_cmd, cwd)
        if left is not None:
            _last["reading"] = (time.monotonic(), left)
        return left
    return read
