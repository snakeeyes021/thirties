"""Daily schedule view displaying the Solar Arc and consolidated big-time blocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from gi.repository import Gtk, Adw, GLib, Graphene

from thirties_core.astronomy import get_current_time
from thirties_core.models import DayPlan, EventSpan, ThirtyBlock
from thirties.widgets.block_widget import BlockWidget, GroupedBlockWidget, format_clock
from thirties.widgets.solar_arc import SolarArcWidget


@dataclass
class Segment:
    """Consecutive thirties that show the same thing (or one open thirty)."""
    blocks: list[ThirtyBlock]
    spans: list[EventSpan] = field(default_factory=list)  # what fills it; empty when open
    is_envelope: bool = False

    @property
    def key(self) -> tuple:
        return (tuple(s.id for s in self.spans), self.blocks[0].index)


def _occupancy(plan: DayPlan, block: ThirtyBlock) -> tuple[bool, list[EventSpan]]:
    """What a thirty shows: its non-envelope spans if any, otherwise the envelope under it."""
    occupants = plan.spans_overlapping(block.start_dt, block.end_dt)
    foreground = [s for s in occupants if not s.is_envelope]
    if foreground:
        return False, foreground
    span = plan.display_span(block)
    return True, [span] if span else []


def build_segments(plan: DayPlan) -> list[Segment]:
    """Group the day's thirties into cards.

    Consecutive thirties with exactly the same occupants become one segment, so
    a two-thirty dinner is one card, and "Dinner + Audiobook" is its own card for
    the thirties where both happen. Anything planned on top of work or sleep
    splits the envelope around it, keeping the list in clock order. Open thirties
    stay individual: each is a slot to plan.
    """
    segments: list[Segment] = []
    for block in plan.blocks:
        is_envelope, spans = _occupancy(plan, block)
        prev = segments[-1] if segments else None
        if (spans and prev is not None and prev.spans
                and [s.id for s in prev.spans] == [s.id for s in spans]):
            prev.blocks.append(block)
        else:
            segments.append(Segment([block], spans, is_envelope))
    return segments


class DayView(Gtk.Box):
    """Container for the daily schedule timeline with collapsed locked chunks."""

    def __init__(self, day_plan: DayPlan | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.day_plan = day_plan
        self.today: date | None = None  # the active diurnal date, set by the window
        self._rows: list[Gtk.Widget] = []
        self._signature = None
        self._now_idx = -1
        self._expanded: set[tuple] = set()

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

        self.daylight_pill = Gtk.Label(label="☼ Daylight: 0/0 available")
        self.daylight_pill.add_css_class("badge-daylight")

        self.dark_pill = Gtk.Label(label="☾ Dark: 0/0 available")
        self.dark_pill.add_css_class("badge-dark")

        tallies_box.append(self.daylight_pill)
        tallies_box.append(self.dark_pill)
        header_card.append(tallies_box)

        self.append(header_card)

    def _build_blocks_list(self) -> None:
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(750)
        clamp.set_tightening_threshold(550)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.list_box.set_margin_top(6)
        self.list_box.set_margin_bottom(24)
        self.list_box.set_margin_start(12)
        self.list_box.set_margin_end(12)

        clamp.set_child(self.list_box)
        self.scrolled.set_child(clamp)
        self.append(self.scrolled)

    # --- time ---

    def live_now(self) -> datetime | None:
        """The current time if it falls on this plan's canvas (the plan is today's), else None."""
        plan = self.day_plan
        if plan is None:
            return None
        now = get_current_time(plan.sunrise.tzinfo)
        if plan.day_start.timestamp() <= now.timestamp() < plan.day_end.timestamp():
            return now
        return None

    def _current_index(self) -> int:
        now = self.live_now()
        block = self.day_plan.block_at(now) if now is not None else None
        return block.index if block else -1

    # --- rendering ---

    def set_today(self, today: date) -> None:
        if today != self.today:
            self.today = today
            if self.day_plan is not None:
                self._update_title()

    def _update_title(self) -> None:
        today = self.today or date.today()
        target = self.day_plan.target_date
        if target == today:
            date_str = f"Today — {target.strftime('%A, %B %d')}"
        elif target == today - timedelta(days=1):
            date_str = f"Yesterday — {target.strftime('%A, %B %d')}"
        elif target == today + timedelta(days=1):
            date_str = f"Tomorrow — {target.strftime('%A, %B %d')}"
        else:
            date_str = target.strftime("%A, %B %d, %Y")
        self.date_title.set_text(date_str)

    def _update_tallies(self) -> None:
        # Today counts only the thirties still ahead; other days count them all.
        counts = self.day_plan.availability(self.live_now())
        self.daylight_pill.set_text(f"☼ Daylight: {counts['daylight_open']}/{counts['daylight_total']} available")
        self.dark_pill.set_text(f"☾ Dark: {counts['dark_open']}/{counts['dark_total']} available")

    @staticmethod
    def _plan_signature(plan: DayPlan) -> tuple:
        return (plan.target_date, tuple(tuple(sorted(s.to_dict().items())) for s in plan.spans))

    def refresh_plan(self, day_plan: DayPlan, force: bool = False) -> None:
        """Show a plan. Rebuilds the list only when the plan's content changed."""
        same_day = self.day_plan is not None and self.day_plan.target_date == day_plan.target_date
        if not same_day:
            self._expanded.clear()
        self.day_plan = day_plan
        self.solar_arc.set_day_plan(day_plan)
        self._update_title()

        signature = self._plan_signature(day_plan)
        if not force and signature == self._signature:
            self.tick()
            return
        self._signature = signature
        self._rebuild_list()
        self._update_tallies()

    def tick(self) -> None:
        """Advance the live "now": arc marker, gold highlight and remaining-time tallies."""
        if self.day_plan is None:
            return
        self.solar_arc.queue_draw()
        self._update_tallies()
        now_idx = self._current_index()
        if now_idx != self._now_idx:
            self._now_idx = now_idx
            for row in self._rows:
                row.update_now(now_idx)

    def _rebuild_list(self) -> None:
        plan = self.day_plan
        for row in self._rows:
            self.list_box.remove(row)
        self._rows = []
        self._now_idx = self._current_index()

        for seg in build_segments(plan):
            row = self._build_row(plan, seg)
            self._rows.append(row)
            self.list_box.append(row)

    def _build_row(self, plan: DayPlan, seg: Segment) -> Gtk.Widget:
        first = seg.blocks[0]
        if len(seg.blocks) == 1:
            return BlockWidget(
                first,
                span=plan.display_span(first),
                is_current=(first.index == self._now_idx),
                display_index=first.number,
                title=plan.display_title(first),
            )

        start = max(first.start_dt, min(s.start_dt for s in seg.spans))
        end = min(seg.blocks[-1].end_dt, max(s.end_dt for s in seg.spans))
        context = None
        if len(seg.spans) == 1:
            span = seg.spans[0]
            if span.start_dt < start or span.end_dt > end:
                context = f"Part of {span.title}, {format_clock(span.start_dt)} – {format_clock(span.end_dt)}"
        key = seg.key
        return GroupedBlockWidget(
            plan.display_span(first),
            [(b, b.number) for b in seg.blocks],
            now_block_idx=self._now_idx,
            title=plan.display_title(first),
            start_dt=start,
            end_dt=end,
            context=context,
            expanded=key in self._expanded,
            on_expanded_changed=lambda _w, expanded, key=key: self._on_expanded_changed(key, expanded),
        )

    def _on_expanded_changed(self, key: tuple, expanded: bool) -> None:
        if expanded:
            self._expanded.add(key)
        else:
            self._expanded.discard(key)

    def scroll_to_now(self) -> None:
        """Bring the current thirty into view once the list has been laid out (other days: the top)."""
        attempts = [0]

        def do_scroll() -> bool:
            row = next((r for r in self._rows if r.is_current), None)
            if row is None:
                self.scrolled.get_vadjustment().set_value(0.0)
                return False
            attempts[0] += 1
            adj = self.scrolled.get_vadjustment()
            ok, point = row.compute_point(self.list_box, Graphene.Point())
            if not ok or row.get_height() == 0 or adj.get_page_size() == 0:
                return attempts[0] < 20  # not laid out yet; try again shortly
            adj.set_value(max(0.0, min(point.y - 24, adj.get_upper() - adj.get_page_size())))
            return False
        GLib.timeout_add(100, do_scroll)
