"""The day at a glance: now and next, what isn't reported yet, and what got done.

The plan is a sketch; this page shows where the day stands. What's done shows
at the times it actually happened, greyed; what's past with nothing reported is
listed on its own (not assumed done), with buttons to say what happened. Replan
lays the rest of today out again (Python's redraw), with Undo.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, List, Optional

from gi.repository import Adw, Gtk

from thirties_core.clock import format_clock, thirty_of
from thirties_core.models import DayPlan, EventSpan, SpanCategory, SpanStatus

Tool = Callable[[str, dict], str]  # run a schedule tool by name; returns its result


def _flexible(span: EventSpan) -> bool:
    return span.category in (SpanCategory.ACTIVITY, SpanCategory.TASK) and not span.tentative


def _rows_by_thirty(spans: List[EventSpan]) -> List[tuple]:
    """(start of thirty, [spans]) with short items sharing a thirty on one row, in order."""
    out: List[tuple] = []
    for s in sorted(spans, key=lambda s: s.start_dt):
        block = thirty_of(s.start_dt)
        short = s.duration_minutes < 30 and not s.is_envelope
        if short and out and out[-1][0] == block and all(x.duration_minutes < 30 for x in out[-1][1]):
            out[-1][1].append(s)
        else:
            out.append((block, [s]))
    return out


class AgendaPage(Gtk.ScrolledWindow):

    def __init__(self, run_tool: Optional[Tool] = None, on_replan: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.run_tool, self.on_replan = run_tool, on_replan
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
        spans = plan.spans_in_view()
        if now is None:  # another day: the plan as it stands, and its record if it's past
            self._fill(self._group("plan", "The plan"), [s for s in spans if s.status == SpanStatus.PLANNED], now)
            self._fill_done(spans)
            return
        current = [s for s in spans if s.start_dt <= now < s.end_dt and s.status == SpanStatus.PLANNED]
        group = self._group("now", "Now")
        group.set_header_suffix(self.replan_btn)
        if current:
            self._fill(group, current, now, current=True)
        else:
            following = [s for s in spans if s.start_dt > now and s.status == SpanStatus.PLANNED]
            until = f" until {format_clock(min(s.start_dt for s in following))}" if following else ""
            group.add(Adw.ActionRow(title=f"Free{until}"))
        upcoming = [s for s in spans if s.start_dt > now and s.status == SpanStatus.PLANNED]
        if upcoming:
            self._fill(self._group("next", "Next"), upcoming, now)
        behind = [s for s in spans if _flexible(s) and s.status == SpanStatus.PLANNED and s.end_dt <= now]
        if behind:
            self._fill(self._group("behind", "Not reported", "Past their time with nothing said: not assumed done."),
                       behind, now, behind=True)
        self._fill_done(spans)

    def _fill(self, group: Adw.PreferencesGroup, spans: List[EventSpan], now: Optional[datetime],
              current: bool = False, behind: bool = False) -> None:
        """Rows by thirty (short items sharing one); items you can mark get a row each."""
        rows = _rows_by_thirty(spans) if not (current or behind) else \
            [(thirty_of(s.start_dt), [s]) for s in sorted(spans, key=lambda s: s.start_dt)]
        for block, items in rows:
            title = ", then ".join(s.title for s in items)
            first = items[0]
            if first.is_envelope or first.duration_minutes >= 30:
                when = f"{format_clock(block)} – {format_clock(thirty_of(first.end_dt - timedelta(seconds=1)) + timedelta(minutes=30))}"
            else:
                when = format_clock(block)
            row = Adw.ActionRow(title=title, subtitle=("planned " + when) if behind else when)
            if first.tentative:
                row.set_subtitle(f"{when} · invite, not answered yet")
            if (current or behind) and _flexible(first):
                row.add_suffix(self._outcome_button(first, "done", now if current else None))
                if behind:
                    row.add_suffix(self._outcome_button(first, "skipped", None))
            group.add(row)
        if not spans:
            group.add(Adw.ActionRow(title="Nothing"))

    def _fill_done(self, spans: List[EventSpan]) -> None:
        done = [s for s in spans if s.status != SpanStatus.PLANNED and _flexible(s)]
        if not done:
            return
        group = self._group("done", "Done today", "What actually happened, and when.")
        for s in sorted(done, key=lambda s: s.actual_start or s.start_dt):
            if s.status == SpanStatus.SKIPPED:
                subtitle = "skipped"
            else:
                start, end = s.actual_start or s.start_dt, s.actual_end or s.end_dt
                subtitle = f"{format_clock(thirty_of(start))} – {format_clock(thirty_of(end - timedelta(seconds=1)) + timedelta(minutes=30))}"
                if s.status == SpanStatus.PARTIAL:
                    subtitle += " · partly"
            row = Adw.ActionRow(title=s.title, subtitle=subtitle)
            row.add_css_class("dim-label")
            group.add(row)

    def _outcome_button(self, span: EventSpan, status: str, finished_now: Optional[datetime]) -> Gtk.Button:
        done = status == "done"
        button = Gtk.Button(icon_name="object-select-symbolic" if done else "window-close-symbolic",
                            valign=Gtk.Align.CENTER, tooltip_text=f"Mark {span.title} {'done' if done else 'skipped'}")
        button.add_css_class("flat")
        button.add_css_class("circular")
        args = {"event": span.id, "status": status}
        if finished_now is not None:
            args["finished"] = format_clock(finished_now)
        button.connect("clicked", lambda _b: self.run_tool and self.run_tool("log_outcome", args))
        return button

    def _on_replan(self, _btn) -> None:
        if self.run_tool is None:
            return
        result = self.run_tool("redraw", {})
        if self.on_replan:
            self.on_replan(result)
