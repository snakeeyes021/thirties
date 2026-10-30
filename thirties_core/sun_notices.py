"""A message at sunrise and/or sunset, every day, when the user wants one (10-30: "I'd use it to turn the indoor
lights on and off"). It's asked for, so it goes out whatever the message level, asleep or not, to the desktop and
the phone, and it isn't added to the conversation. The times are the real NOAA ones for the user's place."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.astronomy import get_solar_phases

WINDOW = timedelta(minutes=5)   # sent within this long after the moment (the background ticks every 15 s)
TEXT = {"sunrise": "🌅 Sunrise.", "sunset": "🌇 Sunset."}

SUN_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "set_sun_notices",
    "description": "Turn the daily sunrise and/or sunset message on or off, when the user asks (e.g. to switch their "
                   "lights). It comes to the desktop and the phone every day at the real time.",
    "parameters": {"sunrise": "true or false (leave out to keep it as it is).",
                   "sunset": "true or false (leave out to keep it as it is)."},
    "required": [],
}]


def _on(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def set_sun_notices(config, settings, sunrise: Any = None, sunset: Any = None) -> str:
    for key, value in (("sunrise", sunrise), ("sunset", sunset)):
        if value not in (None, ""):
            setattr(config.assistant, f"{key}_notice", _on(value))
            if settings is not None:
                settings.set(f"assistant.{key}_notice", _on(value))
    on = [k for k in ("sunrise", "sunset") if getattr(config.assistant, f"{k}_notice", False)]
    return ("You'll get a message at " + " and ".join(on) + " every day." if on
            else "No sunrise or sunset messages.")


def due(config, now: datetime, sent: Dict[str, str]) -> Optional[str]:
    """The notice due now ("sunrise" / "sunset"), if it's switched on and not sent today."""
    gen = config.general
    try:
        phases = get_solar_phases(gen.latitude, gen.longitude, now.date(), gen.timezone)
    except Exception:
        return None
    for key in ("sunrise", "sunset"):
        if not getattr(config.assistant, f"{key}_notice", False):
            continue
        at = phases.get(key)
        if at is not None and at <= now < at + WINDOW and sent.get(key) != now.date().isoformat():
            return key
    return None


__all__ = ["SUN_TOOL_SPECS", "set_sun_notices", "due", "TEXT", "WINDOW"]
