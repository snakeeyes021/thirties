"""Daily schedule view displaying the Solar Arc and consolidated big-time blocks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from gi.repository import Gtk, Adw

from thirties_core.astronomy import get_current_time
from thirties_core.models import BlockKind, DayPlan, ThirtyBlock
from thirties.widgets.block_widget import BlockWidget, GroupedBlockWidget
from thirties.widgets.solar_arc import SolarArcWidget


class DayView(Gtk.Box):
    """Container for the daily schedule timeline with collapsed locked chunks."""

    def __init__(self, day_plan: DayPlan | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.day_plan = day_plan

        self.set_hexpand(True)
        self.set_vexpand(True)

        self._build_header()
        self._build_blocks_list()

        if self.day_plan:
            self.refresh_plan(self.day_plan)

    def _build_header(self) -> None:
        header_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header_card.set_margin_start(16)
        header_card.set_margin_end(16)
        header_card.set_margin_top(10)
        header_card.set_margin_bottom(6)
        header_card.add_css_class("card")

        # Date title placed cleanly at top of schedule
        self.date_title = Gtk.Label(label="Today")
        self.date_title.add_css_class("title-3")
        self.date_title.set_margin_top(8)
        self.date_title.set_halign(Gtk.Align.CENTER)
        header_card.append(self.date_title)

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
        clamp.set_maximum_size(750)
        clamp.set_tightening_threshold(550)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.list_box.set_margin_top(6)
        self.list_box.set_margin_bottom(24)
        self.list_box.set_margin_start(12)
        self.list_box.set_margin_end(12)

        clamp.set_child(self.list_box)
        scrolled.set_child(clamp)
        self.append(scrolled)

    def refresh_plan(self, day_plan: DayPlan) -> None:
        self.day_plan = day_plan
        self.solar_arc.set_day_plan(day_plan)

        # Update date title
        today = date.today()
        target = day_plan.target_date
        if target == today:
            date_str = f"Today — {target.strftime('%A, %B %d')}"
        elif target == today - timedelta(days=1):
            date_str = f"Yesterday — {target.strftime('%A, %B %d')}"
        elif target == today + timedelta(days=1):
            date_str = f"Tomorrow — {target.strftime('%A, %B %d')}"
        else:
            date_str = target.strftime("%A, %B %d, %Y")
        self.date_title.set_text(date_str)

        # Update tallies badges (Available / Total Discretionary)
        self.daylight_pill.set_text(f"☼ Daylight: {day_plan.daylight_available_count}/{day_plan.daylight_discretionary_total} Available")
        self.dark_pill.set_text(f"☾ Dark: {day_plan.dark_available_count}/{day_plan.dark_discretionary_total} Available")

        # Clear existing list items
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)

        now = get_current_time(day_plan.sunrise.tzinfo)
        now_idx = -1
        for b in day_plan.blocks:
            if b.start_dt <= now < b.end_dt:
                now_idx = b.index
                break

        # Natural Day Flow: Start day at sunrise (Block #01)
        sunrise_idx = next((b.index for b in day_plan.blocks if b.is_sunlight), 14)

        ordered_items: list[tuple[ThirtyBlock, int]] = [
            (day_plan.get_block((sunrise_idx + i) % 48), i + 1) for i in range(48)
        ]

        # Group contiguous locked Sleep and Work blocks
        current_run: list[tuple[ThirtyBlock, int]] = []
        current_kind: BlockKind | None = None

        def flush_run():
            nonlocal current_run, current_kind
            if not current_run:
                return
            if current_kind in (BlockKind.SLEEP, BlockKind.WORK) and len(current_run) > 1:
                self.list_box.append(GroupedBlockWidget(current_run, now_block_idx=now_idx))
            else:
                for b, disp_idx in current_run:
                    self.list_box.append(BlockWidget(b, is_current=(b.index == now_idx), display_index=disp_idx))
            current_run = []
            current_kind = None

        for block, disp_idx in ordered_items:
            is_collapsible = block.is_locked and block.kind in (BlockKind.SLEEP, BlockKind.WORK)

            if is_collapsible:
                if current_kind == block.kind:
                    current_run.append((block, disp_idx))
                else:
                    flush_run()
                    current_kind = block.kind
                    current_run = [(block, disp_idx)]
            else:
                flush_run()
                self.list_box.append(BlockWidget(block, is_current=(block.index == now_idx), display_index=disp_idx))

        flush_run()
