"""Where the user is: the sun's times depend on it.

City search and naming use OpenStreetMap's Nominatim service (a few requests at
setup, within its usage policy). The time zone comes from the system.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = "Thirties/0.1 (tech.redfoxlabs.Thirties; personal day planner)"


@dataclass
class Place:
    name: str
    latitude: float
    longitude: float


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _short_name(address: dict, fallback: str) -> str:
    town = next((address.get(k) for k in ("city", "town", "village", "hamlet", "municipality", "county") if address.get(k)), "")
    region = address.get("state") or address.get("country") or ""
    return ", ".join(x for x in (town, region) if x) or fallback


def search_places(query: str, limit: int = 6) -> List[Place]:
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "addressdetails": 1, "limit": limit})
    results = _get(f"{NOMINATIM}/search?{params}") or []
    return [Place(_short_name(r.get("address", {}), r.get("display_name", query)), float(r["lat"]), float(r["lon"]))
            for r in results]


def place_name(latitude: float, longitude: float) -> str:
    params = urllib.parse.urlencode({"lat": latitude, "lon": longitude, "format": "jsonv2", "zoom": 10})
    try:
        data = _get(f"{NOMINATIM}/reverse?{params}") or {}
        return _short_name(data.get("address", {}), f"{latitude:.3f}, {longitude:.3f}")
    except Exception:
        return f"{latitude:.3f}, {longitude:.3f}"


def system_timezone() -> str:
    """The system's IANA time zone name ("America/New_York")."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"
