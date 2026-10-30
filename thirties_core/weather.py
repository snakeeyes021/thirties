"""Local weather, so outdoor plans fit the sky.

The forecast comes from Open-Meteo (free, no account or key), at most once an
hour, cached in the state database so the app works offline with the last one.
Python turns it into plain facts ("Rain likely 6–9 PM (60%)") and answers "is
this a bad time to be outside?"; brains never interpret raw numbers.
"""

from __future__ import annotations

import json
import locale
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from thirties_core.clock import format_clock

logger = logging.getLogger(__name__)

API = "https://api.open-meteo.com/v1/forecast"
CACHE_KEY = "weather_cache"
REFRESH = timedelta(hours=1)

# WMO weather codes (Open-Meteo): description, and whether it's bad for being outside
WMO: Dict[int, tuple] = {
    0: ("clear", False), 1: ("mostly clear", False), 2: ("partly cloudy", False), 3: ("overcast", False),
    45: ("fog", False), 48: ("freezing fog", True),
    51: ("light drizzle", True), 53: ("drizzle", True), 55: ("heavy drizzle", True),
    56: ("freezing drizzle", True), 57: ("freezing drizzle", True),
    61: ("light rain", True), 63: ("rain", True), 65: ("heavy rain", True), 66: ("freezing rain", True),
    67: ("freezing rain", True), 71: ("light snow", True), 73: ("snow", True), 75: ("heavy snow", True),
    77: ("snow grains", True), 80: ("light showers", True), 81: ("showers", True), 82: ("heavy showers", True),
    85: ("snow showers", True), 86: ("heavy snow showers", True), 95: ("thunderstorms", True),
    96: ("thunderstorms with hail", True), 99: ("thunderstorms with hail", True),
}

OUTDOOR_RE = re.compile(r"\b(walk|run|jog|hike|bike|cycl|ride|garden|yard|mow|lawn|outside|outdoor|park|beach|"
                        r"picnic|sunbath|stargaz|patio|porch|grill|bbq|wash the car|car wash|kayak|paddle|golf|"
                        r"tennis|soccer|frisbee|fishing|camp)", re.IGNORECASE)


def default_unit() -> str:
    """Fahrenheit where the locale says so (US and a few others), else Celsius."""
    loc = os.environ.get("LC_MEASUREMENT") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if not loc:
        try:
            loc = locale.getlocale()[0] or ""
        except Exception:
            loc = ""
    return "fahrenheit" if re.search(r"_(US|LR|MM|BS|BZ|KY|PW)\b", loc) else "celsius"


@dataclass
class Hour:
    at: datetime
    temp: float
    rain_chance: int
    code: int
    wind: float

    @property
    def description(self) -> str:
        return WMO.get(self.code, ("unsettled", False))[0]

    def bad_reason(self, unit: str) -> Optional[str]:
        """Why this hour is a poor time to be outside, or None."""
        bad_sky = WMO.get(self.code, ("", False))[1]
        if bad_sky or self.rain_chance >= 50:
            return self.description if bad_sky else f"rain likely ({self.rain_chance}%)"
        cold, hot = (25, 95) if unit == "fahrenheit" else (-4, 35)
        if self.temp <= cold:
            return "very cold"
        if self.temp >= hot:
            return "very hot"
        if self.wind >= (30 if unit == "fahrenheit" else 48):
            return "very windy"
        return None


class Weather:
    def __init__(self, state_db, latitude: float, longitude: float, tz_name: str, unit: str = "",
                 fetcher: Optional[Callable[[str], dict]] = None) -> None:
        self.db = state_db
        self.lat, self.lon = latitude, longitude
        self.tz = ZoneInfo(tz_name)
        self.unit = unit or default_unit()
        self._fetch = fetcher or self._get

    @staticmethod
    def _get(url: str) -> dict:
        if os.environ.get("THIRTIES_OFFLINE"):
            raise OSError("offline (THIRTIES_OFFLINE is set)")
        req = urllib.request.Request(url, headers={"User-Agent": "Thirties/0.1 (tech.redfoxlabs.Thirties)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _url(self) -> str:
        params = {"latitude": round(self.lat, 3), "longitude": round(self.lon, 3), "timezone": self.tz.key,
                  "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
                  "forecast_days": 4, "temperature_unit": self.unit,
                  "wind_speed_unit": "mph" if self.unit == "fahrenheit" else "kmh"}
        return f"{API}?{urllib.parse.urlencode(params)}"

    def hours(self) -> List[Hour]:
        """The forecast, refreshed at most hourly; the last one if offline; [] if never fetched."""
        raw = self.db.get_sync_meta(CACHE_KEY)
        cached = json.loads(raw) if raw else None
        fresh = cached and cached.get("key") == self._url() and \
            datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"]) < REFRESH
        if not fresh:
            try:
                data = self._fetch(self._url())
                cached = {"key": self._url(), "fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
                self.db.set_sync_meta(CACHE_KEY, json.dumps(cached))
            except Exception as e:
                logger.info("Weather unavailable (%s); using the last forecast if any.", e)
        if not cached or cached.get("key") != self._url():  # another place or unit: not this forecast
            return []
        h = cached["data"]["hourly"]
        return [Hour(datetime.fromisoformat(t).replace(tzinfo=self.tz), h["temperature_2m"][i],
                     int(h["precipitation_probability"][i] or 0), int(h["weather_code"][i] or 0),
                     float(h["wind_speed_10m"][i] or 0)) for i, t in enumerate(h["time"])]

    def _deg(self, temp: float) -> str:
        return f"{round(temp)}°{'F' if self.unit == 'fahrenheit' else 'C'}"

    def bad_for_outdoors(self, start: datetime, end: datetime) -> Optional[str]:
        """'rain likely (70%) around 6 PM' if being outside then is a poor idea, else None."""
        for h in self.hours():
            if start - timedelta(minutes=59) < h.at < end:
                reason = h.bad_reason(self.unit)
                if reason:
                    return f"{reason} around {format_clock(max(h.at, start))}"
        return None

    def facts(self, start: datetime, end: datetime) -> List[str]:
        """Plain sentences about the weather between start and end (e.g. the rest of the day)."""
        hours = [h for h in self.hours() if start - timedelta(minutes=59) < h.at < end]
        if not hours:
            return []
        now = hours[0]
        lines = [f"Around {format_clock(max(now.at, start))}: {self._deg(now.temp)}, {now.description}."]
        temps = [h.temp for h in hours]
        lines.append(f"Until {format_clock(end)}: between {self._deg(min(temps))} and {self._deg(max(temps))}.")
        run: List[Hour] = []
        for h in hours + [None]:
            reason = h.bad_reason(self.unit) if h else None
            if reason:
                run.append(h)
                continue
            if run:
                first, last = run[0], run[-1]
                worst = max(run, key=lambda x: x.rain_chance)
                what = first.bad_reason(self.unit)
                span = (f"{format_clock(first.at)}–{format_clock(last.at + timedelta(hours=1))}"
                        if len(run) > 1 else f"around {format_clock(first.at)}")
                chance = f" ({worst.rain_chance}% chance of rain)" if worst.rain_chance >= 30 else ""
                lines.append(f"Poor for being outside {span}: {what}{chance}.")
                run = []
        if len(lines) == 2:
            lines.append("No bad weather expected for being outside.")
        return lines


def weather_for(config, state_db) -> Optional[Weather]:
    gen = config.general
    if gen.latitude is None or gen.longitude is None:
        return None
    return Weather(state_db, gen.latitude, gen.longitude, gen.timezone, getattr(gen, "temperature_unit", ""))
