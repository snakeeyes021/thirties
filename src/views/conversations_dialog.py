"""All conversations: every conversation, by day, newest first, with a search. Picking one opens its day
with that conversation in the chat. The listing is thirties_core/conversation_index.py."""

from __future__ import annotations

from datetime import date
from typing import Callable, Dict, Optional

from gi.repository import Adw, GLib, Gtk

from thirties_core.conversation_index import all_conversations, day_heading


class AllConversationsDialog(Adw.Dialog):

    def __init__(self, store, today: Optional[date], tz, on_open: Callable[[date, str], None]) -> None:
        super().__init__(title="All Conversations", content_width=520, content_height=640)
        self.store, self.today, self.tz, self.on_open = store, today, tz, on_open
        self.rows: Dict[str, Adw.ActionRow] = {}
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.search = Gtk.SearchEntry(placeholder_text="Search conversations", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.fill())
        bar = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        bar.append(self.search)
        view.add_top_bar(bar)
        self.page = Adw.PreferencesPage()
        self.empty = Adw.StatusPage(icon_name="chat-message-new-symbolic", title="No Conversations Yet")
        self.empty.add_css_class("compact")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.page, "list")
        self.stack.add_named(self.empty, "empty")
        view.set_content(self.stack)
        self.set_child(view)
        self.groups = []
        self.fill()

    def fill(self) -> None:
        for g in self.groups:
            self.page.remove(g)
        self.groups, self.rows = [], {}
        query = self.search.get_text().strip()
        days = all_conversations(self.store, query, self.tz)
        for day, entries in days:
            group = Adw.PreferencesGroup(title=day_heading(day, self.today))
            for e in entries:
                count = f"{e.messages} message{'s' if e.messages != 1 else ''}"
                row = Adw.ActionRow(title=GLib.markup_escape_text(e.title or "Conversation"), activatable=True,
                                    subtitle=GLib.markup_escape_text(
                                        " · ".join(x for x in (e.started, count) if x)
                                        + (f"\n{e.summary}" if e.summary else "")),
                                    subtitle_lines=3)
                row.set_title_lines(1)
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda _r, d=day, i=e.id: self._open(d, i))
                group.add(row)
                self.rows[e.id] = row
            self.page.add(group)
            self.groups.append(group)
        if not days:
            self.empty.set_title("Nothing Found" if query else "No Conversations Yet")
            self.empty.set_description("Try other words." if query else "What you say to Thirties shows up here.")
        self.stack.set_visible_child_name("list" if days else "empty")

    def _open(self, day: date, conversation_id: str) -> None:
        if self.get_parent() is not None:  # presented (a test may build it without)
            self.close()
        self.on_open(day, conversation_id)


__all__ = ["AllConversationsDialog"]
