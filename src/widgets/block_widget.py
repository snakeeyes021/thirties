"""Block widgets representing discrete and collapsed Thirty blocks in GTK 4."""

from __future__ import annotations

from gi.repository import Gtk, Adw, Pango

from thirties_core.models import EventSpan, SpanCategory, ThirtyBlock


class BlockWidget(Gtk.Box):
    """Visual pill representing a single Thirty block."""

    def __init__(
        self,
        block: ThirtyBlock,
        span: EventSpan | None = None,
        is_current: bool = False,
        display_index: int = -1,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.block = block
        self.span = span
        self.is_current = is_current
        self.display_index = display_index

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

        idx_num = self.display_index if self.display_index > 0 else (self.block.index + 1)
        idx_label = Gtk.Label(label=f"#{idx_num:02d}")
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

    @property
    def _is_assigned(self) -> bool:
        return self.span is not None and not self.span.is_envelope

    def _get_icon_name(self) -> str:
        span = self.span
        if span is None:
            if self.block.is_sunlight:
                return "weather-clear-symbolic"
            if self.block.is_twilight:
                return "weather-few-clouds-symbolic"
            return "weather-clear-night-symbolic"
        return {
            SpanCategory.SLEEP: "night-light-symbolic",
            SpanCategory.WORK: "view-grid-symbolic",
            SpanCategory.CALENDAR: "x-office-calendar-symbolic",
        }.get(span.category, "starred-symbolic")

    def _get_display_title(self) -> str:
        if self.span is not None:
            return self.span.title
        return "Open Daylight Thirty" if self.block.is_sunlight else "Open Dark Thirty"

    def _build_status_badge(self) -> Gtk.Widget | None:
        if self._is_assigned:
            pill = Gtk.Label(label="Planned")
            pill.add_css_class("badge-assigned")
            return pill
        if self.is_current:
            pill = Gtk.Label(label="Now")
            pill.add_css_class("badge-current")
            return pill
        return None

    def _apply_styling(self) -> None:
        self.add_css_class("block-sunlight" if self.block.is_sunlight else "block-dark")
        if self.span is not None and (self.span.is_envelope or self.span.is_firm):
            self.add_css_class("block-locked")
        if self._is_assigned:
            self.add_css_class("block-assigned")
        if self.is_current:
            self.add_css_class("block-current")


class GroupedBlockWidget(Gtk.Box):
    """Consolidated card for the thirties under one work or sleep span, with optional drilldown."""

    def __init__(
        self,
        span: EventSpan,
        blocks_with_indices: list[tuple[ThirtyBlock, int]],
        now_block_idx: int = -1,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.span = span
        self.items = blocks_with_indices
        self.blocks = [b for b, _ in blocks_with_indices]
        self.indices = [idx for _, idx in blocks_with_indices]
        self.is_sleep = span.category == SpanCategory.SLEEP
        self.now_block_idx = now_block_idx
        self.is_current = any(b.index == self.now_block_idx for b in self.blocks)

        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self.add_css_class("card")
        self.add_css_class("block-grouped")

        if self.is_current:
            self.add_css_class("block-current")

        self._build_ui()

    def _build_ui(self) -> None:
        # Header Row
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.set_margin_start(12)
        header_row.set_margin_end(12)
        header_row.set_margin_top(10)
        header_row.set_margin_bottom(10)

        # 1. Logical Range Pill (e.g. #33–#48)
        start_idx = self.indices[0]
        end_idx = self.indices[-1]
        range_tag = f"#{start_idx:02d}–#{end_idx:02d}"
        count_pill = Gtk.Label(label=range_tag)
        count_pill.add_css_class("caption")
        count_pill.add_css_class("dim-label")
        count_pill.set_size_request(80, -1)
        count_pill.set_xalign(0.0)
        header_row.append(count_pill)

        # 2. Icon
        icon_name = "night-light-symbolic" if self.is_sleep else "view-grid-symbolic"
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(20)
        header_row.append(icon)

        # 3. Title & Range Description
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)

        title_text = self.span.title
        title_lbl = Gtk.Label(label=title_text)
        title_lbl.add_css_class("heading")
        title_lbl.set_xalign(0.0)

        start_str = self.span.start_dt.strftime("%I:%M %p").lstrip("0")
        end_str = self.span.end_dt.strftime("%I:%M %p").lstrip("0")
        total_hours = self.span.duration_minutes / 60
        range_lbl = Gtk.Label(label=f"{start_str} – {end_str} ({total_hours:.1f} hours, {len(self.blocks)} Thirties)")
        range_lbl.add_css_class("caption")
        range_lbl.add_css_class("dim-label")
        range_lbl.set_xalign(0.0)

        title_box.append(title_lbl)
        title_box.append(range_lbl)
        header_row.append(title_box)

        # 4. "Now" indicator if current time is inside this collapsed block
        if self.is_current:
            now_badge = Gtk.Label(label="Now")
            now_badge.add_css_class("badge-current")
            header_row.append(now_badge)

        # 5. Expand / Collapse Button
        self.toggle_btn = Gtk.Button(icon_name="pan-down-symbolic")
        self.toggle_btn.add_css_class("flat")
        self.toggle_btn.set_tooltip_text("Expand individual thirties")
        self.toggle_btn.connect("clicked", self._on_toggle)
        header_row.append(self.toggle_btn)

        self.append(header_row)

        # 6. Collapsible Revealer containing individual pills with logical numbering
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_reveal_child(False)

        sub_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sub_list.set_margin_start(16)
        sub_list.set_margin_end(8)
        sub_list.set_margin_bottom(8)

        for b, disp_idx in self.items:
            is_cur = (b.index == self.now_block_idx)
            sub_list.append(BlockWidget(b, span=self.span, is_current=is_cur, display_index=disp_idx))

        self.revealer.set_child(sub_list)
        self.append(self.revealer)

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        is_revealed = self.revealer.get_reveal_child()
        self.revealer.set_reveal_child(not is_revealed)
        self.toggle_btn.set_icon_name("pan-up-symbolic" if not is_revealed else "pan-down-symbolic")
