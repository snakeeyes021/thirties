"""Block widgets representing discrete and collapsed Thirty blocks in GTK 4."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from gi.repository import Gtk, Pango

from thirties_core.models import EventSpan, SpanCategory, ThirtyBlock


def format_clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def format_duration(minutes: int) -> str:
    """Human duration: "30 min", "1 h", "1 h 30 min"."""
    hours, mins = divmod(max(0, minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def span_icon_name(span: EventSpan | None, block: ThirtyBlock) -> str:
    if span is None:
        if block.is_sunlight:
            return "weather-clear-symbolic"
        if block.is_twilight:
            return "weather-few-clouds-symbolic"
        return "weather-clear-night-symbolic"
    return {
        SpanCategory.SLEEP: "night-light-symbolic",
        SpanCategory.WORK: "view-grid-symbolic",
        SpanCategory.CALENDAR: "x-office-calendar-symbolic",
    }.get(span.category, "starred-symbolic")


class BlockWidget(Gtk.Box):
    """Visual pill representing a single Thirty block."""

    def __init__(
        self,
        block: ThirtyBlock,
        span: EventSpan | None = None,
        is_current: bool = False,
        display_index: int = -1,
        title: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.block = block
        self.span = span
        self.is_current = False
        self.display_index = display_index
        self.title = title

        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(3)
        self.set_margin_bottom(3)

        self.add_css_class("card")
        self.add_css_class("block-pill")

        self._build_ui()
        self._apply_styling()
        self.set_current(is_current)

    def _build_ui(self) -> None:
        # 1. Index and Time Column
        time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        time_box.set_size_request(80, -1)

        idx_num = self.display_index if self.display_index > 0 else (self.block.index + 1)
        idx_label = Gtk.Label(label=f"#{idx_num:02d}")
        idx_label.add_css_class("caption")
        idx_label.add_css_class("dim-label")
        idx_label.set_xalign(0.0)

        start_time_str = format_clock(self.block.start_dt)
        time_label = Gtk.Label(label=start_time_str)
        time_label.add_css_class("heading")
        time_label.set_xalign(0.0)

        time_box.append(idx_label)
        time_box.append(time_label)
        self.append(time_box)

        # 2. Kind / Solar Indicator Icon
        icon = Gtk.Image.new_from_icon_name(span_icon_name(self.span, self.block))
        icon.set_pixel_size(20)
        self.append(icon)

        # 3. Label / Assignment Details
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content_box.set_hexpand(True)

        self.title_label = Gtk.Label(label=self._get_display_title())
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_xalign(0.0)
        self.title_label.add_css_class("body")

        range_str = f"{start_time_str} – {format_clock(self.block.end_dt)}"
        subtitle_label = Gtk.Label(label=range_str)
        subtitle_label.add_css_class("caption")
        subtitle_label.add_css_class("dim-label")
        subtitle_label.set_xalign(0.0)

        content_box.append(self.title_label)
        content_box.append(subtitle_label)
        self.append(content_box)

        # 4. Status badges: "Now" while current, otherwise "Planned" for assigned thirties
        self.now_badge = Gtk.Label(label="Now")
        self.now_badge.add_css_class("badge-current")
        self.now_badge.set_valign(Gtk.Align.CENTER)
        self.append(self.now_badge)

        self.planned_badge = None
        if self._is_assigned:
            self.planned_badge = Gtk.Label(label="Planned")
            self.planned_badge.add_css_class("badge-assigned")
            self.planned_badge.set_valign(Gtk.Align.CENTER)
            self.append(self.planned_badge)

    @property
    def _is_assigned(self) -> bool:
        return self.span is not None and not self.span.is_envelope

    def _get_display_title(self) -> str:
        if self.title:
            return self.title
        if self.span is not None:
            return self.span.title
        return "Open daylight block" if self.block.is_sunlight else "Open dark block"

    def _apply_styling(self) -> None:
        self.add_css_class("block-sunlight" if self.block.is_sunlight else "block-dark")
        if self.span is not None and (self.span.is_envelope or self.span.is_firm):
            self.add_css_class("block-locked")
        if self._is_assigned:
            self.add_css_class("block-assigned")

    def set_current(self, is_current: bool) -> None:
        """Move the gold "now" highlight on or off this thirty without rebuilding it."""
        self.is_current = is_current
        if is_current:
            self.add_css_class("block-current")
        else:
            self.remove_css_class("block-current")
        self.now_badge.set_visible(is_current)
        if self.planned_badge is not None:
            self.planned_badge.set_visible(not is_current)

    def update_now(self, now_block_idx: int) -> None:
        self.set_current(self.block.index == now_block_idx)


class GroupedBlockWidget(Gtk.Box):
    """One collapsible card for consecutive thirties showing the same thing.

    Used for work and sleep envelopes and for anything planned that runs longer
    than one thirty (a two-thirty dinner, a three-thirty movie). Expanding it
    shows the individual thirties.
    """

    def __init__(
        self,
        span: EventSpan,
        blocks_with_indices: list[tuple[ThirtyBlock, int]],
        now_block_idx: int = -1,
        title: str | None = None,
        start_dt: datetime | None = None,
        end_dt: datetime | None = None,
        context: str | None = None,
        block_titles: list[str] | None = None,
        block_spans: list[EventSpan | None] | None = None,
        expanded: bool = False,
        on_expanded_changed: Optional[Callable[["GroupedBlockWidget", bool], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.span = span
        self.items = blocks_with_indices
        self.blocks = [b for b, _ in blocks_with_indices]
        self.indices = [idx for _, idx in blocks_with_indices]
        self.is_sleep = span.category == SpanCategory.SLEEP
        self.is_envelope = span.is_envelope
        self.title = title or span.title
        self.start_dt = start_dt or max(span.start_dt, self.blocks[0].start_dt)
        self.end_dt = end_dt or min(span.end_dt, self.blocks[-1].end_dt)
        self.context = context
        self.block_titles = block_titles
        self.block_spans = block_spans
        self.on_expanded_changed = on_expanded_changed
        self.is_current = False
        self.sub_widgets: list[BlockWidget] = []

        self.set_margin_start(8)
        self.set_margin_end(8)
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self.add_css_class("card")
        self.add_css_class("block-grouped")
        if not self.is_envelope:
            self.add_css_class("block-assigned")

        self._build_ui()
        self.update_now(now_block_idx)
        if expanded:
            self.set_expanded(True)

    def _build_ui(self) -> None:
        # Header Row
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.set_margin_start(12)
        header_row.set_margin_end(12)
        header_row.set_margin_top(10)
        header_row.set_margin_bottom(10)

        # 1. Logical Range Pill (e.g. #33–#48) above the start time
        range_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        range_box.set_size_request(80, -1)
        count_pill = Gtk.Label(label=f"#{self.indices[0]:02d}–#{self.indices[-1]:02d}")
        count_pill.add_css_class("caption")
        count_pill.add_css_class("dim-label")
        count_pill.set_xalign(0.0)
        start_lbl = Gtk.Label(label=format_clock(self.start_dt))
        start_lbl.add_css_class("heading")
        start_lbl.set_xalign(0.0)
        range_box.append(count_pill)
        range_box.append(start_lbl)
        header_row.append(range_box)

        # 2. Icon
        icon = Gtk.Image.new_from_icon_name(span_icon_name(self.span, self.blocks[0]))
        icon.set_pixel_size(20)
        header_row.append(icon)

        # 3. Title & Range Description
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)

        self.title_label = Gtk.Label(label=self.title)
        self.title_label.add_css_class("heading")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_xalign(0.0)

        minutes = int((self.end_dt.timestamp() - self.start_dt.timestamp()) // 60)
        summary = (f"{format_clock(self.start_dt)} – {format_clock(self.end_dt)} · "
                   f"{format_duration(minutes)} · {len(self.blocks)} blocks")
        self.range_label = Gtk.Label(label=summary)
        self.range_label.add_css_class("caption")
        self.range_label.add_css_class("dim-label")
        self.range_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.range_label.set_xalign(0.0)

        title_box.append(self.title_label)
        title_box.append(self.range_label)
        if self.context:
            context_lbl = Gtk.Label(label=self.context)
            context_lbl.add_css_class("caption")
            context_lbl.add_css_class("dim-label")
            context_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            context_lbl.set_xalign(0.0)
            title_box.append(context_lbl)
        header_row.append(title_box)

        # 4. "Now" indicator if current time is inside this collapsed block
        self.now_badge = Gtk.Label(label="Now")
        self.now_badge.add_css_class("badge-current")
        self.now_badge.set_valign(Gtk.Align.CENTER)
        header_row.append(self.now_badge)

        # 5. Expand / Collapse Button
        self.toggle_btn = Gtk.Button(icon_name="pan-down-symbolic")
        self.toggle_btn.add_css_class("flat")
        self.toggle_btn.set_valign(Gtk.Align.CENTER)
        self.toggle_btn.set_tooltip_text("Show each block")
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

        for i, (b, disp_idx) in enumerate(self.items):
            span = self.block_spans[i] if self.block_spans else self.span
            title = self.block_titles[i] if self.block_titles else self.title
            child = BlockWidget(b, span=span, display_index=disp_idx, title=title)
            self.sub_widgets.append(child)
            sub_list.append(child)

        self.revealer.set_child(sub_list)
        self.append(self.revealer)

    @property
    def expanded(self) -> bool:
        return self.revealer.get_reveal_child()

    def set_expanded(self, expanded: bool) -> None:
        self.revealer.set_reveal_child(expanded)
        self.toggle_btn.set_icon_name("pan-up-symbolic" if expanded else "pan-down-symbolic")
        self.toggle_btn.set_tooltip_text("Collapse" if expanded else "Show each block")

    def update_now(self, now_block_idx: int) -> None:
        """Move the gold "now" highlight without rebuilding the card."""
        self.is_current = any(b.index == now_block_idx for b in self.blocks)
        if self.is_current:
            self.add_css_class("block-current")
        else:
            self.remove_css_class("block-current")
        self.now_badge.set_visible(self.is_current)
        for child in self.sub_widgets:
            child.update_now(now_block_idx)

    def _on_toggle(self, _btn: Gtk.Button) -> None:
        self.set_expanded(not self.expanded)
        if self.on_expanded_changed:
            self.on_expanded_changed(self, self.expanded)
