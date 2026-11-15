"""The notes the user left for the engineers (engineer_notes.py): read-only, newest first, each with when and where
it was said and the message it was part of."""

from __future__ import annotations

from datetime import datetime

from gi.repository import Adw, GLib

WHERE = {"app": "in the app", "telegram": "on Telegram", "web": "on the phone page"}


def when_text(iso: str) -> str:
    """'Sunday, November 15, 2:10 PM' (no seconds, no zone)."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return dt.strftime("%A, %B %-d, ") + dt.strftime("%I:%M %p").lstrip("0")


class EngineerNotesDialog(Adw.Dialog):

    def __init__(self, notes) -> None:
        """``notes``: EngineerNote objects (id, at, via, text, message)."""
        super().__init__(title="Notes to the Engineers", content_width=520, content_height=560)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.rows = []
        notes = sorted(notes, key=lambda n: n.at, reverse=True)
        if not notes:
            empty = Adw.StatusPage(icon_name="mail-unread-symbolic", title="No Notes",
                                   description="A message that starts “note to the engineers” is kept here.")
            empty.add_css_class("compact")
            view.set_content(empty)
        else:
            page = Adw.PreferencesPage()
            group = Adw.PreferencesGroup(description="Kept for the people who build Thirties. The assistant "
                                                     "doesn't act on them.")
            for n in notes:
                said = [x for x in (when_text(n.at), WHERE.get(n.via, "")) if x]
                lines = [", ".join(said)] if said else []
                if n.message and " ".join(n.message.split()) != " ".join(n.text.split()):
                    lines.append("Said in: " + " ".join(n.message.split()))
                row = Adw.ActionRow(title=GLib.markup_escape_text(n.text),
                                    subtitle=GLib.markup_escape_text("\n".join(lines)), title_selectable=True)
                row.set_title_lines(0)
                row.set_subtitle_lines(0)
                group.add(row)
                self.rows.append(row)
            page.add(group)
            view.set_content(page)
        self.set_child(view)


__all__ = ["EngineerNotesDialog", "when_text"]
