"""Conversational planning interface."""

from __future__ import annotations

from typing import Callable, Optional
import threading
from gi.repository import Gtk, Adw, Pango, GLib, Gdk

from thirties_core.conversation import ConversationManager
from thirties_core.models import DayPlan


class ChatMessageWidget(Gtk.Box):
    """Renders a single message bubble in the conversation stream."""

    def __init__(self, role: str, text: str, on_action_click: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.role = role
        self.text = text
        self.on_action_click = on_action_click

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._build_bubble()

    def _build_bubble(self) -> None:
        bubble_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        if self.role == "user":
            bubble_box.set_halign(Gtk.Align.END)
            user_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            user_col.set_halign(Gtk.Align.END)

            label = Gtk.Label(label=self.text)
            label.set_selectable(True)
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_max_width_chars(44)
            label.set_xalign(1.0)
            label.add_css_class("chat-bubble-user")
            user_col.append(label)

            action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            action_row.set_halign(Gtk.Align.END)
            action_row.set_margin_top(2)

            copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
            copy_btn.add_css_class("flat")
            copy_btn.add_css_class("circular")
            copy_btn.set_tooltip_text("Copy message")
            copy_btn.connect("clicked", self._on_copy_clicked)
            action_row.append(copy_btn)

            user_col.append(action_row)
            bubble_box.append(user_col)
        else:
            bubble_box.set_halign(Gtk.Align.START)

            avatar = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
            avatar.set_pixel_size(20)
            avatar.set_valign(Gtk.Align.START)
            avatar.set_margin_top(4)
            bubble_box.append(avatar)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            text_col.set_halign(Gtk.Align.START)

            label = Gtk.Label(label=self.text)
            label.set_selectable(True)
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_max_width_chars(48)
            label.set_xalign(0.0)
            label.add_css_class("chat-bubble-agent")
            text_col.append(label)

            action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            action_row.set_halign(Gtk.Align.START)
            action_row.set_margin_top(2)

            copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
            copy_btn.add_css_class("flat")
            copy_btn.add_css_class("circular")
            copy_btn.set_tooltip_text("Copy response")
            copy_btn.connect("clicked", self._on_copy_clicked)
            action_row.append(copy_btn)

            # Suggest quick confirmation pills if text mentions ambiguous event or actions
            if self.on_action_click:
                lower = self.text.lower()
                if "attending" in lower or "appointment" in lower or "unconfirmed" in lower:
                    yes_btn = Gtk.Button(label="Yes, Attending")
                    yes_btn.add_css_class("suggested-action")
                    yes_btn.add_css_class("pill")
                    yes_btn.connect("clicked", lambda _: self.on_action_click("Yes, attending"))
                    action_row.append(yes_btn)

                    no_btn = Gtk.Button(label="Decline")
                    no_btn.add_css_class("pill")
                    no_btn.connect("clicked", lambda _: self.on_action_click("Decline"))
                    action_row.append(no_btn)

            text_col.append(action_row)
            bubble_box.append(text_col)

        self.append(bubble_box)

    def _on_copy_clicked(self, btn: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display:
            clipboard = display.get_clipboard()
            clipboard.set(self.text)
        btn.set_icon_name("object-select-symbolic")
        btn.set_tooltip_text("Copied!")
        GLib.timeout_add(1500, self._reset_copy_btn, btn)

    def _reset_copy_btn(self, btn: Gtk.Button) -> bool:
        btn.set_icon_name("edit-copy-symbolic")
        btn.set_tooltip_text("Copy message")
        return False


class ChatPanel(Gtk.Box):
    """Dedicated full-height conversational stream for negotiating the daily schedule."""

    def __init__(
        self,
        conversation_manager: ConversationManager,
        on_plan_updated: Optional[Callable[[DayPlan], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.conversation_manager = conversation_manager
        self.on_plan_updated = on_plan_updated

        self.set_hexpand(True)
        self.set_vexpand(True)

        self._build_stream()
        self._build_input_bar()
        self._send_initial_greeting()

    def set_conversation_manager(
        self,
        manager: ConversationManager,
        on_plan_updated: Optional[Callable[[DayPlan], None]] = None,
    ) -> None:
        self.conversation_manager = manager
        self.on_plan_updated = on_plan_updated

        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)

        self._send_initial_greeting()

    def _build_stream(self) -> None:
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)
        clamp.set_tightening_threshold(500)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.messages_box.set_margin_top(16)
        self.messages_box.set_margin_bottom(16)
        self.messages_box.set_margin_start(16)
        self.messages_box.set_margin_end(16)

        clamp.set_child(self.messages_box)
        self.scrolled.set_child(clamp)
        self.append(self.scrolled)

    def _build_input_bar(self) -> None:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)
        clamp.set_tightening_threshold(500)

        input_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_container.set_margin_start(16)
        input_container.set_margin_end(16)
        input_container.set_margin_top(8)
        input_container.set_margin_bottom(16)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask or plan your day...")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send_clicked)

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.connect("clicked", self._on_send_clicked)

        input_container.append(self.entry)
        input_container.append(self.send_btn)
        clamp.set_child(input_container)
        self.append(clamp)

    def _send_initial_greeting(self) -> None:
        plan = self.conversation_manager.day_plan
        ambiguous = self.conversation_manager.ambiguous_events

        greeting = (
            f"Good day! You have {plan.daylight_available_count} Daylight Thirties "
            f"and {plan.dark_available_count} Dark Thirties available.\n\n"
        )
        if ambiguous:
            ev = ambiguous[0]
            greeting += f"You have an unconfirmed event: '{ev.summary}'. Are you attending?"
        else:
            greeting += "What would you like to focus on today?"

        self.add_message("assistant", greeting)

    def add_message(self, role: str, text: str) -> None:
        msg_widget = ChatMessageWidget(
            role=role,
            text=text,
            on_action_click=self.send_user_text,
        )
        self.messages_box.append(msg_widget)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        def do_scroll():
            vadj = self.scrolled.get_vadjustment()
            vadj.set_value(vadj.get_upper() - vadj.get_page_size())
            return False
        GLib.idle_add(do_scroll)

    def _on_send_clicked(self, _widget) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.send_user_text(text)

    def send_user_text(self, text: str) -> None:
        self.add_message("user", text)
        self.entry.set_sensitive(False)
        self.send_btn.set_sensitive(False)

        def worker():
            reply = self.conversation_manager.send_user_message(text)

            def update_ui():
                self.add_message("assistant", reply)
                self.entry.set_sensitive(True)
                self.send_btn.set_sensitive(True)
                self.entry.grab_focus()
                if self.on_plan_updated:
                    self.on_plan_updated(self.conversation_manager.day_plan)
                return False

            GLib.idle_add(update_ui)

        threading.Thread(target=worker, daemon=True).start()
