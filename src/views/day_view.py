"""The schedule pane: the solar arc header (the day's date, the sun's arc and the daylight/dark tallies), then the
Agenda (where the day stands: agenda.py) and the Timeline page (timeline.py).

The old block-card timeline was replaced by the Timeline page on 10-30; the solar arc header stays ("put that
back. That was beautiful.", the user, 10-30).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from gi.repository import Adw, Gtk

from thirties_core.astronomy import get_current_time
from thirties_core.kinds import ONGOING, kind_by_wording
from thirties_core.models import DayPlan
from thirties_core.clock import thirty_of
from thirties.widgets.solar_arc import SolarArcWidget


class DayView(Gtk.Box):
    """Day and Timeline pages for one day's plan."""

    def __init__(self, day_plan: DayPlan | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.day_plan = day_plan
        self.today: date | None = None  # the active diurnal date, set by the window
        self._signature = None
        self._now_thirty = None
        # () -> (span -> bool): which items are ongoing (a done session reads "worked on"); the window gives the
        # assistant's reading (list roles, the user's word), read once per redraw. Until then, the wording.
        self.ongoing_provider = None
        self._ongoing = lambda span: kind_by_wording(span.title) == ONGOING
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._build_header()
        self._build_pages()
        if self.day_plan:
            self.refresh_plan(self.day_plan)

    def _build_header(self) -> None:
        """The date, the sun's arc over the day, and how much daylight and dark is still open."""
        header_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=16, margin_end=16,
                              margin_top=10, margin_bottom=6)
        header_card.add_css_class("card")
        self.date_title = Gtk.Label(label="Today", margin_top=8, halign=Gtk.Align.CENTER)
        self.date_title.add_css_class("title-3")
        header_card.append(self.date_title)
        self.solar_arc = SolarArcWidget(self.day_plan)
        header_card.append(self.solar_arc)
        tallies = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16, halign=Gtk.Align.CENTER,
                          margin_bottom=8)
        self.daylight_pill = Gtk.Label(label="☼ Daylight: 0/0 available")
        self.daylight_pill.add_css_class("badge-daylight")
        self.dark_pill = Gtk.Label(label="☾ Dark: 0/0 available")
        self.dark_pill.add_css_class("badge-dark")
        tallies.append(self.daylight_pill)
        tallies.append(self.dark_pill)
        header_card.append(tallies)
        self.append(header_card)

    def _update_title(self) -> None:
        today = self.today or date.today()
        target = self.day_plan.target_date
        if target == today:
            text = f"Today — {target.strftime('%A, %B %d')}"
        elif target == today - timedelta(days=1):
            text = f"Yesterday — {target.strftime('%A, %B %d')}"
        elif target == today + timedelta(days=1):
            text = f"Tomorrow — {target.strftime('%A, %B %d')}"
        else:
            text = target.strftime("%A, %B %d, %Y")
        self.date_title.set_text(text)

    def _update_tallies(self) -> None:
        # Today counts only the thirties still ahead; other days count them all.
        counts = self.day_plan.availability(self.live_now())
        self.daylight_pill.set_text(f"☼ Daylight: {counts['daylight_open']}/{counts['daylight_total']} available")
        self.dark_pill.set_text(f"☾ Dark: {counts['dark_open']}/{counts['dark_total']} available")

    def _build_pages(self) -> None:
        """Day (where things stand: now and next, not reported, done) and Timeline (the day and the week)."""
        from thirties.views.agenda import AgendaPage
        from thirties.views.timeline import TimelinePage
        self.run_tool = None
        self.agenda = AgendaPage(run_tool=lambda name, args: self.run_tool(name, args) if self.run_tool else "",
                                 on_replan=lambda result: self.on_replan(result) if self.on_replan else None)
        self.on_replan = None
        self.agenda.ongoing = self._ongoing
        self.timeline = TimelinePage()
        self.timeline.run_tool = lambda name, args: self.run_tool(name, args) if self.run_tool else ""
        self.timeline.ongoing = self._ongoing
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled_with_icon(self.agenda, "day", "Agenda", "view-list-symbolic")
        self.stack.add_titled_with_icon(self.timeline, "timeline", "Timeline", "x-office-calendar-symbolic")
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.CENTER,
                                    margin_top=6)
        self.append(switcher)
        self.append(self.stack)

    def set_actions(self, run_tool, on_replan=None, on_notice=None, routines=None, ongoing=None) -> None:
        """``run_tool(name, args)`` runs a schedule tool (and refreshes); ``on_replan(result)`` after Replan;
        ``on_notice(text)`` shows a short result (a skip); ``routines(plan)`` lists the day's routines with their
        skip choices (RoutineTools.day_routines); ``ongoing()`` gives a span -> bool for ongoing items."""
        self.run_tool, self.on_replan = run_tool, on_replan
        self.agenda.on_notice, self.agenda.routines = on_notice, routines
        self.ongoing_provider = ongoing

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

    # --- rendering ---

    def set_today(self, today: date) -> None:
        if today != self.today:
            self.today = today
            self.timeline.today = today

    @staticmethod
    def _plan_signature(plan: DayPlan) -> tuple:
        return (plan.target_date, tuple(tuple(sorted(s.to_dict().items())) for s in plan.spans))

    def refresh_plan(self, day_plan: DayPlan, force: bool = False) -> None:
        """Show a plan. Redraws only when the plan's content changed."""
        self.day_plan = day_plan
        self.solar_arc.set_day_plan(day_plan)
        self._update_title()
        self._update_tallies()
        signature = self._plan_signature(day_plan)
        if not force and signature == self._signature:
            self.timeline.sync_units()
            self.tick()
            return
        self._signature = signature
        self._read_kinds()
        now = self.live_now()
        self._now_thirty = thirty_of(now) if now else None
        self.agenda.refresh(day_plan, now)
        self.timeline.refresh(day_plan, self.today)

    def _read_kinds(self) -> None:
        if self.ongoing_provider is None:
            return
        try:
            self._ongoing = self.ongoing_provider()
        except Exception:  # the wording still reads the obvious cases
            self._ongoing = lambda span: kind_by_wording(span.title) == ONGOING
        self.agenda.ongoing = self._ongoing
        self.timeline.ongoing = self._ongoing

    def tick(self) -> None:
        """The clock moved: the hour hand follows; a new thirty moves now and next on."""
        if self.day_plan is None:
            return
        self.solar_arc.queue_draw()
        self._update_tallies()
        self.timeline.tick()
        now = self.live_now()
        thirty = thirty_of(now) if now else None
        if thirty != self._now_thirty:
            self._now_thirty = thirty
            self.agenda.refresh(self.day_plan, now)

    def move_to(self, span_id: str, start: datetime) -> bool:
        """Move an item to a thirty (dragged on the timeline). What's there stays and shares the time (item 17)."""
        return self.timeline.move_to(span_id, start)

    def scroll_to_now(self) -> None:
        self.timeline.scroll_to_now()
