"""Astronomical and solar phase calculations for the Thirties grid.

Calculates dawn (civil twilight), sunrise, solar noon, sunset, and dusk,
and builds the solar canvas for a diurnal day: 48 discrete 30-minute
intervals starting at the thirty that contains sunrise.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from thirties_core.models import DayPlan, ThirtyBlock


_SIMULATED_DEBUG_TIME: Optional[str] = None


def set_debug_time(val: Optional[str | datetime]) -> None:
    """Set or clear runtime simulated time (e.g. '22:30', '12:00', datetime, or None)."""
    global _SIMULATED_DEBUG_TIME
    if isinstance(val, datetime):
        _SIMULATED_DEBUG_TIME = val.isoformat()
    else:
        _SIMULATED_DEBUG_TIME = val


def get_current_time(tz: Optional[ZoneInfo] = None) -> datetime:
    """Return the current time, supporting runtime or env var debug simulation.

    Checks _SIMULATED_DEBUG_TIME first, then THIRTIES_DEBUG_TIME env var.
    Format examples:
    - "22:30" (10:30 PM today)
    - "02:15" (2:15 AM today)
    - "2026-10-09T22:30:00" (explicit ISO timestamp)
    """
    debug_val = _SIMULATED_DEBUG_TIME or os.environ.get("THIRTIES_DEBUG_TIME")
    if debug_val:
        try:
            if "T" in debug_val:
                dt = datetime.fromisoformat(debug_val)
                return dt.astimezone(tz) if tz else dt
            elif ":" in debug_val:
                parts = debug_val.strip().split(":")
                hour = int(parts[0])
                minute = int(parts[1])
                now_base = datetime.now(tz)
                return now_base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            pass
    return datetime.now(tz)


def _get_zoneinfo(tz: str | ZoneInfo) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo("UTC")


def _noaa_solar_phases(lat: float, lon: float, target_date: date, tz: ZoneInfo) -> Dict[str, datetime]:
    """Calculate solar phases using standard NOAA solar calculation equations."""
    y = target_date.year
    m = target_date.month
    d = target_date.day

    if m <= 2:
        y -= 1
        m += 12

    a = math.floor(y / 100)
    b = 2 - a + math.floor(a / 4)
    jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5
    t = (jd - 2451545.0) / 36525.0

    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360)
    m_anom = math.radians((357.52911 + t * (35999.05029 - 0.0001537 * t)) % 360)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    c = (
        math.sin(m_anom) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_anom) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_anom) * 0.000289
    )
    sun_true_lon = l0 + math.radians(c)

    omega = math.radians(125.04 - 1934.136 * t)
    lambda_sun = sun_true_lon - math.radians(0.00569) - math.radians(0.00478) * math.sin(omega)

    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))

    # Solar declination
    delta = math.asin(math.sin(eps) * math.sin(lambda_sun))

    # Equation of time (in minutes)
    y_tan = math.tan(eps / 2.0) ** 2
    eq_time = 4.0 * math.degrees(
        y_tan * math.sin(2 * l0)
        - 2 * e * math.sin(m_anom)
        + 4 * e * y_tan * math.sin(m_anom) * math.cos(2 * l0)
        - 0.5 * (y_tan ** 2) * math.sin(4 * l0)
        - 1.25 * (e ** 2) * math.sin(2 * m_anom)
    )

    # Solar noon in UTC minutes from 00:00
    noon_utc_min = 720 - 4.0 * lon - eq_time

    lat_rad = math.radians(lat)
    cos_lat = math.cos(lat_rad)
    sin_lat = math.sin(lat_rad)
    cos_delta = math.cos(delta)
    sin_delta = math.sin(delta)

    def hour_angle(zenith_deg: float) -> Optional[float]:
        cos_z = math.cos(math.radians(zenith_deg))
        denominator = cos_lat * cos_delta
        if abs(denominator) < 1e-9:
            return None
        cos_ha = (cos_z - sin_lat * sin_delta) / denominator
        if cos_ha > 1.0 or cos_ha < -1.0:
            return None
        return math.degrees(math.acos(cos_ha))

    # Standard sunrise/sunset zenith with atmospheric refraction: 90.833 degrees
    ha_sun = hour_angle(90.833)
    # Civil twilight zenith: 96.0 degrees
    ha_civil = hour_angle(96.0)

    def minutes_to_datetime(minutes: float) -> datetime:
        base_utc = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
        return (base_utc + timedelta(minutes=minutes)).astimezone(tz)

    solar_noon_dt = minutes_to_datetime(noon_utc_min)

    if ha_sun is None:
        # Polar day or night: fallback approximate dawn/dusk/noon
        return {
            "dawn": solar_noon_dt,
            "sunrise": solar_noon_dt,
            "solar_noon": solar_noon_dt,
            "sunset": solar_noon_dt,
            "dusk": solar_noon_dt,
        }

    ha_civil_val = ha_civil if ha_civil is not None else ha_sun

    return {
        "dawn": minutes_to_datetime(noon_utc_min - 4.0 * ha_civil_val),
        "sunrise": minutes_to_datetime(noon_utc_min - 4.0 * ha_sun),
        "solar_noon": solar_noon_dt,
        "sunset": minutes_to_datetime(noon_utc_min + 4.0 * ha_sun),
        "dusk": minutes_to_datetime(noon_utc_min + 4.0 * ha_civil_val),
    }


def get_solar_phases(
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str | ZoneInfo = "UTC",
) -> Dict[str, datetime]:
    """Retrieve solar phase datetimes (dawn, sunrise, solar_noon, sunset, dusk) for a date.

    Pure-Python NOAA equations only: an optional Astral path meant the Flatpak (no Astral) and a dev venv
    with it installed could disagree by a minute about where the day starts.
    """
    return _noaa_solar_phases(lat, lon, target_date, _get_zoneinfo(tz_name))


def get_diurnal_date(
    current_dt: Optional[datetime] = None,
    lat: float = 40.7128,
    lon: float = -74.0060,
    tz_name: str | ZoneInfo = "UTC",
) -> date:
    """Return the active diurnal date for current_dt.

    A Thirties day cycle begins at sunrise. If current_dt is before today's
    sunrise, it belongs to yesterday's nocturnal cycle.
    """
    tz = _get_zoneinfo(tz_name)
    now = current_dt or get_current_time(tz)
    cal_date = now.date()
    try:
        phases = get_solar_phases(lat, lon, cal_date, tz)
        today_sunrise = phases["sunrise"]
        if now < today_sunrise:
            return cal_date - timedelta(days=1)
    except Exception:
        pass
    return cal_date


def diurnal_day_start(sunrise: datetime) -> datetime:
    """Start of the diurnal day: sunrise rounded down to the half hour, in local wall time."""
    return sunrise.replace(minute=0 if sunrise.minute < 30 else 30, second=0, microsecond=0)


def build_base_thirties(
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str | ZoneInfo = "UTC",
) -> List[ThirtyBlock]:
    """Construct the 48-thirty solar canvas for the diurnal day target_date.

    Block 0 is the thirty containing sunrise on target_date; the canvas then
    runs 24 hours, through the night, into the early morning of the next
    calendar date. Blocks are spaced in absolute time, so DST changes never
    produce duplicated or missing thirties.
    """
    tz = _get_zoneinfo(tz_name)
    phases = get_solar_phases(lat, lon, target_date, tz)
    next_phases = get_solar_phases(lat, lon, target_date + timedelta(days=1), tz)
    sunrise, sunset = phases["sunrise"], phases["sunset"]

    start_utc = diurnal_day_start(sunrise).astimezone(timezone.utc)
    blocks: List[ThirtyBlock] = []
    for index in range(48):
        block_start = (start_utc + timedelta(minutes=30 * index)).astimezone(tz)
        block_end = (start_utc + timedelta(minutes=30 * (index + 1))).astimezone(tz)
        midpoint = block_start + timedelta(minutes=15)

        is_sunlight = sunrise <= midpoint <= sunset
        is_twilight = (
            (sunset < midpoint <= phases["dusk"])
            or (next_phases["dawn"] <= midpoint < next_phases["sunrise"])
            or (phases["dawn"] <= midpoint < sunrise)
        )
        blocks.append(
            ThirtyBlock(
                index=index,
                start_dt=block_start,
                end_dt=block_end,
                is_sunlight=is_sunlight,
                is_twilight=is_twilight,
            )
        )

    return blocks


def create_base_day_plan(
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str | ZoneInfo = "UTC",
) -> DayPlan:
    """Create an empty DayPlan: the solar canvas with no spans."""
    tz = _get_zoneinfo(tz_name)
    phases = get_solar_phases(lat, lon, target_date, tz)
    return DayPlan(
        target_date=target_date,
        blocks=build_base_thirties(lat, lon, target_date, tz),
        sunrise=phases["sunrise"],
        sunset=phases["sunset"],
    )


# --- the moon: its phase, waxing or waning, and the next new and full moon ---
# Low-precision positions after Meeus, Astronomical Algorithms ch. 25 and 47 (the Moon's longitude to about 0.3°):
# the phase follows from how far the Moon is east of the Sun (its elongation), and the next new or full moon is
# found by stepping forward and halving the step, to within a few minutes of the formula (an hour or two of the
# almanac's time).

SYNODIC_DAYS = 29.530588853
_PRINCIPAL = ((0.0, "new moon"), (90.0, "first quarter"), (180.0, "full moon"), (270.0, "last quarter"))
_PRINCIPAL_WITHIN = 7.0  # degrees either side (about 14 hours) that still count as the named instant


def _julian_day(when: datetime) -> float:
    return when.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5


def moon_elongation(when: datetime) -> float:
    """How far the Moon is east of the Sun, in degrees from 0 (new) through 180 (full) to 360."""
    t = (_julian_day(when) - 2451545.0) / 36525.0
    rad = math.radians
    sun_mean = 280.46646 + 36000.76983 * t
    sun_anom = rad(357.52911 + 35999.05029 * t)
    sun_long = sun_mean + (1.914602 - 0.004817 * t) * math.sin(sun_anom) + 0.019993 * math.sin(2 * sun_anom) \
        + 0.000289 * math.sin(3 * sun_anom)
    moon_mean = 218.3164477 + 481267.88123421 * t
    d = rad(297.8501921 + 445267.1114034 * t)
    m_moon = rad(134.9633964 + 477198.8675055 * t)
    f = rad(93.2720950 + 483202.0175233 * t)
    moon_long = (moon_mean + 6.288774 * math.sin(m_moon) + 1.274027 * math.sin(2 * d - m_moon)
                 + 0.658314 * math.sin(2 * d) + 0.213618 * math.sin(2 * m_moon) - 0.185116 * math.sin(sun_anom)
                 - 0.114332 * math.sin(2 * f) + 0.058793 * math.sin(2 * d - 2 * m_moon)
                 + 0.057066 * math.sin(2 * d - sun_anom - m_moon) + 0.053322 * math.sin(2 * d + m_moon)
                 + 0.045758 * math.sin(2 * d - sun_anom) - 0.040923 * math.sin(sun_anom - m_moon)
                 - 0.034720 * math.sin(d) - 0.030383 * math.sin(sun_anom + m_moon))
    return (moon_long - sun_long) % 360.0


def moon_phase(when: datetime) -> Dict[str, object]:
    """The Moon's phase at ``when``: its name ("waxing gibbous"), the share of its face lit (0 to 1), and
    whether it's waxing (growing toward full) or waning."""
    e = moon_elongation(when)
    lit = (1.0 - math.cos(math.radians(e))) / 2.0
    waxing = e < 180.0
    name = ""
    for angle, label in _PRINCIPAL:
        gap = min(abs(e - angle), 360.0 - abs(e - angle))
        if gap <= _PRINCIPAL_WITHIN:
            name = label
    if not name:
        shape = "crescent" if lit < 0.5 else "gibbous"
        name = f"{'waxing' if waxing else 'waning'} {shape}"
    return {"name": name, "lit": lit, "waxing": waxing, "elongation": e}


