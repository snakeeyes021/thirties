"""The sun and the moon on request: the next sunrise, or the moon's phase and whether it's growing (a model left to
guess gets it wrong). Python computes it for the user's place
(astronomy.py: NOAA for the sun, Meeus for the moon); the model only reads the answer back. sky_line puts
the moon tonight and tomorrow's sun in the prompt, and reply_checks holds a reply to the computed phase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from thirties_core.astronomy import moon_phase as _moon, next_moon, next_sun_events
from thirties_core.clock import format_clock

SKY_TOOL_SPECS: List[Dict[str, Any]] = [{
    "name": "sky",
    "description": "The sun and the moon for the user's place, computed: the next sunrise, sunset, dawn, dusk and "
                   "solar noon from now, the moon's phase (waxing or waning, how much is lit), and the next new and "
                   "full moon. Use it for any question about the sun or the moon; never guess them. Read-only.",
    "parameters": {"day": "A day to ask about instead of now (e.g. 'Saturday'): its sun times and its moon."},
    "required": [],
}]
SKY_READ_ONLY = {"sky"}


def _day_word(when: datetime, today: date) -> str:
    d = when.date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return when.strftime("%A, %B %-d")


def _soon(when: datetime, now: datetime) -> str:
    days = (when - now).total_seconds() / 86400
    if days < 1:
        return "within a day"
    if days < 1.5:
        return "in about a day"
    return f"in about {round(days)} days"


def moon_text(when: datetime) -> str:
    phase = _moon(when)
    lit = round(100 * float(phase["lit"]))
    trend = "growing toward full" if phase["waxing"] else "shrinking toward new"
    name = str(phase["name"])
    if name in ("new moon", "full moon"):
        said = f"Moon: {name}, {lit}% lit"
    else:
        said = f"Moon: {name} ({trend}), {lit}% lit"
    full, new = next_moon(when, 180.0), next_moon(when, 0.0)
    nxt = sorted([(full, "full moon"), (new, "new moon")])
    return said + ". " + " ".join(f"Next {label}: {t.strftime('%A, %B %-d')} at {format_clock(t)} "
                                  f"({_soon(t, when)})." for t, label in nxt)


def sky_text(lat: float, lon: float, tz, now: datetime, day: date = None) -> str:
    """The answer, in plain lines."""
    if day is not None and day != now.date():
        from thirties_core.astronomy import get_solar_phases
        p = get_solar_phases(lat, lon, day, tz)
        noon = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(hours=12)
        return "\n".join([f"{day.strftime('%A, %B %-d')}: sunrise {format_clock(p['sunrise'])}, sunset "
                          f"{format_clock(p['sunset'])} (first light {format_clock(p['dawn'])}, last light "
                          f"{format_clock(p['dusk'])}).", moon_text(noon)])
    nxt = next_sun_events(lat, lon, now, tz)
    today = now.date()
    lines = [f"It's {format_clock(now)} now.",
             f"Next sunrise: {_day_word(nxt['sunrise'], today)} at {format_clock(nxt['sunrise'])}.",
             f"Next sunset: {_day_word(nxt['sunset'], today)} at {format_clock(nxt['sunset'])}.",
             f"Next first light: {_day_word(nxt['dawn'], today)} at {format_clock(nxt['dawn'])}; next last light: "
             f"{_day_word(nxt['dusk'], today)} at {format_clock(nxt['dusk'])}.",
             moon_text(now)]
    return "\n".join(lines)


def tool(assistant, day: Any = None) -> str:
    from thirties_core.assistant_tools import ToolError
    from thirties_core.clock import ClockError, parse_day
    pt = assistant.pt
    gen = pt.scheduler.config.general if pt.scheduler else None
    if gen is None:
        raise ToolError("The user's place isn't known here.")
    now = pt._now()
    which = pt.day_plan.target_date if pt.day_plan.target_date != pt.today() else None  # "day" picks the plan
    if day not in (None, ""):
        try:
            which = parse_day(day, now.date())
        except (ClockError, ValueError):
            raise ToolError(f"Which day is '{day}'?")
    return sky_text(gen.latitude, gen.longitude, gen.timezone, now, which)


def moon_phase(when: datetime) -> Tuple[str, int]:
    """(phase name, percent lit) at ``when``."""
    phase = _moon(when)
    return str(phase["name"]), round(100 * float(phase["lit"]))


def waxing(when: datetime) -> bool:
    return bool(_moon(when)["waxing"])


def next_phase(when: datetime, full: bool) -> datetime:
    """When the next full (or new) moon is."""
    return next_moon(when, 180.0 if full else 0.0)


def sky_line(now: datetime, lat: Optional[float] = None, lon: Optional[float] = None) -> str:
    """One line of facts for the assistant's prompt: the moon tonight, and tomorrow's sun when the place is known."""
    name, lit = moon_phase(now)
    out = (f"Moon tonight: {name} ({'waxing' if waxing(now) else 'waning'}, {lit}% lit); next full moon "
           f"{next_phase(now, True).strftime('%a %b %-d')}, next new moon {next_phase(now, False).strftime('%a %b %-d')}.")
    if lat is not None and lon is not None and now.tzinfo is not None:
        try:
            from thirties_core.astronomy import get_solar_phases
            tomorrow = (now + timedelta(days=1)).date()
            phases = get_solar_phases(lat, lon, tomorrow, now.tzinfo)
            out += f" Tomorrow: sunrise {format_clock(phases['sunrise'])}, sunset {format_clock(phases['sunset'])}."
        except Exception:
            pass
    return out


__all__ = ["SKY_TOOL_SPECS", "SKY_READ_ONLY", "sky_text", "moon_text", "tool", "moon_phase", "waxing",
           "next_phase", "sky_line"]
