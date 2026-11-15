"""The Agenda's "Before today" tab (11-06, the user's design): what's left from earlier days, and what Thirties did
with it. It acts on the same records as the chat (thirties_core/before_today.py) through one tool,
leftover_action: Keep rows can be accepted, marked done or dropped; Drop rows accepted, kept or marked done; the
Archive (drops the app applied that were never confirmed, by day) can be undropped. Nothing is ever deleted.

Rows look like the Today tab's: a card per row, with its time (if it's on today's schedule) or its horizon in the
column on the left.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from gi.repository import Adw, GLib, Gtk

from thirties_core.titles import title_of
from thirties.views.agenda import GUTTER, short_clock

# copy-madlibs T3-027 to T3-038 (button labels, section titles and hints, empty state)
KEEP_TITLE, KEEP_HINT = "Keep", "Waiting for a day they fit; today's are on the schedule."
DROP_TITLE, DROP_HINT = "Drop", "Proposed. Applied by itself after today unless you say otherwise."
RESTORE_TITLE = "Taken off your lists earlier"
RESTORE_HINT = "Put any of these back on their lists?"
RESTORE_PUT_BACK, RESTORE_LEAVE, RESTORE_LEAVE_ALL = "Put back", "Leave off", "Leave all off"
ARCHIVE_TITLE, ARCHIVE_HINT = "Archive", "Let go. Nothing is deleted: undrop brings one back."
EMPTY = "Nothing left from before."
ASK_TITLE = "Still want these?"
ASK_HINT = "Missed several days running, so Thirties stopped offering them. Asked once."
ASK_LABELS = {"keep": "Keep for this week", "park": "Park for later", "drop": "Drop"}
LABELS = {"accept": "Accept", "done": "Done", "drop": "Drop", "keep": "Keep", "undrop": "Undrop"}


class BeforeTodayPage(Gtk.ScrolledWindow):

    def __init__(self) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.rows_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.run_tool: Optional[Callable[[str, Dict[str, Any]], str]] = None
        self.on_notice: Optional[Callable[[str], None]] = None
        self.page = Adw.PreferencesPage()
        self.set_child(self.page)
        self.groups: Dict[str, Adw.PreferencesGroup] = {}
        self.buttons: Dict[tuple, Gtk.Button] = {}  # (title, action) -> its button (tests reach it here)

    def _group(self, key: str, title: str, description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(group)
        self.groups[key] = group
        return group

    def refresh(self) -> None:
        for group in self.groups.values():
            self.page.remove(group)
        self.groups, self.buttons = {}, {}
        try:
            data = self.rows_provider() if self.rows_provider else {}
        except Exception:  # never in the way of the rest of the window
            data = {}
        keep, drop, archive = data.get("keep") or [], data.get("drop") or [], data.get("archive") or []
        restore = data.get("restore") or []
        ask = data.get("ask") or []
        if ask:  # the one question about what's been missed several days running (backoff.py)
            group = self._group("ask", ASK_TITLE, ASK_HINT)
            for r in ask:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(r["title"])),
                                    subtitle=GLib.markup_escape_text(r.get("why") or ""))
                for choice in ("keep", "park", "drop"):
                    b = Gtk.Button(label=ASK_LABELS[choice], valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, t=r["title"], c=choice: self._answer(t, c))
                    self.buttons[("still", r["title"], choice)] = b
                    row.add_suffix(b)
                group.add(row)
        if restore:  # shown once: items taken off their lists earlier, each can go back
            group = self._group("restore", RESTORE_TITLE, RESTORE_HINT)
            for title in restore:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(title)))
                for put_back, label in ((True, RESTORE_PUT_BACK), (False, RESTORE_LEAVE)):
                    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, t=title, p=put_back: self._restore(p, t))
                    self.buttons[("restore", title, "put_back" if put_back else "leave")] = b
                    row.add_suffix(b)
                group.add(row)
            leave_all = Gtk.Button(label=RESTORE_LEAVE_ALL, halign=Gtk.Align.END, margin_top=6)
            leave_all.connect("clicked", lambda _b: self._restore(False))
            self.buttons[("restore", "leave_all")] = leave_all
            group.add(leave_all)
        if not keep and not drop and not archive and not restore and not ask:
            self._group("empty", "").add(Adw.ActionRow(title=EMPTY))
            return
        if keep:
            group = self._group("keep", KEEP_TITLE, KEEP_HINT)
            self._add_rows(group, [(self._when(r), self._row(r, ("accept", "done", "drop"))) for r in keep])
        if drop:
            group = self._group("drop", DROP_TITLE, DROP_HINT)
            # where it would go if kept, in the left column like the Keep rows (11-06): Keep does exactly that
            self._add_rows(group, [(self._when(r), self._row(r, ("accept", "keep", "done"))) for r in drop])
        if archive:
            group = self._group("archive", ARCHIVE_TITLE, ARCHIVE_HINT)
            for day, rows in archive:
                expander = Adw.ExpanderRow(title=_day_text(day), subtitle=str(len(rows)))
                for r in rows:
                    expander.add_row(self._row(r, ("undrop",)))
                group.add(expander)

    @staticmethod
    def _when(r: Dict[str, Any]) -> str:
        at = r.get("at")
        return short_clock(at) if isinstance(at, datetime) else (r.get("when") or "")

    def _row(self, r: Dict[str, Any], actions: tuple) -> Adw.ActionRow:
        from gi.repository import GLib
        from thirties_core.titles import title_of
        row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(r["title"])),  # short title; actions use the item
                            subtitle=GLib.markup_escape_text(r.get("why") or ""))
        for action in actions:
            b = Gtk.Button(label=LABELS[action], valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, t=r["title"], a=action: self._act(t, a))
            self.buttons[(r["title"], action)] = b
            row.add_suffix(b)
        return row

    def _restore(self, put_back: bool, item: str = "") -> None:
        args = {"put_back": put_back, **({"item": item} if item else {})}
        out = self.run_tool("restore_old_drops", args) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    def _answer(self, title: str, choice: str) -> None:
        out = self.run_tool("still_want", {"item": title, "choice": choice}) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    def _act(self, title: str, action: str) -> None:
        out = self.run_tool("leftover_action", {"item": title, "action": action}) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    @staticmethod
    def _add_rows(group: Adw.PreferencesGroup, entries: List[tuple]) -> None:
        """(left-column text, row) pairs as the Today tab lays them out: one card per row, the text on the left."""
        for when, row in entries:
            box = Gtk.Box(spacing=10, margin_bottom=6)
            box.add_css_class("agenda-block")
            label = Gtk.Label(label=when, xalign=1, yalign=0, margin_top=15, wrap=True, justify=Gtk.Justification.RIGHT)
            label.set_size_request(GUTTER, -1)
            label.set_max_width_chars(9)
            label.add_css_class("caption-heading")
            label.add_css_class("dim-label")
            box.append(label)
            card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
            card.add_css_class("boxed-list")
            card.append(row)
            box.append(card)
            box.when_text, box.rows = when, [row]  # read by the tests
            group.add(box)


def _day_text(day: str) -> str:
    try:
        return date.fromisoformat(day).strftime("%a %b %-d")
    except ValueError:
        return day
