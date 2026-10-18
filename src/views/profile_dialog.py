"""Dialog showing what the assistant has learned: the user profile and its memory notes."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from gi.repository import Adw, Gtk, Pango


def _note_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return ""


class ProfileMemoryDialog(Adw.Dialog):
    """Read and correct the user profile (markdown) and the assistant's memory notes.

    ``profile`` is a ``ProfileStore`` and ``memory`` a ``MemoryStore``; either may be
    None when no database is attached. ``on_changed`` runs after every save or delete.
    """

    def __init__(self, profile, memory, on_changed: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.profile = profile
        self.memory = memory
        self.on_changed = on_changed

        self.set_title("Profile & Memory")
        self.set_content_width(680)
        self.set_content_height(620)

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._build_profile_page(), "profile", "Profile", "user-info-symbolic")
        self.stack.add_titled_with_icon(self._build_memory_page(), "memory", "Memory", "user-bookmarks-symbolic")

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar.add_top_bar(header)

        toolbar.set_content(self.stack)
        self.toast_overlay.set_child(toolbar)
        self.set_child(self.toast_overlay)

        self.load_profile()
        self.load_memory()

    def _toast(self, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=2))

    def _changed(self) -> None:
        if self.on_changed:
            self.on_changed()

    # --- profile ---

    def _build_profile_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(16)
        page.set_margin_end(16)
        page.set_margin_top(12)
        page.set_margin_bottom(16)

        intro = Gtk.Label(label=(
            "What the assistant knows about your life. Each \"## \" heading is a section; "
            "edit freely and save."
        ))
        intro.set_wrap(True)
        intro.set_xalign(0.0)
        intro.add_css_class("dim-label")
        page.append(intro)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("card")

        self.profile_view = Gtk.TextView()
        self.profile_view.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.profile_view.set_monospace(True)
        self.profile_view.set_top_margin(10)
        self.profile_view.set_bottom_margin(10)
        self.profile_view.set_left_margin(12)
        self.profile_view.set_right_margin(12)
        self.profile_view.add_css_class("profile-editor")
        self.profile_buffer = self.profile_view.get_buffer()
        self.profile_buffer.connect("modified-changed", self._on_profile_modified)
        scrolled.set_child(self.profile_view)
        page.append(scrolled)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", lambda _b: self.load_profile())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._on_save_profile)
        buttons.append(self.revert_btn)
        buttons.append(self.save_btn)
        page.append(buttons)

        if self.profile is None:
            page.set_sensitive(False)
        return page

    def load_profile(self) -> None:
        text = self.profile.as_markdown() if self.profile is not None else ""
        self.profile_buffer.set_text(text)
        self.profile_buffer.set_modified(False)
        self._on_profile_modified(self.profile_buffer)

    def _on_profile_modified(self, buf: Gtk.TextBuffer) -> None:
        modified = buf.get_modified()
        self.save_btn.set_sensitive(modified)
        self.revert_btn.set_sensitive(modified)

    def _on_save_profile(self, _btn=None) -> None:
        if self.profile is None:
            return
        start, end = self.profile_buffer.get_bounds()
        text = self.profile_buffer.get_text(start, end, True)
        # as_markdown() shows "(empty)" under empty sections; don't store it next to new text.
        text = "\n".join(line for line in text.splitlines() if line.strip() != "(empty)")
        self.profile.save_markdown(text)
        self.load_profile()
        self._toast("Profile saved")
        self._changed()

    # --- memory ---

    def _build_memory_page(self) -> Gtk.Widget:
        self.memory_page = Adw.PreferencesPage()
        self.memory_group = Adw.PreferencesGroup()
        self.memory_group.set_title("Memory notes")
        self.memory_group.set_description(
            "How things work for you, as the assistant understands it. Edit a note and press "
            "Enter or the check mark to save it; delete notes that are wrong."
        )
        self.memory_page.add(self.memory_group)

        self.memory_empty = Adw.StatusPage()
        self.memory_empty.set_icon_name("user-bookmarks-symbolic")
        self.memory_empty.set_title("No Memory Notes Yet")
        self.memory_empty.set_description("The assistant saves notes here as it learns how you like things done.")
        self.memory_empty.add_css_class("compact")

        self.memory_stack = Gtk.Stack()
        self.memory_stack.add_named(self.memory_page, "notes")
        self.memory_stack.add_named(self.memory_empty, "empty")
        return self.memory_stack

    def load_memory(self) -> None:
        for row in getattr(self, "_note_rows", []):
            self.memory_group.remove(row)
        self._note_rows: list[Adw.EntryRow] = []
        notes = self.memory.notes() if self.memory is not None else []
        for note in notes:
            row = Adw.EntryRow()
            row.note_id = note.id
            row.set_title(" · ".join(p for p in (_note_date(note.created_at), note.source) if p))
            row.set_text(note.text)
            row.set_show_apply_button(True)
            row.connect("apply", self._on_note_apply)

            delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
            delete_btn.add_css_class("flat")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.set_tooltip_text("Delete note")
            delete_btn.connect("clicked", self._on_note_delete, row)
            row.add_suffix(delete_btn)

            self.memory_group.add(row)
            self._note_rows.append(row)
        self.memory_stack.set_visible_child_name("notes" if notes else "empty")

    def _on_note_apply(self, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        if not text:
            self._on_note_delete(None, row)
            return
        if self.memory.update(row.note_id, text):
            self._toast("Note saved")
            self._changed()

    def _on_note_delete(self, _btn, row: Adw.EntryRow) -> None:
        if self.memory.forget(row.note_id):
            self._toast("Note deleted")
            self._changed()
        self.load_memory()
