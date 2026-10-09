"""Solar and Nocturnal Lunar arc visualizer drawn with Cairo."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from gi.repository import Gtk

from thirties_core.astronomy import get_current_time
from thirties_core.models import DayPlan


def get_moon_phase(dt: datetime) -> tuple[str, str]:
    """Calculate synodic lunar phase and emoji glyph for a given datetime."""
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    diff = (dt.astimezone(timezone.utc) - ref).total_seconds() / 86400.0
    synodic = 29.53058867
    cycle = diff % synodic

    if cycle < 1.84:
        return ("🌑", "New Moon")
    elif cycle < 5.53:
        return ("🌒", "Waxing Crescent")
    elif cycle < 9.22:
        return ("🌓", "First Quarter")
    elif cycle < 12.92:
        return ("🌔", "Waxing Gibbous")
    elif cycle < 16.61:
        return ("🌕", "Full Moon")
    elif cycle < 20.30:
        return ("🌖", "Waning Gibbous")
    elif cycle < 23.99:
        return ("🌗", "Last Quarter")
    elif cycle < 27.68:
        return ("🌘", "Waning Crescent")
    else:
        return ("🌑", "New Moon")


class SolarArcWidget(Gtk.DrawingArea):
    """Visualizes the daily sun / nocturnal moon trajectory across the horizon."""

    def __init__(self, day_plan: DayPlan | None = None) -> None:
        super().__init__()
        self.day_plan = day_plan
        self.set_content_height(140)
        self.set_hexpand(True)
        self.set_draw_func(self._on_draw)

    def set_day_plan(self, day_plan: DayPlan) -> None:
        self.day_plan = day_plan
        self.queue_draw()

    def _on_draw(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        if not self.day_plan:
            return

        horizon_y = height - 30
        margin_x = 40
        arc_width = width - 2 * margin_x
        center_x = width / 2.0
        peak_y = 25.0

        now = get_current_time(self.day_plan.sunrise.tzinfo)
        sunrise = self.day_plan.sunrise
        sunset = self.day_plan.sunset
        is_today = (self.day_plan.target_date == now.date())

        is_night = (now < sunrise or now > sunset) if is_today else False

        # 1. Horizon Line
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
        cr.set_line_width(2.0)
        cr.move_to(margin_x, horizon_y)
        cr.line_to(width - margin_x, horizon_y)
        cr.stroke()

        # 2. Arc Curve (Parabolic trajectory)
        if is_night:
            # Twilight Blue / Cool Indigo for nighttime
            cr.set_source_rgba(0.40, 0.62, 0.95, 0.85)
        else:
            # Warm Amber / Golden for daylight
            cr.set_source_rgba(0.95, 0.72, 0.20, 0.80)

        cr.set_line_width(3.0)

        steps = 60
        for i in range(steps + 1):
            fraction = i / steps  # 0.0 to 1.0
            x = margin_x + fraction * arc_width
            norm_x = (fraction - 0.5) * 2.0  # -1.0 to 1.0
            y = peak_y + (1.0 - (1.0 - norm_x * norm_x)) * (horizon_y - peak_y)
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # 3. Sun / Moon Marker at current time
        if is_today:
            if not is_night and (sunrise <= now <= sunset):
                # Sun marker
                day_duration = (sunset - sunrise).total_seconds()
                progress = (now - sunrise).total_seconds() / max(day_duration, 1.0)
                progress = max(0.0, min(1.0, progress))

                sun_x = margin_x + progress * arc_width
                norm_x = (progress - 0.5) * 2.0
                sun_y = peak_y + (1.0 - (1.0 - norm_x * norm_x)) * (horizon_y - peak_y)

                # Outer sun glow
                cr.set_source_rgba(0.98, 0.82, 0.25, 0.35)
                cr.arc(sun_x, sun_y, 14.0, 0, 2 * math.pi)
                cr.fill()

                # Core sun disc
                cr.set_source_rgba(1.0, 0.90, 0.35, 1.0)
                cr.arc(sun_x, sun_y, 7.0, 0, 2 * math.pi)
                cr.fill()
            elif is_night:
                # Moon marker along nocturnal trajectory
                if now > sunset:
                    next_sunrise = sunrise + timedelta(days=1)
                    night_duration = (next_sunrise - sunset).total_seconds()
                    progress = (now - sunset).total_seconds() / max(night_duration, 1.0)
                else:
                    prev_sunset = sunset - timedelta(days=1)
                    night_duration = (sunrise - prev_sunset).total_seconds()
                    progress = (now - prev_sunset).total_seconds() / max(night_duration, 1.0)

                progress = max(0.0, min(1.0, progress))
                moon_x = margin_x + progress * arc_width
                norm_x = (progress - 0.5) * 2.0
                moon_y = peak_y + (1.0 - (1.0 - norm_x * norm_x)) * (horizon_y - peak_y)

                # Outer cool lunar glow
                cr.set_source_rgba(0.40, 0.65, 1.0, 0.35)
                cr.arc(moon_x, moon_y, 14.0, 0, 2 * math.pi)
                cr.fill()

                # Single Moon Phase Glyph centered directly on the trajectory!
                phase_glyph, _ = get_moon_phase(now)
                cr.set_font_size(16.0)
                m_ext = cr.text_extents(phase_glyph)
                cr.move_to(moon_x - m_ext.width / 2.0, moon_y + m_ext.height / 2.0)
                cr.show_text(phase_glyph)

        # 4. Labels for Horizon bounds and Apex (Midday / Midnight)
        cr.set_source_rgba(0.75, 0.75, 0.75, 0.9)
        cr.set_font_size(11.0)

        if is_night:
            if now > sunset:
                left_time = sunset
                right_time = sunrise + timedelta(days=1)
            else:
                left_time = sunset - timedelta(days=1)
                right_time = sunrise
            left_text = f"☾ {left_time.strftime('%I:%M %p').lstrip('0')}"
            right_text = f"☼ {right_time.strftime('%I:%M %p').lstrip('0')}"
            center_text = "Midnight"
        else:
            left_text = f"☼ {sunrise.strftime('%I:%M %p').lstrip('0')}"
            right_text = f"☾ {sunset.strftime('%I:%M %p').lstrip('0')}"
            center_text = "Midday"

        # Left label
        cr.move_to(margin_x, horizon_y + 18)
        cr.show_text(left_text)

        # Right label (dynamically right-aligned)
        r_ext = cr.text_extents(right_text)
        cr.move_to(width - margin_x - r_ext.width, horizon_y + 18)
        cr.show_text(right_text)

        # Apex label (dynamically centered at top)
        c_ext = cr.text_extents(center_text)
        cr.move_to(center_x - c_ext.width / 2.0, peak_y - 8)
        cr.show_text(center_text)
