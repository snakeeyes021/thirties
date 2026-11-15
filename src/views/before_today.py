"""The Agenda's "Before today" tab (11-06, the user's design): what's left from earlier days, and what Thirties did
with it. It acts on the same records as the chat (thirties_core/before_today.py) through one tool,
leftover_action: Keep rows can be accepted, marked done or dropped; Drop rows accepted, kept or marked done; the
Archive (drops the app applied that were never confirmed, by day) can be undropped. Nothing is ever deleted.

Rows look like the Today tab's: a card per row, with its time (if it's on today's schedule) or its horizon in the
column on the left. On a wide page a row's buttons sit beside its title; on a narrow one (a phone) they go on a line
of their own under it, so the title always has the row's width to itself.
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
ASK_LABELS = {"keep": "Keep for this week", "park": "Park for later", "drop": "Drop", "break": "Break It Down"}
# put off several times (break_down.py): break it into smaller pieces, or keep it as it is
OFFER_TITLE = "Put off several times"
OFFER_HINT = "Smaller pieces can be easier to start. Asked once."
OFFER_LABELS = {"break": "Break It Down", "keep": "Keep as It Is"}
BROKEN_NOTE = "{title} stays off the plan until it has pieces. Send the message in the chat for a few first steps."
BREAK_ASK = "Help me break {title} into a few small first steps."  # written in the chat box, for the user to send
LABELS = {"accept": "Accept", "done": "Done", "drop": "Drop", "keep": "Keep", "undrop": "Undrop"}


class BeforeTodayPage(Gtk.ScrolledWindow):

    NARROW_BELOW = 560  # px of the page's width: under this, a row's buttons go under its title

    def __init__(self) -> None:
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.rows_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.run_tool: Optional[Callable[[str, Dict[str, Any]], str]] = None
        self.on_notice: Optional[Callable[[str], None]] = None
        # the put-off offers to break an item down: [{"task_id", "title", ...}] (break_down.open_offers); None where
        # breaking down isn't built in
        self.offers_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None
        # after "Break it down": the window asks the assistant for first steps (title -> None)
        self.on_break: Optional[Callable[[str], None]] = None
        self.page = Adw.PreferencesPage()
        self.set_child(self.page)
        self.groups: Dict[str, Adw.PreferencesGroup] = {}
        self.buttons: Dict[tuple, Gtk.Button] = {}  # (title, action) -> its button (tests reach it here)
        self.narrow = False  # laid out for a narrow page (set from the page's own width)
        self._relayout_id = 0

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)
        narrow = width < self.NARROW_BELOW
        if narrow != self.narrow:
            self.narrow = narrow
            if not self._relayout_id:  # never rebuilt during a layout: once it's done
                self._relayout_id = GLib.idle_add(self._relayout)

    def _relayout(self) -> bool:
        self._relayout_id = 0
        self.refresh()
        return False

    def _with_buttons(self, row: Adw.ActionRow, buttons: List[Gtk.Button]) -> Optional[Gtk.ListBoxRow]:
        """The buttons beside the row's title (wide), or on a line of their own under it (narrow): then that line is
        returned, to go right after the row."""
        if not self.narrow:
            for b in buttons:
                row.add_suffix(b)
            return None
        line = Adw.WrapBox(child_spacing=4, line_spacing=4, margin_start=6, margin_end=6, margin_top=2,
                           margin_bottom=4)
        for b in buttons:
            line.append(b)
        return Gtk.ListBoxRow(child=line, activatable=False, selectable=False)

    def _card(self, *rows: Optional[Gtk.Widget]) -> Gtk.ListBox:
        card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
        card.add_css_class("boxed-list")
        for r in rows:
            if r is not None:
                card.append(r)
        return card

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
        try:
            offers = (self.offers_provider() or []) if self.offers_provider else []
        except Exception:
            offers = []
        choices = ("keep", "park", "drop") + (("break",) if self.offers_provider is not None else ())
        if ask:  # the one question about what's been missed several days running (backoff.py)
            group = self._group("ask", ASK_TITLE, ASK_HINT)
            for r in ask:  # the title and why on top, the choices on a line of their own under them
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=10, margin_bottom=8,
                              margin_start=12, margin_end=12)
                title = Gtk.Label(label=title_of(r["title"]), xalign=0, wrap=True)
                box.append(title)
                if r.get("why"):
                    why = Gtk.Label(label=r["why"], xalign=0)
                    why.add_css_class("caption")
                    why.add_css_class("dim-label")
                    box.append(why)
                wrap = Adw.WrapBox(child_spacing=4, line_spacing=4, margin_top=4)
                for choice in choices:
                    b = Gtk.Button(label=ASK_LABELS[choice], valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, t=r["title"], c=choice: self._answer(t, c))
                    self.buttons[("still", r["title"], choice)] = b
                    wrap.append(b)
                box.append(wrap)
                card = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                card.add_css_class("boxed-list")
                card.append(Gtk.ListBoxRow(child=box, activatable=False))
                group.add(card)
        if offers:  # put off several times: break it down, or keep it as it is (each asked once)
            group = self._group("offers", OFFER_TITLE, OFFER_HINT)
            for o in offers:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(o["title"])))
                buttons = []
                for choice in ("break", "keep"):
                    b = Gtk.Button(label=OFFER_LABELS[choice], valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, t=o["title"], c=choice: self._answer(t, c))
                    self.buttons[("offer", o["title"], choice)] = b
                    buttons.append(b)
                line = self._with_buttons(row, buttons)
                group.add(row if line is None else self._card(row, line))
        if restore:  # shown once: items taken off their lists earlier, each can go back
            group = self._group("restore", RESTORE_TITLE, RESTORE_HINT)
            for title in restore:
                row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(title)))
                buttons = []
                for put_back, label in ((True, RESTORE_PUT_BACK), (False, RESTORE_LEAVE)):
                    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                    b.add_css_class("flat")
                    b.connect("clicked", lambda _b, t=title, p=put_back: self._restore(p, t))
                    self.buttons[("restore", title, "put_back" if put_back else "leave")] = b
                    buttons.append(b)
                line = self._with_buttons(row, buttons)
                group.add(row if line is None else self._card(row, line))
            leave_all = Gtk.Button(label=RESTORE_LEAVE_ALL, halign=Gtk.Align.END, margin_top=6)
            leave_all.connect("clicked", lambda _b: self._restore(False))
            self.buttons[("restore", "leave_all")] = leave_all
            group.add(leave_all)
        if not keep and not drop and not archive and not restore and not ask and not offers:
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
                    for part in self._row(r, ("undrop",)):
                        if part is not None:
                            expander.add_row(part)
                group.add(expander)

    @staticmethod
    def _when(r: Dict[str, Any]) -> str:
        at = r.get("at")
        return short_clock(at) if isinstance(at, datetime) else (r.get("when") or "")

    def _row(self, r: Dict[str, Any], actions: tuple) -> tuple:
        """(the item's row, the line of its buttons under it on a narrow page, else None)."""
        row = Adw.ActionRow(title=GLib.markup_escape_text(title_of(r["title"])),  # short title; actions use the item
                            subtitle=GLib.markup_escape_text(r.get("why") or ""))
        buttons = []
        for action in actions:
            b = Gtk.Button(label=LABELS[action], valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, t=r["title"], a=action: self._act(t, a))
            self.buttons[(r["title"], action)] = b
            buttons.append(b)
        return row, self._with_buttons(row, buttons)

    def _restore(self, put_back: bool, item: str = "") -> None:
        args = {"put_back": put_back, **({"item": item} if item else {})}
        out = self.run_tool("restore_old_drops", args) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    def _answer(self, title: str, choice: str) -> None:
        out = self.run_tool("still_want", {"item": title, "choice": choice}) if self.run_tool else ""
        if choice == "break":  # its result is for the assistant (what to propose next), not for reading here
            if out and not out.startswith("NOT_DONE"):
                if self.on_notice:
                    self.on_notice(BROKEN_NOTE.format(title=title_of(title)))
                if self.on_break:
                    self.on_break(title)
        elif out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    def _act(self, title: str, action: str) -> None:
        out = self.run_tool("leftover_action", {"item": title, "action": action}) if self.run_tool else ""
        if out and self.on_notice:
            self.on_notice(out)
        self.refresh()

    def _add_rows(self, group: Adw.PreferencesGroup, entries: List[tuple]) -> None:
        """(left-column text, (row, its button line or None)) as the Today tab lays them out: one card per row, the
        text on the left."""
        for when, (row, line) in entries:
            box = Gtk.Box(spacing=10, margin_bottom=6)
            box.add_css_class("agenda-block")
            label = Gtk.Label(label=when, xalign=1, yalign=0, margin_top=15, wrap=True, justify=Gtk.Justification.RIGHT)
            label.set_size_request(GUTTER, -1)
            label.set_max_width_chars(9)
            label.add_css_class("caption-heading")
            label.add_css_class("dim-label")
            box.append(label)
            box.append(self._card(row, line))
            box.when_text, box.rows = when, [row]  # read by the tests
            group.add(box)


def _day_text(day: str) -> str:
    try:
        return date.fromisoformat(day).strftime("%a %b %-d")
    except ValueError:
        return day
