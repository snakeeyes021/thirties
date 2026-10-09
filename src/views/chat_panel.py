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

            # Suggest quick confirmation pills ONLY for unconfirmed calendar events
            if self.on_action_click:
                lower = self.text.lower()
                if "unconfirmed event" in lower or ("are you attending" in lower and "event" in lower):
                    pills_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                    def on_pill(choice: str) -> None:
                        action_row.remove(pills_box)
                        self.on_action_click(choice)

                    yes_btn = Gtk.Button(label="Yes, Attending")
                    yes_btn.add_css_class("suggested-action")
                    yes_btn.add_css_class("pill")
                    yes_btn.connect("clicked", lambda _: on_pill("Yes, attending"))
                    pills_box.append(yes_btn)

                    no_btn = Gtk.Button(label="Decline")
                    no_btn.add_css_class("pill")
                    no_btn.connect("clicked", lambda _: on_pill("Decline"))
                    pills_box.append(no_btn)

                    action_row.append(pills_box)

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
        self.chat_history: list[tuple[str, str]] = []

        self._build_header()
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

        self.chat_history.clear()
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)

        self._send_initial_greeting()

    def _build_header(self) -> None:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)
        clamp.set_tightening_threshold(500)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_margin_start(16)
        header_box.set_margin_end(16)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(4)

        title_lbl = Gtk.Label(label="Planning Assistant")
        title_lbl.add_css_class("heading")
        title_lbl.set_xalign(0.0)
        title_lbl.set_hexpand(True)
        header_box.append(title_lbl)

        self.copy_all_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_all_btn.add_css_class("flat")
        self.copy_all_btn.set_tooltip_text("Copy entire chat transcript")
        self.copy_all_btn.connect("clicked", self._on_copy_all_clicked)
        header_box.append(self.copy_all_btn)

        clamp.set_child(header_box)
        self.append(clamp)

    def _on_copy_all_clicked(self, btn: Gtk.Button) -> None:
        lines = []
        for role, text in self.chat_history:
            speaker = "User" if role == "user" else "Assistant"
            lines.append(f"{speaker}:\n{text}\n")
        transcript = "\n".join(lines).strip()
        display = Gdk.Display.get_default()
        if display and transcript:
            clipboard = display.get_clipboard()
            clipboard.set(transcript)
        btn.set_icon_name("object-select-symbolic")
        btn.set_tooltip_text("Transcript copied!")
        GLib.timeout_add(1500, self._reset_copy_all_btn, btn)

    def _reset_copy_all_btn(self, btn: Gtk.Button) -> bool:
        btn.set_icon_name("edit-copy-symbolic")
        btn.set_tooltip_text("Copy entire chat transcript")
        return False

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

        # Auto-scroll managed after message insertion

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

        # Multi-line word-wrapping text view in scrolled container
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_min_content_height(38)
        scrolled.set_max_content_height(130)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("chat-entry-box")

        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.text_view.set_hexpand(True)
        self.text_view.set_top_margin(6)
        self.text_view.set_bottom_margin(6)
        self.text_view.set_left_margin(8)
        self.text_view.set_right_margin(8)

        # Key controller for Enter (send) vs Shift+Enter (newline)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(key_ctrl)

        scrolled.set_child(self.text_view)
        overlay.set_child(scrolled)

        # Dim placeholder label
        self.placeholder = Gtk.Label(label="Ask or plan your day...")
        self.placeholder.add_css_class("dim-label")
        self.placeholder.set_halign(Gtk.Align.START)
        self.placeholder.set_valign(Gtk.Align.START)
        self.placeholder.set_margin_start(10)
        self.placeholder.set_margin_top(8)
        self.placeholder.set_can_target(False)
        overlay.add_overlay(self.placeholder)

        self.text_buffer = self.text_view.get_buffer()
        self.text_buffer.connect("changed", self._on_buffer_changed)

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.set_margin_bottom(2)
        self.send_btn.connect("clicked", self._on_send_clicked)

        input_container.append(overlay)
        input_container.append(self.send_btn)
        clamp.set_child(input_container)
        self.append(clamp)

    def _on_buffer_changed(self, buf: Gtk.TextBuffer) -> None:
        self.placeholder.set_visible(buf.get_char_count() == 0)

    def _on_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                self._on_send_clicked(None)
                return True
        return False

    def _send_initial_greeting(self) -> None:
        from datetime import date
        plan = self.conversation_manager.day_plan
        ambiguous = self.conversation_manager.ambiguous_events

        target_str = plan.target_date.strftime("%A, %B %d")
        day_prefix = f"Today ({target_str})" if plan.target_date == date.today() else target_str

        greeting = (
            f"Good day! You are planning {day_prefix}.\n"
            f"You have {plan.daylight_available_count}/{plan.daylight_discretionary_total} Daylight Thirties "
            f"and {plan.dark_available_count}/{plan.dark_discretionary_total} Dark Thirties available.\n\n"
        )
        if ambiguous:
            ev = ambiguous[0]
            greeting += f"You have an unconfirmed event: '{ev.summary}'. Are you attending?"
        else:
            greeting += "What would you like to focus on today?"

        self.add_message("assistant", greeting)

    def add_message(self, role: str, text: str) -> None:
        self.chat_history.append((role, text))
        msg_widget = ChatMessageWidget(
            role=role,
            text=text,
            on_action_click=self.send_user_text,
        )
        self.messages_box.append(msg_widget)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        def do_scroll():
            last = self.messages_box.get_last_child()
            vp = self.scrolled.get_child()
            if vp and hasattr(vp, "scroll_to") and last:
                try:
                    vp.scroll_to(last)
                except Exception:
                    adj = self.scrolled.get_vadjustment()
                    target = adj.get_upper() - adj.get_page_size()
                    if target > 0:
                        adj.set_value(target)
            else:
                adj = self.scrolled.get_vadjustment()
                target = adj.get_upper() - adj.get_page_size()
                if target > 0:
                    adj.set_value(target)
            return False
        GLib.idle_add(do_scroll)
        GLib.timeout_add(80, do_scroll)

    def _on_send_clicked(self, _widget) -> None:
        start, end = self.text_buffer.get_bounds()
        text = self.text_buffer.get_text(start, end, True).strip()
        if not text:
            return
        self.text_buffer.set_text("")
        self.send_user_text(text)

    def send_user_text(self, text: str) -> None:
        self.add_message("user", text)
        self.text_view.set_sensitive(False)
        self.send_btn.set_sensitive(False)

        def worker():
            reply = self.conversation_manager.send_user_message(text)

            def update_ui():
                self.add_message("assistant", reply)
                self.text_view.set_sensitive(True)
                self.send_btn.set_sensitive(True)
                self.text_view.grab_focus()
                if self.on_plan_updated:
                    self.on_plan_updated(self.conversation_manager.day_plan)
                return False

            GLib.idle_add(update_ui)

        threading.Thread(target=worker, daemon=True).start()
