"""Solar arc visualizer drawn with Cairo."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from gi.repository import Gtk

from thirties_core.models import DayPlan


class SolarArcWidget(Gtk.DrawingArea):
    """Visualizes the daily sun trajectory across the horizon."""

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

        # 1. Horizon Line
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
        cr.set_line_width(2.0)
        cr.move_to(margin_x, horizon_y)
        cr.line_to(width - margin_x, horizon_y)
        cr.stroke()

        # 2. Solar Arc Curve (Parabolic trajectory)
        peak_y = 25.0
        cr.set_source_rgba(0.95, 0.72, 0.2, 0.8)  # Warm Amber / Golden
        cr.set_line_width(3.0)

        steps = 60
        for i in range(steps + 1):
            fraction = i / steps  # 0.0 to 1.0
            x = margin_x + fraction * arc_width
            # Parabola peaking at fraction = 0.5
            norm_x = (fraction - 0.5) * 2.0  # -1.0 to 1.0
            y = peak_y + (1.0 - (1.0 - norm_x * norm_x)) * (horizon_y - peak_y)
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()

        # 3. Sun marker at current time (if within daylight window)
        now = datetime.now(self.day_plan.sunrise.tzinfo)
        sunrise = self.day_plan.sunrise
        sunset = self.day_plan.sunset

        if sunrise <= now <= sunset:
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

        # 4. Labels for Sunrise and Sunset
        cr.set_source_rgba(0.7, 0.7, 0.7, 0.9)
        cr.set_font_size(11.0)

        sunrise_text = f"☼ {sunrise.strftime('%I:%M %p').lstrip('0')}"
        cr.move_to(margin_x, horizon_y + 18)
        cr.show_text(sunrise_text)

        sunset_text = f"☾ {sunset.strftime('%I:%M %p').lstrip('0')}"
        cr.move_to(width - margin_x - 65, horizon_y + 18)
        cr.show_text(sunset_text)

        noon_text = f"Noon"
        cr.move_to(center_x - 14, peak_y - 8)
        cr.show_text(noon_text)
