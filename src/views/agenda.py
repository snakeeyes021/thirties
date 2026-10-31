"""The day at a glance: what's not marked yet, now and next, and what got done.

The plan is a sketch; this page shows where the day stands. What's past with
nothing said comes first (not assumed done; being behind is common), with buttons
to say what happened; now and next can be marked too (things get done early).
What's done shows at the times it actually happened, greyed (a session on something
ongoing reads "worked on"; what went moot unreported reads "not reported"). Replan
lays the rest of today out again (Python's redraw), with Undo. Someone else's
calendar events ("show only") and unanswered invites are shown, dimmed, but never
make the user busy. A routine due today can be skipped the three ways a
subscription can (RoutineTools.skip_choices), through the same skip_routine tool
the assistant calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from gi.repository import Adw, GLib, Gtk

from thirties_core.clock import format_clock, thirty_ceil, thirty_of
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus
from thirties_core.plan_tools import NOT_DONE

Tool = Callable[[str, dict], str]  # run a schedule tool by name; returns its result


def _flexible(span: EventSpan) -> bool:
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and not span.tentative


def _markable(span: EventSpan) -> bool:
    """Planned activities, and calendar events too: an event is something you'd mark done, and its outcome is kept
    in Thirties without touching the calendar (soak friction 1)."""
    return _flexible(span) or (span.category == SpanCategory.CALENDAR and not span.tentative and not span.shown_only)


def _takes_time(span: EventSpan) -> bool:
    """Someone else's event and an unanswered invite are shown, but the user isn't busy with them."""
    return not span.shown_only and not span.tentative


def _day_text(d: Optional[date], today: Optional[date] = None) -> str:
    if d is None:
        return "only when asked"
    if today is not None and d == today + timedelta(days=1):
        return "tomorrow"
    return d.strftime("%a %b %-d")


GUTTER = 60  # px: the block times' column, to the left of the cards


def short_clock(dt: datetime) -> str:
    """'11 AM', '5:15 PM': the minutes only when there are some (the user, 10-31: the times were too busy)."""
    return format_clock(dt).replace(":00 ", " ")


def _block_end(span: EventSpan) -> datetime:
    return thirty_ceil(span.end_dt)


class AgendaPage(Gtk.ScrolledWindow):

    def __init__(self, run_tool: Optional[Tool] = None, on_replan: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.run_tool, self.on_replan = run_tool, on_replan
        self.on_notice: Optional[Callable[[str], None]] = None
        # plan -> [{"title", "state", "choices": [{"how", "label", "back"}]}] (RoutineTools.day_routines)
        self.routines: Optional[Callable[[DayPlan], List[Dict[str, Any]]]] = None
        self.ongoing: Callable[[EventSpan], bool] = lambda span: False
        # span -> bool: set by someone else's clock (shown at its own time; the rest by their block's start)
        self.fixed: Callable[[EventSpan], bool] = lambda span: span.source == "calendar" or span.is_firm
        self.skip_menus: Dict[str, Gtk.MenuButton] = {}  # routine title -> its Skip menu (tests reach it here)
        self.read_tool: Optional[Tool] = None  # a read-only tool, run without refreshing (set by the window)
        self.page = Adw.PreferencesPage()
        self.set_child(self.page)
        self.groups: dict[str, Adw.PreferencesGroup] = {}
        self.replan_btn = Gtk.Button(label="Replan", valign=Gtk.Align.CENTER,
                                     tooltip_text="Lay the rest of today out again from now")
        self.replan_btn.add_css_class("flat")
        self.replan_btn.connect("clicked", self._on_replan)

    # --- building ---

    def _group(self, key: str, title: str, description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(group)
        self.groups[key] = group
        return group

    def refresh(self, plan: DayPlan, now: Optional[datetime]) -> None:
        for group in self.groups.values():
            if group is self.groups.get("now"):
                group.set_header_suffix(None)
            self.page.remove(group)
        self.groups = {}
        self.skip_menus = {}
        spans = plan.spans_in_view()
        if now is None:  # another day: the plan as it stands, and its record if it's past
            self._fill(self._group("plan", "The plan"), [s for s in spans if s.status == SpanStatus.PLANNED], now)
            self._fill_pencilled(plan.target_date.isoformat())
            self._fill_routines(plan)
            self._fill_done(spans)
            return
        # In blocks, as the cards say (10-31: one 11:00 block was split across Now and Next, and a 9:00 item was
        # "past" at 9:06): an item is now through its whole last thirty, and past only once that's over.
        block = thirty_of(now)
        behind = [s for s in spans if _flexible(s) and s.status == SpanStatus.PLANNED and _block_end(s) <= block]
        if behind:  # first: being behind is common, and these are what to sort out
            self._fill(self._group("behind", "Not marked yet", "Past their time with nothing said: not assumed done."),
                       behind, now, behind=True)
        current = [s for s in spans if thirty_of(s.start_dt) <= block < _block_end(s) and s.status == SpanStatus.PLANNED
                   and not (s.is_envelope and not s.start_dt <= now < s.end_dt)]
        group = self._group("now", "Now")
        group.set_header_suffix(self.replan_btn)
        if any(_takes_time(s) for s in current):
            self._fill(group, current, now, current=True)
        else:
            following = [s for s in spans if thirty_of(s.start_dt) > block and s.status == SpanStatus.PLANNED
                         and _takes_time(s)]
            until = f" until {short_clock(min(self._when(s) for s in following))}" if following else ""
            group.add(Adw.ActionRow(title=f"Free{until}"))
            if current:  # someone else's event, or an invite, happening now: shown under "Free", dimmed
                self._fill(group, current, now)
        upcoming = [s for s in spans if thirty_of(s.start_dt) > block and s.status == SpanStatus.PLANNED]
        if upcoming:
            self._fill(self._group("next", "Next"), upcoming, now, ahead=True)
        self._fill_pencilled(None)
        self._fill_routines(plan, now)
        self._fill_done(spans)

    def _fill_pencilled(self, day: Optional[str]) -> None:
        """On the day's list with no time yet (pencilled in, O3): shown as such, to be given a time or not. Read
        through ``read_tool``, which only reads: ``run_tool`` refreshes this page, so it would loop."""
        if self.read_tool is None:
            return
        try:
            out = self.read_tool("pencilled_in", {"day": day} if day else {}) or ""
        except Exception:
            return
        items = [line[2:] for line in out.splitlines() if line.startswith("- ")]
        if not items:
            return
        group = self._group("pencilled", "On the list, no time yet", "Pencilled in: give one a time, or leave it.")
        for title in items:
            group.add(Adw.ActionRow(title=title))

    def _fill(self, group: Adw.PreferencesGroup, spans: List[EventSpan], now: Optional[datetime],
              current: bool = False, behind: bool = False, ahead: bool = False) -> None:
        """A card per block, its time once in a gutter on the left (the user's pick, 10-31: "Next" read as one
        monolith of separate items); items you can mark (now, behind, or next: done early) get ✓ and ✗."""
        markable = current or behind or ahead
        entries = []
        for s in sorted(spans, key=lambda s: (self._when(s), s.start_dt)):
            row = Adw.ActionRow(title=s.title)
            if s.tentative:
                row.set_subtitle("invite, not answered yet")
            elif s.shown_only:
                row.set_subtitle("not yours (shown only)")
                row.add_css_class("dim-label")
            if markable and _markable(s):
                # done now: it finished at the click; done early (ahead): Thirties picks the times, on blocks (soak 3);
                # a calendar event happened at its calendar time
                row.add_suffix(self._outcome_button(s, "done", now if current and _flexible(s) else None))
                row.add_suffix(self._outcome_button(s, "skipped", None))  # now is a block: skip one of it too
            entries.append((self._when(s), row))
        self._add_blocks(group, entries)
        if not spans:
            group.add(Adw.ActionRow(title="Nothing"))

    def _add_blocks(self, group: Adw.PreferencesGroup, entries: List[tuple]) -> None:
        """(time, row) pairs, in order, as one card per time with the time in the gutter."""
        blocks: List[tuple] = []
        for when, row in entries:
            if blocks and blocks[-1][0] == when:
                blocks[-1][1].append(row)
            else:
                blocks.append((when, [row]))
        for when, rows in blocks:
            box = Gtk.Box(spacing=10, margin_bottom=6)
            box.add_css_class("agenda-block")
            label = Gtk.Label(label=short_clock(when) if when else "", xalign=1, yalign=0, margin_top=15)
            label.set_size_request(GUTTER, -1)
            label.add_css_class("caption-heading")
            label.add_css_class("dim-label")
            box.append(label)
            card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
            card.add_css_class("boxed-list")
            for row in rows:
                card.append(row)
            box.append(card)
            box.when_text, box.rows = label.get_label(), rows  # read by the tests
            group.add(box)

    def _fill_done(self, spans: List[EventSpan]) -> None:
        done = [s for s in spans if s.status != SpanStatus.PLANNED and _markable(s)]
        if not done:
            return
        group = self._group("done", "Done today", "What actually happened, and when.")
        entries = []
        for s in sorted(done, key=lambda s: s.actual_start or s.start_dt):
            note = ""
            if s.status == SpanStatus.SKIPPED:
                note = "skipped"
            elif s.status == SpanStatus.UNKNOWN:  # gone moot with nothing said: not known to have happened
                note = "not reported"
            elif s.status == SpanStatus.PARTIAL:
                note = "partly"
            elif self._ongoing(s):  # a session: the thing itself carries on (B2)
                note = "worked on"
            row = Adw.ActionRow(title=s.title, subtitle=note)
            row.add_css_class("dim-label")
            entries.append((thirty_of(s.actual_start or s.start_dt), row))
        self._add_blocks(group, entries)

    def _when(self, span: EventSpan) -> datetime:
        """Where an item shows: a fixed thing at its own time, everything else at its block's start."""
        try:
            fixed = bool(self.fixed(span))
        except Exception:
            fixed = span.source == "calendar"
        return span.start_dt if fixed else thirty_of(span.start_dt)

    def _ongoing(self, span: EventSpan) -> bool:
        try:
            return bool(self.ongoing(span))
        except Exception:
            return False

    # --- routines: skip like a subscription (0.2 item 8) ---

    def _fill_routines(self, plan: DayPlan, now: Optional[datetime] = None) -> None:
        """The day's routines, each with a Skip menu: skip today (back tomorrow), skip this time (a full cycle
        later), or skip until a day picked on a calendar. A past day has nothing left to skip."""
        if self.routines is None or self.run_tool is None:
            return
        if now is None and plan.day_end.timestamp() <= datetime.now(plan.day_end.tzinfo).timestamp():
            return
        try:
            routines = self.routines(plan)
        except Exception:  # the routine lookup never breaks the day page
            return
        if not routines:
            return
        group = self._group("routines", "Routines", "Skipping one isn't putting it off.")
        for r in routines:
            row = Adw.ActionRow(title=r["title"], subtitle=r.get("state", ""))
            menu = self._skip_menu(plan, r)
            row.add_suffix(menu)
            self.skip_menus[r["title"]] = menu
            group.add(row)

    def _skip_menu(self, plan: DayPlan, routine: Dict[str, Any]) -> Gtk.MenuButton:
        """A "Skip" menu button whose popover holds the three choices, each with the day it brings the routine
        back, and a calendar for "until"."""
        title, day = routine["title"], plan.target_date
        button = Gtk.MenuButton(label="Skip", valign=Gtk.Align.CENTER, tooltip_text=f"Skip {title}")
        button.add_css_class("flat")
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        popover.set_child(box)
        button.set_popover(popover)
        choices: Dict[str, Gtk.Button] = {}
        picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        calendar = Gtk.Calendar()
        confirm = Gtk.Button(label="Skip until this day")
        confirm.add_css_class("suggested-action")
        picker.append(calendar)
        picker.append(confirm)
        for choice in routine["choices"]:
            how = choice["how"]
            if how == "until":
                text, back = "Skip until a day…", "Pick the day it comes back"
            else:
                text = "Skip today" if how == "today" else "Skip this time"
                back = "Back " + _day_text(choice["back"], day) if choice.get("back") else "Next only when asked"
            label = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            label.append(Gtk.Label(label=text, xalign=0))
            sub = Gtk.Label(label=back, xalign=0)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            label.append(sub)
            b = Gtk.Button(child=label, tooltip_text=f"{text.rstrip('…')}: {back}")
            b.add_css_class("flat")
            choices[how] = b
            box.append(b)
        box.append(picker)

        def skip(how: str, until: Optional[date] = None) -> None:
            popover.popdown()
            args: Dict[str, Any] = {"routine": title, "how": how}
            if until is not None:
                args["until"] = until.isoformat()
            # no day: the tools work on the day this page shows (the window's manager is that day's)
            result = self.run_tool("skip_routine", args) if self.run_tool else ""
            if result and not result.startswith(NOT_DONE) and self.on_notice:  # a refusal is shown by the window
                self.on_notice(result)

        def show_picker(_b) -> None:
            back = day + timedelta(days=2)
            calendar.set_date(GLib.DateTime.new_local(back.year, back.month, back.day, 0, 0, 0))
            picker.set_visible(True)

        def picked(_b) -> None:
            d = calendar.get_date()
            until = date(d.get_year(), d.get_month(), d.get_day_of_month())
            if until <= day:
                if self.on_notice:
                    self.on_notice("Pick a day after this one.")
                return
            skip("until", until)

        for how, b in choices.items():
            b.connect("clicked", show_picker if how == "until" else (lambda _b, how=how: skip(how)))
        confirm.connect("clicked", picked)
        button.choices, button.calendar, button.confirm = choices, calendar, confirm
        return button

    def _outcome_button(self, span: EventSpan, status: str, finished_now: Optional[datetime]) -> Gtk.Button:
        done = status == "done"
        button = Gtk.Button(icon_name="object-select-symbolic" if done else "window-close-symbolic",
                            valign=Gtk.Align.CENTER, tooltip_text=f"Mark {span.title} {'done' if done else 'skipped'}")
        button.add_css_class("flat")
        button.add_css_class("circular")
        def clicked(_b):
            args = {"event": span.id, "status": status}
            if finished_now is not None:  # it's happening now: it finished at the click
                from thirties_core.astronomy import get_current_time
                args["finished"] = format_clock(get_current_time(span.start_dt.tzinfo))
            if self.run_tool:
                self.run_tool("log_outcome", args)
        button.connect("clicked", clicked)
        return button

    def _on_replan(self, _btn) -> None:
        if self.run_tool is None:
            return
        result = self.run_tool("redraw", {})
        if self.on_replan:
            self.on_replan(result)
