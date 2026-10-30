"""Daily schedule view displaying the Solar Arc and consolidated big-time blocks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

from thirties_core.astronomy import get_current_time
from thirties_core.day_layout import (Segment, build_segments, card_rows, segment_context, segment_title,  # noqa: F401
                                      status_label)
from thirties_core.models import _DISPLAY_PRIORITY, DayPlan
from thirties.widgets.block_widget import BlockWidget, GroupedBlockWidget, format_clock
from thirties.widgets.solar_arc import SolarArcWidget


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
        self._build_pages()

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

    def _build_pages(self) -> None:
        """Day (where things stand: now and next, not reported, done) and Timeline (all 48 thirties)."""
        from thirties.views.agenda import AgendaPage
        self.run_tool = None
        self.agenda = AgendaPage(run_tool=lambda name, args: self.run_tool(name, args) if self.run_tool else "",
                                 on_replan=lambda result: self.on_replan(result) if self.on_replan else None)
        self.on_replan = None
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled_with_icon(self.agenda, "day", "Day", "view-list-symbolic")
        self._build_blocks_list()
        self.stack.add_titled_with_icon(self.scrolled, "timeline", "Timeline", "x-office-calendar-symbolic")
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.CENTER)
        self.append(switcher)
        self.append(self.stack)

    def set_actions(self, run_tool, on_replan=None) -> None:
        """``run_tool(name, args)`` runs a schedule tool (and refreshes); ``on_replan(result)`` after Replan."""
        self.run_tool, self.on_replan = run_tool, on_replan

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
        self.agenda.refresh(day_plan, self.live_now())
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
            self.agenda.refresh(self.day_plan, self.live_now())  # a new thirty: now and next move on

    def _rebuild_list(self) -> None:
        plan = self.day_plan
        for row in self._rows:
            self.list_box.remove(row)
        self._rows = []
        self._now_idx = self._current_index()

        for seg in build_segments(plan):
            row = self._build_row(plan, seg)
            self._make_draggable(row, seg)
            self._rows.append(row)
            self.list_box.append(row)

    # --- moving things by dragging them on the timeline ---

    def _make_draggable(self, row: Gtk.Widget, seg: Segment) -> None:
        movable = [s for s in seg.spans if not s.is_envelope and s.source != "calendar" and not s.tentative]
        if len(movable) == 1:
            source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            span_id = movable[0].id
            source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(span_id))
            row.add_controller(source)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        start = seg.blocks[0].start_dt
        target.connect("drop", lambda _t, value, _x, _y: self.move_to(value, start))
        row.add_controller(target)

    def move_to(self, span_id: str, start: datetime) -> bool:
        """Move an item to a thirty. What's there stays and shares the time: moving one item never moves another
        (item 17)."""
        if self.run_tool is None:
            return False
        self.run_tool("update_event", {"event": span_id, "start": format_clock(start)})
        return True

    def _build_row(self, plan: DayPlan, seg: Segment) -> Gtk.Widget:
        first = seg.blocks[0]
        items = [s for s in seg.spans if not s.is_envelope]
        title = segment_title(plan, seg)
        context = segment_context(seg)
        if len(seg.blocks) == 1 and len(items) <= 1:
            return BlockWidget(
                first,
                span=plan.display_span(first),
                is_current=(first.index == self._now_idx),
                display_index=first.number,
                title=title,
                status=status_label(items),
                context=context,
            )
        lead = min(items, key=lambda s: (_DISPLAY_PRIORITY[s.category], s.start_dt)) if items else seg.spans[0]
        key = seg.key
        return GroupedBlockWidget(
            lead,
            [(b, b.number) for b in seg.blocks],
            now_block_idx=self._now_idx,
            title=title,
            start_dt=seg.start_dt,
            end_dt=seg.end_dt,
            context=context,
            rows=card_rows(plan, seg),
            status=status_label(items),
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
