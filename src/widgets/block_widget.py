"""Block widget representing a discrete 30-minute Thirty block in GTK 4."""

from __future__ import annotations

from datetime import datetime
from gi.repository import Gtk, Adw, Pango

from thirties_core.models import BlockKind, ThirtyBlock


class BlockWidget(Gtk.Box):
    """Visual pill representing a single Thirty block."""

    def __init__(self, block: ThirtyBlock, is_current: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.block = block
        self.is_current = is_current

        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(3)
        self.set_margin_bottom(3)

        self.add_css_class("card")
        self.add_css_class("block-pill")

        self._build_ui()
        self._apply_styling()

    def _build_ui(self) -> None:
        # 1. Index and Time Column
        time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        time_box.set_size_request(80, -1)

        idx_label = Gtk.Label(label=f"#{self.block.index:02d}")
        idx_label.add_css_class("caption")
        idx_label.add_css_class("dim-label")
        idx_label.set_xalign(0.0)

        start_time_str = self.block.start_dt.strftime("%I:%M %p").lstrip("0")
        time_label = Gtk.Label(label=start_time_str)
        time_label.add_css_class("heading")
        time_label.set_xalign(0.0)

        time_box.append(idx_label)
        time_box.append(time_label)
        self.append(time_box)

        # 2. Kind / Solar Indicator Icon
        icon_name = self._get_icon_name()
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(20)
        self.append(icon)

        # 3. Label / Assignment Details
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content_box.set_hexpand(True)

        display_title = self._get_display_title()
        title_label = Gtk.Label(label=display_title)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_xalign(0.0)
        title_label.add_css_class("body")

        end_time_str = self.block.end_dt.strftime("%I:%M %p").lstrip("0")
        range_str = f"{start_time_str} – {end_time_str}"
        subtitle_label = Gtk.Label(label=range_str)
        subtitle_label.add_css_class("caption")
        subtitle_label.add_css_class("dim-label")
        subtitle_label.set_xalign(0.0)

        content_box.append(title_label)
        content_box.append(subtitle_label)
        self.append(content_box)

        # 4. Status Badge if locked, assigned, or ambiguous
        badge = self._build_status_badge()
        if badge:
            self.append(badge)

    def _get_icon_name(self) -> str:
        kind = self.block.kind
        if kind == BlockKind.SLEEP:
            return "night-light-symbolic"
        elif kind == BlockKind.WORK:
            return "view-grid-symbolic"
        elif kind == BlockKind.BUSY_CALENDAR:
            return "x-office-calendar-symbolic"
        elif kind == BlockKind.AMBIGUOUS_CALENDAR:
            return "dialog-question-symbolic"
        elif kind == BlockKind.ASSIGNED:
            return "starred-symbolic"
        else:
            if self.block.is_sunlight:
                return "weather-clear-symbolic"
            elif self.block.is_twilight:
                return "weather-few-clouds-symbolic"
            else:
                return "weather-clear-night-symbolic"

    def _get_display_title(self) -> str:
        if self.block.label:
            return self.block.label

        kind = self.block.kind
        if kind == BlockKind.SLEEP:
            return "Sleep"
        elif kind == BlockKind.WORK:
            return "Work"
        elif kind == BlockKind.DAYLIGHT_DISCRETIONARY:
            return "Daylight Discretionary"
        elif kind == BlockKind.DARK_DISCRETIONARY:
            return "Dark Discretionary"
        elif kind == BlockKind.ASSIGNED:
            return "Assigned Task"
        elif kind == BlockKind.AMBIGUOUS_CALENDAR:
            return "Unconfirmed Event"
        return "Discretionary"

    def _build_status_badge(self) -> Gtk.Widget | None:
        if self.block.kind == BlockKind.AMBIGUOUS_CALENDAR:
            pill = Gtk.Label(label="Needs Action")
            pill.add_css_class("badge-warning")
            return pill
        elif self.block.kind == BlockKind.ASSIGNED:
            pill = Gtk.Label(label="Allocated")
            pill.add_css_class("badge-assigned")
            return pill
        elif self.is_current:
            pill = Gtk.Label(label="Now")
            pill.add_css_class("badge-current")
            return pill
        return None

    def _apply_styling(self) -> None:
        if self.block.is_sunlight:
            self.add_css_class("block-sunlight")
        else:
            self.add_css_class("block-dark")

        if self.block.is_locked:
            self.add_css_class("block-locked")

        if self.block.kind == BlockKind.ASSIGNED:
            self.add_css_class("block-assigned")

        if self.is_current:
            self.add_css_class("block-current")
