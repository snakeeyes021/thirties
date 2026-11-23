"""The day at a glance: what's not marked yet, now and next, and what got done.

The plan is a sketch; this page shows where the day stands. What's past with
nothing said comes first (not assumed done; being behind is common), with buttons
to say what happened; now and next can be marked too (things get done early).
What's done shows when it was done (the block it ended in, or the time the user
gave), greyed (a session on something ongoing reads "worked on"; what went moot
unreported reads "not reported"). Replan
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

# If there's time (if_time.py)
IF_TIME_TITLE = "If there's time"
IF_TIME_DONE = "Done"
IF_TIME_SCHEDULE = "Schedule it"
IF_TIME_NOT_TODAY = "Not today"
IF_TIME_TOMORROW = "Tomorrow"
IF_TIME_WEEK = "Later this week"
IF_TIME_DROP = "Drop"
# Pencilled in on a later day's list (day_actions.py)
PENCIL_NOT_THAT_DAY = "Not that day"
PENCIL_DAY_AFTER = "The day after"
# The days ahead, pencilled in (week_draft.py): a draft, not the plan
DRAFT_TITLE = "Pencilled in"
DRAFT_TEXT = "A draft for this day. It firms up that morning, and takes no time until then."
# Not yours today (not_yours.py)
NOT_YOURS_TITLE = "Not yours today"
NOT_YOURS_HIDE = "Hide for today"
NOT_YOURS_NOT_MINE = "Not mine, every time"
NOT_YOURS_MINE = "Mine too, every time"  # the user's too: on their plan, the whole series (others_events.py)
NOT_YOURS_MENU = "Whose is this?"
NOT_YOURS_SWITCH = "Show other people's events"
NOT_YOURS_SHOW = "Show"


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


BEHIND_TITLE = "Not marked yet"
BEHIND_TEXT = "Past their time with nothing said: not assumed done."

GUTTER = 60  # px: the block times' column, to the left of the cards


def short_clock(dt: datetime) -> str:
    """'11 AM', '5:15 PM': the minutes only when there are some (10-31: the times were too busy)."""
    return format_clock(dt).replace(":00 ", " ")


def _block_end(span: EventSpan) -> datetime:
    return thirty_ceil(span.end_dt)


# What each untimed list offers: (Done, Schedule it, the "not now" menu's label and its choices)
IF_TIME_CHOICES = (IF_TIME_NOT_TODAY, (("tomorrow", IF_TIME_TOMORROW), ("week", IF_TIME_WEEK), ("drop", IF_TIME_DROP)))
PENCIL_CHOICES = (PENCIL_NOT_THAT_DAY, (("next_day", PENCIL_DAY_AFTER), ("drop", IF_TIME_DROP)))


def untimed_actions(title: str, choices: tuple, act: Callable[[str, str], None]) -> tuple:
    """An untimed item's actions, the same on the Agenda and the Timeline: Done (a tick), Schedule it, and a
    menu to put it off. Returns (done, schedule, later, the menu's box); each calls ``act(title, action)``."""
    later_label, later_choices = choices
    done = Gtk.Button(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER, tooltip_text=IF_TIME_DONE)
    done.add_css_class("flat")
    done.set_cursor_from_name("pointer")
    done.update_property([Gtk.AccessibleProperty.LABEL], [f"{IF_TIME_DONE}: {title}"])
    done.connect("clicked", lambda _b: act(title, "done"))
    schedule = Gtk.Button(label=IF_TIME_SCHEDULE, valign=Gtk.Align.CENTER)
    schedule.add_css_class("flat")
    schedule.connect("clicked", lambda _b: act(title, "schedule"))
    later = Gtk.MenuButton(label=later_label, valign=Gtk.Align.CENTER)
    later.add_css_class("flat")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                  margin_start=6, margin_end=6)
    pop = Gtk.Popover(child=box)
    for action, label in later_choices:
        b = Gtk.Button(label=label)
        b.add_css_class("flat")
        b.connect("clicked", lambda _b, a=action: (pop.popdown(), act(title, a)))
        box.append(b)
    later.set_popover(pop)
    return done, schedule, later, box


def _draft_span(d: Dict[str, Any], start: datetime) -> EventSpan:
    """A drafted item as a span, only so it's titled like every other row (short title, never a raw id)."""
    return EventSpan(f"draft:{d.get('task_id') or d.get('title')}", str(d.get("title") or ""), start,
                     start + timedelta(minutes=int(d.get("minutes") or 30)), SpanCategory.TASK,
                     task_id=d.get("task_id") or None)