def next_moon(when: datetime, target: float) -> datetime:
    """The next moment after ``when`` that the Moon is ``target`` degrees east of the Sun (0: new, 180: full)."""
    def ahead(t: datetime) -> float:  # degrees still to go to the target, 0..360
        return (target - moon_elongation(t)) % 360.0
    step = timedelta(hours=6)
    t = when
    gone = ahead(t)
    if gone < 1e-6:
        t += step
        gone = ahead(t)
    for _ in range(4 * 31):
        nxt = t + step
        if ahead(nxt) > gone:  # crossed the target between t and nxt
            lo, hi = t, nxt
            while hi - lo > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if ahead(mid) > ahead(lo):
                    hi = mid
                else:
                    lo = mid
            return hi
        t, gone = nxt, ahead(nxt)
    return when + timedelta(days=SYNODIC_DAYS)  # pragma: no cover - the loop always finds it


def next_sun_events(lat: float, lon: float, now: datetime, tz_name: str | ZoneInfo = "UTC") -> Dict[str, datetime]:
    """The next dawn, sunrise, solar noon, sunset and dusk after ``now`` (each today's if still ahead, else
    tomorrow's)."""
    tz = _get_zoneinfo(tz_name)
    local = now.astimezone(tz)
    today = get_solar_phases(lat, lon, local.date(), tz)
    tomorrow = get_solar_phases(lat, lon, local.date() + timedelta(days=1), tz)
    return {k: (today[k] if today[k] > local else tomorrow[k]) for k in today}
