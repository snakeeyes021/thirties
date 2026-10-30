"""Conversational planning interface."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable, Optional
import threading
from gi.repository import Gtk, Adw, Pango, GLib, Gdk

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.conversation import ConversationManager
from thirties_core.models import DayPlan

logger = logging.getLogger(__name__)


def persist_conversation(manager: ConversationManager) -> None:
    """Store the manager's current conversation after a turn.

    ``ConversationManager.send_user_message`` does not save the transcript itself
    (only ``add_assistant_message`` does), so without this a desktop turn would be
    lost on the next day switch or refresh. Remove once the core saves every turn.
    """
    save = getattr(manager, "_save", None)
    if callable(save):
        try:
            save()
        except Exception:
            logger.exception("Could not save the conversation")


class ChatMessageWidget(Gtk.Box):
    """Renders a single message bubble in the conversation stream."""

    def __init__(
        self,
        role: str,
        text: str,
        on_action_click: Optional[Callable[[str], None]] = None,
        offer_actions: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.role = role
        self.text = text
        self.on_action_click = on_action_click if offer_actions else None

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
    """Conversation with the planning assistant about the day being shown.

    The transcript always comes from the stored conversation (``history()``), so
    it matches what the Telegram bridge sees. A greeting is shown while the
    conversation is empty, and stored as its first message once the user replies.
    """

    def __init__(
        self,
        conversation_manager: ConversationManager | None = None,
        on_plan_updated: Optional[Callable[[DayPlan], None]] = None,
        external_header: bool = False,
    ) -> None:
        """With ``external_header``, the conversation picker and buttons are left for the window to put in
        the chat pane's header bar (``conversations_btn``, ``header_buttons``)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._external_header = external_header
        self.conversation_manager = conversation_manager
        self.on_plan_updated = on_plan_updated
        # Held while a turn runs; the window shares it so its background refresh never reloads the manager
        # under a turn (or a turn starts mid-reload: it waits, off the UI thread).
        self.store_lock = threading.Lock()

        self.set_hexpand(True)
        self.set_vexpand(True)
        self.chat_history: list[tuple[str, str]] = []
        self._rendered: list[dict] | None = None
        self._shown_greeting: str | None = None
        # The manager whose turn is in flight (None when idle).
        self._busy_manager: ConversationManager | None = None
        # Conversations the user explicitly switched to, per day; refresh() would
        # otherwise move back to the day's latest one.
        self._pinned: dict[date, str] = {}

        self._build_header()
        self._build_stream()
        self._build_input_bar()
        if conversation_manager is not None:
            self.set_conversation_manager(conversation_manager, on_plan_updated)

    # --- manager & conversations ---

    @property
    def is_busy(self) -> bool:
        return self._busy_manager is not None

    def set_conversation_manager(
        self,
        manager: ConversationManager,
        on_plan_updated: Optional[Callable[[DayPlan], None]] = None,
    ) -> None:
        self.conversation_manager = manager
        if on_plan_updated is not None:
            self.on_plan_updated = on_plan_updated
        self._apply_pin()
        self._update_input_sensitivity()
        self.render(force=True)

    def _day(self) -> date:
        return self.conversation_manager.day_plan.target_date

    def _apply_pin(self) -> None:
        manager = self.conversation_manager
        pinned = self._pinned.get(self._day())
        current = manager.conversation.id if manager.conversation else None
        if pinned and pinned != current and not manager.switch_conversation(pinned):
            self._pinned.pop(self._day(), None)

    def after_store_refresh(self) -> None:
        """Re-render after ``conversation_manager.refresh()`` picked up changes from other clients."""
        if self.conversation_manager is None:
            return
        self._apply_pin()
        self.render()

    def new_conversation(self) -> None:
        if self.conversation_manager is None or self.is_busy:
            return
        self.conversation_manager.new_conversation()
        self._pinned.pop(self._day(), None)
        self.render(force=True)
        self.text_view.grab_focus()

    def switch_conversation(self, conversation_id: str) -> None:
        manager = self.conversation_manager
        if manager is None or self.is_busy or not manager.switch_conversation(conversation_id):
            return
        convs = manager.conversations_for_day()
        if convs and convs[-1].id == conversation_id:
            self._pinned.pop(self._day(), None)
        else:
            self._pinned[self._day()] = conversation_id
        self.render(force=True)
        self.text_view.grab_focus()

    # --- header ---

    def _build_header(self) -> None:
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(4)

        # Conversation switcher: the current conversation's title, opening a list of the day's conversations
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.conversation_title = Gtk.Label(label="Planning Assistant")
        self.conversation_title.add_css_class("heading")
        self.conversation_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.conversation_title.set_xalign(0.0)
        self.conversation_title.set_hexpand(True)
        btn_box.append(self.conversation_title)
        btn_box.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))

        self.conversations_list = Gtk.ListBox()
        self.conversations_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.conversations_list.add_css_class("navigation-sidebar")
        self.conversations_list.connect("row-activated", self._on_conversation_row_activated)

        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroller.set_propagate_natural_height(True)
        list_scroller.set_max_content_height(360)
        list_scroller.set_min_content_width(280)
        list_scroller.set_child(self.conversations_list)

        self.conversations_popover = Gtk.Popover()
        self.conversations_popover.set_child(list_scroller)
        self.conversations_popover.connect("show", lambda _p: self._populate_conversations())

        self.conversations_btn = Gtk.MenuButton()
        self.conversations_btn.set_child(btn_box)
        self.conversations_btn.add_css_class("flat")
        self.conversations_btn.set_hexpand(True)
        self.conversations_btn.set_tooltip_text("Switch between this day's conversations")
        self.conversations_btn.set_popover(self.conversations_popover)
        header_box.append(self.conversations_btn)

        self.new_conv_btn = Gtk.Button(icon_name="chat-message-new-symbolic")
        self.new_conv_btn.add_css_class("flat")
        self.new_conv_btn.set_tooltip_text("New conversation")
        self.new_conv_btn.connect("clicked", lambda _b: self.new_conversation())
        header_box.append(self.new_conv_btn)

        self.copy_all_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_all_btn.add_css_class("flat")
        self.copy_all_btn.set_tooltip_text("Copy entire chat transcript")
        self.copy_all_btn.connect("clicked", self._on_copy_all_clicked)
        header_box.append(self.copy_all_btn)

        self.header_buttons = [self.new_conv_btn, self.copy_all_btn]
        if self._external_header:
            for widget in [self.conversations_btn] + self.header_buttons:
                header_box.remove(widget)
            self.conversations_btn.set_hexpand(False)
            self.conversation_title.set_hexpand(False)
            self.conversation_title.set_max_width_chars(28)
            return
        self.append(header_box)
        self.append(Gtk.Separator())

    def _conversation_time(self, iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
            tz = self.conversation_manager.day_plan.sunrise.tzinfo
            return dt.astimezone(tz).strftime("%I:%M %p").lstrip("0")
        except (ValueError, TypeError):
            return ""

    def _populate_conversations(self) -> None:
        while row := self.conversations_list.get_first_child():
            self.conversations_list.remove(row)
        manager = self.conversation_manager
        if manager is None:
            return
        current = manager.conversation.id if manager.conversation else None
        convs = manager.conversations_for_day()
        if not convs:
            empty = Gtk.Label(label="No conversations yet for this day")
            empty.add_css_class("dim-label")
            empty.set_margin_top(12)
            empty.set_margin_bottom(12)
            self.conversations_list.append(empty)
            return
        for conv in reversed(convs):  # newest first
            row = Gtk.ListBoxRow()
            row.conversation_id = conv.id
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(6)
            box.set_margin_end(6)

            time_lbl = Gtk.Label(label=self._conversation_time(conv.created_at))
            time_lbl.add_css_class("caption")
            time_lbl.add_css_class("dim-label")
            time_lbl.add_css_class("numeric")
            time_lbl.set_size_request(64, -1)
            time_lbl.set_xalign(0.0)
            box.append(time_lbl)

            text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_col.set_hexpand(True)
            title_lbl = Gtk.Label(label=conv.title or "New conversation")
            title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            title_lbl.set_width_chars(26)
            title_lbl.set_max_width_chars(40)
            title_lbl.set_xalign(0.0)
            turns = conv.user_turns
            sub_lbl = Gtk.Label(label=f"{turns} message{'s' if turns != 1 else ''}" if turns else "Empty")
            sub_lbl.add_css_class("caption")
            sub_lbl.add_css_class("dim-label")
            sub_lbl.set_xalign(0.0)
            text_col.append(title_lbl)
            text_col.append(sub_lbl)
            box.append(text_col)

            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_opacity(1.0 if conv.id == current else 0.0)
            box.append(check)

            row.set_child(box)
            self.conversations_list.append(row)

    def _on_conversation_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        conv_id = getattr(row, "conversation_id", None)
        self.conversations_popover.popdown()
        if conv_id:
            self.switch_conversation(conv_id)

    def _update_header(self) -> None:
        manager = self.conversation_manager
        conv = manager.conversation if manager else None
        title = conv.title if conv and conv.title else "New conversation"
        self.conversation_title.set_text(title)
        self.new_conv_btn.set_sensitive(not self.is_busy and bool(conv and conv.messages))

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

    # --- stream ---

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
        self.messages_box.set_margin_start(8)
        self.messages_box.set_margin_end(8)

        clamp.set_child(self.messages_box)
        self.scrolled.set_child(clamp)
        self.append(self.scrolled)

    def _build_input_bar(self) -> None:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)
        clamp.set_tightening_threshold(500)

        input_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_container.set_margin_start(12)
        input_container.set_margin_end(12)
        input_container.set_margin_top(8)
        input_container.set_margin_bottom(12)

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
        self.send_btn.set_tooltip_text("Send (Enter)")
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

    def focus_input(self) -> None:
        if self.text_view.get_sensitive():
            self.text_view.grab_focus()

    def _update_input_sensitivity(self) -> None:
        # The input stays usable on other days while a turn is in flight elsewhere.
        busy_here = self._busy_manager is not None and self._busy_manager is self.conversation_manager
        self.text_view.set_sensitive(not busy_here)
        self.send_btn.set_sensitive(not busy_here)

    def _greeting(self) -> str:
        manager = self.conversation_manager
        plan = manager.day_plan
        ambiguous = manager.ambiguous_events

        gen = manager.scheduler.config.general if manager.scheduler else None
        lat = gen.latitude if gen else 40.7128
        lon = gen.longitude if gen else -74.0060
        tz_name = gen.timezone if gen else "UTC"
        diurnal_today = get_diurnal_date(lat=lat, lon=lon, tz_name=tz_name)

        target_str = plan.target_date.strftime("%A, %B %d")
        is_today = plan.target_date == diurnal_today
        day_prefix = f"Today ({target_str})" if is_today else target_str

        now = get_current_time(plan.sunrise.tzinfo)
        live = plan.day_start.timestamp() <= now.timestamp() < plan.day_end.timestamp()
        counts = plan.availability(now if live else None)
        available = "still available" if live else "available"
        greeting = (
            f"Good day! You are planning {day_prefix}.\n"
            f"You have {counts['daylight_open']}/{counts['daylight_total']} Daylight Thirties "
            f"and {counts['dark_open']}/{counts['dark_total']} Dark Thirties {available}.\n\n"
        )
        if ambiguous:
            ev = ambiguous[0]
            greeting += f"You have an unconfirmed event: '{ev.summary}'. Are you attending?"
        else:
            greeting += "What would you like to focus on today?" if is_today else "What would you like to plan?"
        return greeting

    def render(self, force: bool = False) -> None:
        """Show the stored transcript of the current conversation (greeting if it is empty)."""
        manager = self.conversation_manager
        if manager is None:
            return
        history = manager.history()
        self._update_header()
        if not force and history == self._rendered:
            return
        self._rendered = [dict(m) for m in history]

        self.chat_history.clear()
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)

        self._shown_greeting = None
        if not history:
            self._shown_greeting = self._greeting()
            self._append_bubble("assistant", self._shown_greeting, offer_actions=True, record=False)
        for i, msg in enumerate(history):
            self._append_bubble(msg["role"], msg["content"], offer_actions=(i == len(history) - 1))
        if self._busy_manager is manager:
            self._append_thinking()
        self._scroll_to_bottom()

    def _append_bubble(self, role: str, text: str, offer_actions: bool = False, record: bool = True) -> None:
        if record:
            self.chat_history.append((role, text))
        self.messages_box.append(ChatMessageWidget(
            role=role,
            text=text,
            on_action_click=self.send_user_text,
            offer_actions=offer_actions,
        ))

    def _append_thinking(self) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(12)
        row.set_margin_top(6)
        row.add_css_class("chat-thinking")
        spinner = Adw.Spinner()
        spinner.set_size_request(16, 16)
        row.append(spinner)
        label = Gtk.Label(label="Thinking…")
        label.add_css_class("dim-label")
        row.append(label)
        self.messages_box.append(row)

    def add_message(self, role: str, text: str) -> None:
        """Show a message that is not (yet) in the stored transcript."""
        self._append_bubble(role, text)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        def do_scroll():
            adj = self.scrolled.get_vadjustment()
            target = adj.get_upper() - adj.get_page_size()
            if target > 0:
                adj.set_value(target)
            return False
        GLib.idle_add(do_scroll)
        GLib.timeout_add(80, do_scroll)

    # --- sending ---

    def _on_send_clicked(self, _widget) -> None:
        start, end = self.text_buffer.get_bounds()
        text = self.text_buffer.get_text(start, end, True).strip()
        if not text or self.is_busy:
            return
        self.text_buffer.set_text("")
        self.send_user_text(text)

    def send_user_text(self, text: str) -> None:
        manager = self.conversation_manager
        if manager is None or self.is_busy:
            return
        if self._shown_greeting and not manager.history():
            manager.add_assistant_message(self._shown_greeting)  # what the user is answering stays, for both
        self._shown_greeting = None
        self._busy_manager = manager
        self._update_input_sensitivity()
        self._update_header()
        self.add_message("user", text)
        self._append_thinking()
        self._scroll_to_bottom()

        def worker():
            error = None
            try:
                with self.store_lock:
                    manager.send_user_message(text)
            except Exception as e:  # keep the UI alive if the model or a tool fails
                logger.exception("Assistant turn failed")
                error = str(e) or e.__class__.__name__
            GLib.idle_add(self._on_turn_finished, manager, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_turn_finished(self, manager: ConversationManager, error: str | None) -> bool:
        persist_conversation(manager)
        self._busy_manager = None
        current = self.conversation_manager
        if current is not manager and current.day_plan.target_date == manager.day_plan.target_date:
            current.refresh()  # the same day was reopened while the turn ran
        self._update_input_sensitivity()
        self.render(force=True)
        if error and current is manager:
            self.add_message("assistant", f"Sorry, something went wrong: {error}")
        self.focus_input()
        if self.on_plan_updated:
            self.on_plan_updated(manager.day_plan)
        return False