def _children(widget: Gtk.Widget) -> List[Gtk.Widget]:
    """Every widget under one, in order."""
    out, child = [], widget.get_first_child()
    while child is not None:
        out.append(child)
        out += _children(child)
        child = child.get_next_sibling()
    return out


class AgendaPage(Gtk.ScrolledWindow):

    def __init__(self, run_tool: Optional[Tool] = None, on_replan: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.run_tool, self.on_replan = run_tool, on_replan
        self._tz = None
        self._work_step = None
        self.work_btn = None
        self.on_notice: Optional[Callable[[str], None]] = None
        # plan -> [{"title", "state", "choices": [{"how", "label", "back"}]}] (RoutineTools.day_routines)
        self.routines: Optional[Callable[[DayPlan], List[Dict[str, Any]]]] = None
        self.ongoing: Callable[[EventSpan], bool] = lambda span: False
        # span -> bool: set by someone else's clock (shown at its own time; the rest by their block's start)
        self.fixed: Callable[[EventSpan], bool] = lambda span: span.source == "calendar" or span.is_firm
        self.skip_menus: Dict[str, Gtk.MenuButton] = {}  # routine title -> its Skip menu (tests reach it here)
        self.read_tool: Optional[Tool] = None  # a read-only tool, run without refreshing (set by the window)
        self.paused: Callable[[], str] = lambda: ""  # "Check-ins paused until 6:30 AM" while they are (the window)
        # the one switch for other people's events: read, and changed (the window saves it and redraws)
        self.show_theirs: Callable[[], bool] = lambda: True
        # after a work button's log_day_times: the window's hook (the rest of the day is replanned from it)
        self.on_work_times: Callable[[str], None] = lambda result: None
        # (title, action) -> what's said: an action on a later day's pencilled-in item (day_actions.pencil_act)
        self.pencil_action: Optional[Callable[[str, str], str]] = None
        # plan -> the day's draft [{"title", "start", "minutes"}] for a later day (week draft); [] otherwise
        self.drafts: Optional[Callable[[DayPlan], List[Dict[str, Any]]]] = None
        self.set_show_theirs: Callable[[bool], None] = lambda on: None
        # an item's short title and its description (11-06: no raw lines, markdown or ids on screen); the window
        # sets these to read the user's own notes and known ids too
        from thirties_core.titles import split_title
        self.title_for: Callable[[EventSpan], str] = lambda span: split_title(span.title)[0]
        self.describe: Callable[[EventSpan], str] = lambda span: split_title(span.title)[1]
        self.page = Adw.PreferencesPage()
        self.set_child(self.page)
        self.groups: dict[str, Adw.PreferencesGroup] = {}
        self.replan_btn = Gtk.Button(label="Replan", valign=Gtk.Align.CENTER,
                                     tooltip_text="Lay the rest of today out again from now")
        self.replan_btn.add_css_class("flat")
        self.replan_btn.connect("clicked", self._on_replan)

    # --- building ---

    def _on_resume(self, _button) -> None:
        out = self.run_tool("unmute_checkins", {}) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)

    def _group(self, key: str, title: str, description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(group)
        self.groups[key] = group
        return group

    def refresh(self, plan: DayPlan, now: Optional[datetime]) -> None:
        self._tz = plan.sunrise.tzinfo
        from thirties_core.day_actions import work_step
        self._work_step = work_step(plan, now) if self.run_tool is not None else None  # on Work's own row
        self.work_btn = None
        for group in self.groups.values():
            if group is self.groups.get("now"):
                group.set_header_suffix(None)
            self.page.remove(group)
        self.groups = {}
        self.skip_menus = {}
        self.not_yours_rows = {}
        from thirties_core.calendar_engine import unread_text
        unread = unread_text(plan.calendar_unread)
        if unread:  # first: an empty-looking day may not be free
            row = Adw.ActionRow(title=unread, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            self._group("calendar_unread", "").add(row)
        paused = self.paused() if now is not None else ""
        if paused:  # quiet, with the way back
            row = Adw.ActionRow(title=paused, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="notifications-disabled-symbolic"))
            self.resume_btn = Gtk.Button(label="Resume", valign=Gtk.Align.CENTER)
            self.resume_btn.add_css_class("flat")
            self.resume_btn.connect("clicked", self._on_resume)
            row.add_suffix(self.resume_btn)
            self._group("paused", "").add(row)
        theirs = [s for s in plan.spans_in_view() if s.shown_only]
        spans = [s for s in plan.spans_in_view() if not s.shown_only]  # someone else's: their own section, last
        # gone by with nothing said (marked "not reported"): not done, so never under Done; still to sort out. What's
        # not marked yet is the one rule the chat's list uses too (not_marked.py)
        from thirties_core.not_marked import unmarked_spans
        unreported = [s for s in unmarked_spans(spans, None) if _markable(s)]
        if now is None:  # another day: the plan as it stands, and its record if it's past
            self._fill(self._group("plan", "The plan"), [s for s in spans if s.status == SpanStatus.PLANNED], now)
            self._fill_drafts(plan)
            self._fill_pencilled(plan.target_date.isoformat())
            self._fill_routines(plan)
            if unreported:  # still to sort out, there too
                self._fill(self._group("behind", BEHIND_TITLE, BEHIND_TEXT), unreported, now, behind=True)
            self._fill_done(spans)
            self._fill_not_yours(theirs, today=False)
            return
        # In blocks, as the cards say (10-31: one 11:00 block was split across Now and Next, and a 9:00 item was
        # "past" at 9:06): an item is now through its whole last thirty, and past only once that's over.
        block = thirty_of(now)
        # a calendar event too: past its time with nothing said, it waits here rather than vanish from the day
        behind = [s for s in unmarked_spans(spans, block) if _markable(s)]
        if behind:  # first: being behind is common, and these are what to sort out
            self._fill(self._group("behind", BEHIND_TITLE, BEHIND_TEXT), behind, now, behind=True)
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
            # in the time column: when the free stretch began, so the row lines up with the rest (11-05)
            began = max((_block_end(s) for s in spans if _takes_time(s) and s.status != SpanStatus.SKIPPED
                         and _block_end(s) <= block), default=None)
            if began is None:
                began = next((s.end_dt for s in spans if s.category == SpanCategory.SLEEP and s.end_dt <= now),
                             plan.day_start)
            self._add_blocks(group, [(began, Adw.ActionRow(title=f"Free{until}"))])
            if current:  # someone else's event, or an invite, happening now: shown under "Free", dimmed
                self._fill(group, current, now)
        upcoming = [s for s in spans if thirty_of(s.start_dt) > block and s.status == SpanStatus.PLANNED]
        listed = {s.id for s in (current if any(_takes_time(x) for x in current) else []) + upcoming}
        self._fill_today_rows(now, listed)  # work started or finished: a tap
        if upcoming:
            self._fill(self._group("next", "Next"), upcoming, now, ahead=True)
        self._fill_if_time()  # after the day's timed items
        # no "on the list, no time yet" section on Today any more (11-06): today's items are placed on the
        # schedule, and what's left from before is in the Before today tab
        self._fill_routines(plan, now)
        self._fill_done(spans)
        self._fill_not_yours(theirs, today=True)

    def _work_button(self, row: Adw.ActionRow) -> None:
        """Work's row gets its button: Started work, then Finished work (the time of the tap, log_day_times)."""
        from thirties_core.day_actions import WORK_LABELS, work_line
        kind, work = self._work_step
        row.set_subtitle(work_line(work))
        button = Gtk.Button(label=WORK_LABELS[kind], valign=Gtk.Align.CENTER, tooltip_text=f"{WORK_LABELS[kind]} now")
        button.add_css_class("flat")
        button.connect("clicked", lambda _b, k=kind: self._work(k))
        row.add_suffix(button)
        self.work_btn = button

    def _fill_today_rows(self, now: datetime, listed: set) -> None:
        """Today only: Work's button on a row of its own when Work isn't listed under Now or Next (it's on Work's
        own row there). No time in the gutter: it's about now."""
        rows = []
        if self._work_step is not None and self._work_step[1].id not in listed:
            work = self._work_step[1]
            row = Adw.ActionRow(title=GLib.markup_escape_text(self.title_for(work)))
            self._work_button(row)
            rows.append((None, row))
        if rows:
            self._add_blocks(self._group("today_rows", ""), rows)

    def _work(self, kind: str) -> None:
        from thirties_core.astronomy import get_current_time
        from thirties_core.day_actions import work_call
        if self.run_tool is None:
            return
        now = get_current_time(self._tz)
        result = self.run_tool(*work_call(kind, now)) or ""
        if not result.startswith(NOT_DONE):
            self.on_work_times(result)

    def _fill_not_yours(self, theirs: List[EventSpan], today: bool) -> None:
        """Someone else's events (not_yours.py), always last: each can be hidden for today, or marked not mine for
        every time it comes up. Never the actions of the user's own items."""
        if today and self.read_tool is not None:  # minus those hidden for today
            try:
                ids = {row[2:] for row in (self.read_tool("not_yours_today", {}) or "").splitlines()
                       if row.startswith("- ")}
                theirs = [s for s in theirs if s.id in ids]
            except Exception:
                pass
        if not theirs:
            return
        group = self._group("not_yours", NOT_YOURS_TITLE)
        try:
            shown = bool(self.show_theirs())
        except Exception:
            shown = True
        switch = Gtk.Switch(active=shown, valign=Gtk.Align.CENTER, tooltip_text=NOT_YOURS_SWITCH)
        switch.update_property([Gtk.AccessibleProperty.LABEL], [NOT_YOURS_SWITCH])
        switch.connect("notify::active", lambda sw, _p: self._show_theirs(sw.get_active()))
        group.set_header_suffix(switch)
        self.not_yours_switch = switch
        if not shown:  # one quiet line, and the way back
            count = len(theirs)
            row = Adw.ActionRow(title=f"{count} hidden" if count != 1 else "1 hidden")
            row.add_css_class("dim-label")
            show = Gtk.Button(label=NOT_YOURS_SHOW, valign=Gtk.Align.CENTER)
            show.add_css_class("flat")
            show.connect("clicked", lambda _b: self._show_theirs(True))
            row.add_suffix(show)
            group.add(row)
            self.not_yours_hidden_row = row
            return
        entries = []
        for s in sorted(theirs, key=lambda s: s.start_dt):
            # the Agenda's row pattern: the time in the left column; whose and until when, quietly, under the title;
            # the three choices in one menu, so the title keeps its room at phone width
            row = Adw.ActionRow(title=GLib.markup_escape_text(self.title_for(s)),
                                subtitle=GLib.markup_escape_text(", ".join(
                                    x for x in (s.whose, f"until {short_clock(s.end_dt)}") if x)))
            row.add_css_class("dim-label")
            menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text=NOT_YOURS_MENU)
            menu.add_css_class("flat")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            pop = Gtk.Popover(child=box)
            choices = ([(NOT_YOURS_HIDE, "hide_for_today", {"event": s.id})] if today else []) + [
                (NOT_YOURS_NOT_MINE, "resolve_calendar_event", {"event": s.id, "attend": False, "series": True}),
                (NOT_YOURS_MINE, "resolve_calendar_event", {"event": s.id, "attend": True, "series": True})]
            buttons = []
            for label, tool, args in choices:
                b = Gtk.Button(label=label)
                b.add_css_class("flat")
                b.get_child().set_xalign(0)
                b.connect("clicked", lambda _b, t=tool, a=args, p=pop: (p.popdown(), self._not_yours(t, a)))
                box.append(b)
                buttons.append(b)
            menu.set_popover(pop)
            row.add_suffix(menu)
            entries.append((s.start_dt, row))
            self.not_yours_rows[s.id] = buttons
        self._add_blocks(group, entries)

    def _show_theirs(self, on: bool) -> None:
        try:
            if bool(self.show_theirs()) == on:
                return
        except Exception:
            pass
        GLib.idle_add(lambda: self.set_show_theirs(on) and False)  # it redraws this page: not inside the handler

    def _not_yours(self, tool: str, args: Dict[str, Any]) -> None:
        if self.run_tool is not None:
            self.run_tool(tool, args)

    def _fill_if_time(self) -> None:
        """Today's things with no time, because the user asked or the day was full (if_time.py): each can be done,
        scheduled in the next gap, or put off (tomorrow, later this week, or dropped)."""
        if self.read_tool is None:
            return
        try:
            out = self.read_tool("if_there_is_time_items", {}) or ""
        except Exception:
            return
        items = [line[2:] for line in out.splitlines() if line.startswith("- ")]
        if not items:
            return
        group = self._group("if_time", IF_TIME_TITLE)
        self.if_time_rows = {}
        for title in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(title))
            done, schedule, later, box = untimed_actions(title, IF_TIME_CHOICES, self._if_time)
            for w in (done, schedule, later):
                row.add_suffix(w)
            group.add(row)
            self.if_time_rows[title] = (done, schedule, later, box)

    def _if_time(self, title: str, action: str) -> None:
        if self.run_tool is not None:
            out = self.run_tool("if_there_is_time", {"item": title, "action": action})
            if out and self.on_notice is not None and action == "schedule":
                self.on_notice(out)

    def _fill_drafts(self, plan: DayPlan) -> None:
        """A later day's draft, pencilled in: light and slanted, at its drafted times, with nothing to tick. It's
        not the plan: it firms up that morning."""
        if self.drafts is None:
            return
        try:
            drafts = self.drafts(plan) or []
        except Exception:
            return
        entries = []
        from thirties_core.clock import format_duration
        for d in drafts:
            try:
                start = datetime.fromisoformat(str(d["start"]))
            except (KeyError, TypeError, ValueError):
                continue
            row = Adw.ActionRow(title=f"<i>{GLib.markup_escape_text(self.title_for(_draft_span(d, start)))}</i>",
                                subtitle=format_duration(int(d.get("minutes") or 30)))
            row.add_css_class("pencilled")
            entries.append((start, row))
        if entries:
            group = self._group("drafts", DRAFT_TITLE, DRAFT_TEXT)
            self._add_blocks(group, entries)
            for box in _children(group):
                if hasattr(box, "when_text"):
                    box.add_css_class("pencilled")

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
        self.pencil_rows = {}
        for title in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(title))
            if self.pencil_action is not None:
                buttons = untimed_actions(title, PENCIL_CHOICES, self._pencil)
                for w in buttons[:3]:  # Done, Schedule it, Not that day (the last is that menu's own box)
                    row.add_suffix(w)
                self.pencil_rows[title] = buttons
            group.add(row)

    def _pencil(self, title: str, action: str) -> None:
        if self.pencil_action is None:
            return
        out = self.pencil_action(title, action)
        if out and self.on_notice is not None:
            self.on_notice(out)

    def _fill(self, group: Adw.PreferencesGroup, spans: List[EventSpan], now: Optional[datetime],
              current: bool = False, behind: bool = False, ahead: bool = False) -> None:
        """A card per block, its time once in a gutter on the left (the user's pick, 10-31: the Next section
        read as one block, not separate items); items you can mark (now, behind, or next: done early) get ✓ and ✗."""
        markable = current or behind or ahead
        entries = []
        for s in sorted(spans, key=lambda s: (self._when(s), s.start_dt)):
            row = self._item_row(s)
            from thirties_core.others_events import marker  # whose calendar it's on (others_events.py)
            if s.tentative:
                row.set_subtitle("invite, not answered yet" + (f", on {s.whose}" if s.whose else ""))
            elif s.shown_only:
                row.set_subtitle(marker(s) or "not yours (shown only)")
                row.add_css_class("dim-label")
            elif s.shared and s.whose:
                row.set_subtitle(marker(s))
            if (current or ahead) and self._work_step is not None and s.id == self._work_step[1].id \
                    and self.work_btn is None:
                self._work_button(row)
            if markable and _markable(s):
                # done now: it finished at the click; done early (ahead): Thirties picks the times, on blocks (soak 3);
                # a calendar event happened at its calendar time
                row.add_suffix(self._outcome_button(s, "done", now if current and _flexible(s) else None))
                row.add_suffix(self._outcome_button(s, "skipped", None))  # now is a block: skip one of it too
            entries.append((self._when(s), row))
        self._add_blocks(group, entries)
        if not spans:
            group.add(Adw.ActionRow(title="Nothing"))

    def _item_row(self, s: EventSpan):
        """An item's row: its short title; with a long description, a row that expands to show it (11-06)."""
        title = GLib.markup_escape_text(self.title_for(s))
        description = self.describe(s)
        if not description:
            return Adw.ActionRow(title=title)
        row = Adw.ExpanderRow(title=title)
        detail = Adw.ActionRow(title=GLib.markup_escape_text(description))
        detail.add_css_class("dim-label")
        try:
            detail.set_title_lines(0)  # wraps: the whole description
        except AttributeError:
            pass
        row.add_row(detail)
        return row

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
        done = [s for s in spans if s.status not in (SpanStatus.PLANNED, SpanStatus.UNKNOWN) and _markable(s)]
        if not done:
            return
        group = self._group("done", "Done today", "What actually happened, and when.")
        entries = []
        from thirties_core.shown_times import done_when  # when it was done, by its block unless the user said
        for s in sorted(done, key=done_when):
            note = ""
            if s.status == SpanStatus.SKIPPED:
                note = "skipped"
            elif s.status == SpanStatus.PARTIAL:
                note = "partly"
            elif self._ongoing(s):  # a session: the thing itself carries on (B2)
                note = "worked on"
            row = self._item_row(s)
            row.set_subtitle(note)
            row.add_css_class("dim-label")
            entries.append((done_when(s), row))
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
        entries = []
        for r in routines:
            row = Adw.ActionRow(title=r["title"], subtitle=r.get("state", ""))
            menu = self._skip_menu(plan, r)
            row.add_suffix(menu)
            self.skip_menus[r["title"]] = menu
            entries.append((r.get("at"), row))  # its time in the time column, like every other row (11-05)
        self._add_blocks(group, entries)

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
