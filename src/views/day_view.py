"""Diurnal Day View displaying the Solar Arc and the 48-block matrix."""

from __future__ import annotations

from datetime import datetime
from gi.repository import Gtk, Adw

from thirties_core.models import DayPlan
from thirties.widgets.block_widget import BlockWidget
from thirties.widgets.solar_arc import SolarArcWidget


class DayView(Gtk.Box):
    """Container for the diurnal timeline and the 48 Thirty pills."""

    def __init__(self, day_plan: DayPlan | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.day_plan = day_plan
        self.block_widgets: list[BlockWidget] = []

        self.set_hexpand(True)
        self.set_vexpand(True)

        self._build_header()
        self._build_blocks_list()

        if self.day_plan:
            self.refresh_plan(self.day_plan)

    def _build_header(self) -> None:
        header_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header_card.set_margin_start(12)
        header_card.set_margin_end(12)
        header_card.set_margin_top(8)
        header_card.set_margin_bottom(4)
        header_card.add_css_class("card")

        # Solar Arc drawing
        self.solar_arc = SolarArcWidget(self.day_plan)
        header_card.append(self.solar_arc)

        # Tallies bar
        tallies_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        tallies_box.set_halign(Gtk.Align.CENTER)
        tallies_box.set_margin_bottom(8)

        self.daylight_pill = Gtk.Label(label="☼ Daylight: 0 Thirties")
        self.daylight_pill.add_css_class("badge-daylight")

        self.dark_pill = Gtk.Label(label="☾ Dark: 0 Thirties")
        self.dark_pill.add_css_class("badge-dark")

        tallies_box.append(self.daylight_pill)
        tallies_box.append(self.dark_pill)
        header_card.append(tallies_box)

        self.append(header_card)

    def _build_blocks_list(self) -> None:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)
        clamp.set_tightening_threshold(500)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.list_box.set_margin_top(6)
        self.list_box.set_margin_bottom(18)
        self.list_box.set_margin_start(12)
        self.list_box.set_margin_end(12)

        clamp.set_child(self.list_box)
        scrolled.set_child(clamp)
        self.append(scrolled)

    def refresh_plan(self, day_plan: DayPlan) -> None:
        self.day_plan = day_plan
        self.solar_arc.set_day_plan(day_plan)

        # Update badges
        self.daylight_pill.set_text(f"☼ Daylight: {day_plan.daylight_available_count} Thirties")
        self.dark_pill.set_text(f"☾ Dark: {day_plan.dark_available_count} Thirties")

        # Clear existing list items
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)
        self.block_widgets.clear()

        now = datetime.now(day_plan.sunrise.tzinfo)

        # Populate 48 blocks
        for block in day_plan.blocks:
            is_current = (block.start_dt <= now < block.end_dt)
            widget = BlockWidget(block, is_current=is_current)
            self.block_widgets.append(widget)
            self.list_box.append(widget)
