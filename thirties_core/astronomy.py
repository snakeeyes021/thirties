"""Astronomical and solar phase calculations for the Thirties grid.

Calculates dawn (civil twilight), sunrise, solar noon, sunset, and dusk,
and partitions a given local day into 48 discrete 30-minute intervals
anchored against sunlight and twilight.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from thirties_core.models import BlockKind, DayPlan, ThirtyBlock


def get_current_time(tz: Optional[ZoneInfo] = None) -> datetime:
    """Return the current time, supporting debug simulation via THIRTIES_DEBUG_TIME env var.

    Format examples for THIRTIES_DEBUG_TIME:
    - "22:30" (10:30 PM today)
    - "02:15" (2:15 AM today)
    - "2026-10-09T22:30:00" (explicit ISO timestamp)
    """
    debug_val = os.environ.get("THIRTIES_DEBUG_TIME")
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
    
    Uses Astral if installed; falls back to pure-Python NOAA astronomical equations.
    """
    tz = _get_zoneinfo(tz_name)

    try:
        from astral import LocationInfo
        from astral.sun import sun

        loc = LocationInfo(
            name="Custom",
            region="Custom",
            timezone=tz.key if hasattr(tz, "key") else str(tz_name),
            latitude=lat,
            longitude=lon,
        )
        s = sun(loc.observer, date=target_date, tzinfo=tz)
        return {
            "dawn": s["dawn"],
            "sunrise": s["sunrise"],
            "solar_noon": s["noon"],
            "sunset": s["sunset"],
            "dusk": s["dusk"],
        }
    except (ImportError, Exception):
        return _noaa_solar_phases(lat, lon, target_date, tz)


def build_base_thirties(
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str | ZoneInfo = "UTC",
) -> List[ThirtyBlock]:
    """Construct the foundational 48 Thirty blocks for target_date.
    
    Each block is tagged with sunlight and twilight properties based on
    its midpoint relative to the calculated solar phases.
    """
    tz = _get_zoneinfo(tz_name)
    phases = get_solar_phases(lat, lon, target_date, tz)
    sunrise = phases["sunrise"]
    sunset = phases["sunset"]
    dawn = phases["dawn"]
    dusk = phases["dusk"]

    day_start = datetime.combine(target_date, time.min, tzinfo=tz)
    blocks: List[ThirtyBlock] = []

    for index in range(48):
        block_start = day_start + timedelta(minutes=30 * index)
        block_end = block_start + timedelta(minutes=30)
        midpoint = block_start + timedelta(minutes=15)

        is_sunlight = (sunrise <= midpoint <= sunset)
        is_twilight = (dawn <= midpoint < sunrise) or (sunset < midpoint <= dusk)

        kind = BlockKind.DAYLIGHT_DISCRETIONARY if is_sunlight else BlockKind.DARK_DISCRETIONARY

        blocks.append(
            ThirtyBlock(
                index=index,
                start_dt=block_start,
                end_dt=block_end,
                is_sunlight=is_sunlight,
                is_twilight=is_twilight,
                kind=kind,
            )
        )

    return blocks


def create_base_day_plan(
    lat: float,
    lon: float,
    target_date: date,
    tz_name: str | ZoneInfo = "UTC",
) -> DayPlan:
    """Create a pristine DayPlan populated with 48 base blocks and solar boundaries."""
    tz = _get_zoneinfo(tz_name)
    phases = get_solar_phases(lat, lon, target_date, tz)
    blocks = build_base_thirties(lat, lon, target_date, tz)

    plan = DayPlan(
        target_date=target_date,
        blocks=blocks,
        sunrise=phases["sunrise"],
        sunset=phases["sunset"],
    )
    plan.recalculate_counts()
    return plan
