"""Main application window for Thirties."""

import logging
import time
from datetime import date, timedelta
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from thirties_core.astronomy import get_diurnal_date
from thirties_core.config import load_config
from thirties_core.backlog import load_backlog
from thirties_core.conversation import ConversationManager
from thirties_core.inference import get_inference_engine
from thirties_core.joplin_engine import JoplinEngine
from thirties_core.scheduler import DeterministicScheduler
from thirties_core.sync_engine import SyncEngine
from thirties.views.chat_panel import ChatPanel
from thirties.views.day_view import DayView
from thirties.views.profile_dialog import ProfileMemoryDialog

logger = logging.getLogger(__name__)

# How often the live "now" (gold highlight, sun/moon, remaining tallies) advances.
TICK_SECONDS = 30
# How often to pick up changes other clients (the Telegram bot) made to the day and its conversation.
STORE_REFRESH_SECONDS = 60
# Refreshing on focus is skipped if the last refresh was this recent.
FOCUS_REFRESH_MIN_SECONDS = 5


@Gtk.Template(resource_path='/tech/redfoxlabs/Thirties/window.ui')
class ThirtiesWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ThirtiesWindow'

    prev_day_btn = Gtk.Template.Child()
    next_day_btn = Gtk.Template.Child()
    today_btn = Gtk.Template.Child()
    calendar_menu_btn = Gtk.Template.Child()
    calendar_popover = Gtk.Template.Child()
    date_calendar = Gtk.Template.Child()
    window_title = Gtk.Template.Child()
    chat_toggle_btn = Gtk.Template.Child()
    split_view = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.config = load_config()
        self.scheduler = DeterministicScheduler(self.config)
        self.joplin_engine = JoplinEngine(self.config)
        self.active_date = self._get_active_diurnal_date()
        self.current_date = self.active_date

        # Ingest backlog tasks & notes from Joplin
        self.backlog_tasks, self.active_notes = load_backlog(self.config, self.joplin_engine, self.scheduler)
        self.scheduler.attach_history_to_tasks(self.backlog_tasks)

        # Inference backend (Gemini via Antigravity, LiteRT, or Mock)
        self.inference_engine = get_inference_engine(self.config)

        # Joplin cross-device synchronization engine
        self.sync_engine = SyncEngine(
            config=self.config,
            state_db=self.scheduler.state_db,
            joplin_engine=self.joplin_engine,
        )
        if self.config.sync.enabled and self.config.sync.auto_pull_on_startup:
            try:
                ok, msg = self.sync_engine.pull()
                if ok:
                    logger.info("Startup sync pull: %s", msg)
            except Exception as e:
                logger.warning("Startup sync pull failed (continuing normally): %s", e)

        # Schedule on the left, chat on the right (see window.ui)
        self.day_view = DayView()
        self.split_view.set_content(self.day_view)

        self.conv_manager = None
        self.chat_panel = ChatPanel(on_plan_updated=self._on_plan_updated)
        self.split_view.set_sidebar(self.chat_panel)

        # Event connections
        self.prev_day_btn.connect("clicked", self._on_prev_day)
        self.next_day_btn.connect("clicked", self._on_next_day)
        self.today_btn.connect("clicked", self._on_today_clicked)
        self.date_calendar.connect("day-selected", self._on_calendar_day_selected)
        self.split_view.connect("notify::show-sidebar", self._on_chat_visibility_changed)
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        self._install_actions()
        self._install_type_to_chat()

        self._last_store_refresh = time.monotonic()
        self._timers = [
            GLib.timeout_add_seconds(TICK_SECONDS, self._on_tick),
            GLib.timeout_add_seconds(STORE_REFRESH_SECONDS, self._on_store_refresh_timer),
        ]

        self._load_day(self.current_date)
        GLib.idle_add(self.chat_panel.focus_input)

    # --- setup ---

    def _install_actions(self) -> None:
        toggle = Gio.SimpleAction.new("toggle-chat", None)
        toggle.connect("activate", lambda *_: self.chat_toggle_btn.set_active(not self.chat_toggle_btn.get_active()))
        self.add_action(toggle)

        profile = Gio.SimpleAction.new("profile-memory", None)
        profile.connect("activate", lambda *_: self._show_profile_dialog())
        self.add_action(profile)

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.toggle-chat", ["F9"])

    def _install_type_to_chat(self) -> None:
        """Typing anywhere in the window goes to the chat input while the chat is shown."""
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_window_key_pressed)
        self.add_controller(keys)

    def _on_window_key_pressed(self, controller, keyval, keycode, state) -> bool:
        if not self.split_view.get_show_sidebar() or self.get_visible_dialog() is not None:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        char = chr(Gdk.keyval_to_unicode(keyval) or 0)
        if not char.isprintable() or char.isspace() or char == "\0":
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.Text, Gtk.TextView)):
            return False
        text_view = self.chat_panel.text_view
        if not text_view.get_sensitive():
            return False
        text_view.grab_focus()
        return controller.forward(text_view)

    def _on_close_request(self, _window) -> bool:
        for source_id in self._timers:
            GLib.source_remove(source_id)
        self._timers = []
        return False

    # --- loading days ---

    def _new_conversation_manager(self, plan, ambiguous) -> ConversationManager:
        return ConversationManager(
            day_plan=plan,
            tasks=self.backlog_tasks,
            ambiguous_events=ambiguous,
            scheduler=self.scheduler,
            joplin_engine=self.joplin_engine,
            inference_engine=self.inference_engine,
            active_notes=self.active_notes,
        )

    def _load_day(self, target_date: date) -> None:
        """Show a day: its plan on the left and its current conversation on the right."""
        self.current_date = target_date
        self._update_day_chrome()

        # Sync GtkCalendar selected day
        try:
            gdt = GLib.DateTime.new_local(target_date.year, target_date.month, target_date.day, 0, 0, 0)
            self.date_calendar.select_day(gdt)
        except Exception:
            pass

        # The conversation manager owns the day's plan; the schedule shows the same object.
        plan, ambiguous = self.scheduler.build_day_plan(target_date)
        self.conv_manager = self._new_conversation_manager(plan, ambiguous)
        self.day_view.refresh_plan(self.conv_manager.day_plan)
        self.day_view.scroll_to_now()
        self.chat_panel.set_conversation_manager(self.conv_manager)
        if self.split_view.get_show_sidebar():
            GLib.idle_add(self.chat_panel.focus_input)

    def _update_day_chrome(self) -> None:
        self.today_btn.set_sensitive(self.current_date != self.active_date)
        self.day_view.set_today(self.active_date)
        target = self.current_date
        if target == self.active_date:
            subtitle = "Today"
        elif target == self.active_date - timedelta(days=1):
            subtitle = "Yesterday"
        elif target == self.active_date + timedelta(days=1):
            subtitle = "Tomorrow"
        else:
            subtitle = target.strftime("%A, %B %d, %Y")
        self.window_title.set_subtitle(subtitle)

    # --- live updates ---

    def _on_tick(self) -> bool:
        """Every TICK_SECONDS: advance "now" and follow the diurnal rollover at sunrise."""
        active = self._get_active_diurnal_date()
        if active != self.active_date:
            was_viewing_today = self.current_date == self.active_date
            self.active_date = active
            if was_viewing_today and not self.chat_panel.is_busy:
                self._load_day(active)
                return True
            self._update_day_chrome()
        self.day_view.tick()
        return True

    def _on_store_refresh_timer(self) -> bool:
        self._refresh_from_store()
        return True

    def _on_active_changed(self, _window, _param) -> None:
        if self.is_active() and time.monotonic() - self._last_store_refresh >= FOCUS_REFRESH_MIN_SECONDS:
            self._refresh_from_store()

    def _refresh_from_store(self) -> None:
        """Pick up changes other clients (e.g. the Telegram bot) made to this day and its conversation."""
        if self.conv_manager is None or self.chat_panel.is_busy:
            return
        self._last_store_refresh = time.monotonic()
        try:
            self.conv_manager.refresh()
        except Exception as e:
            logger.warning("Refreshing from the database failed: %s", e)
            return
        self.day_view.refresh_plan(self.conv_manager.day_plan)
        self.chat_panel.after_store_refresh()

    def _on_plan_updated(self, plan) -> None:
        """Triggered after an assistant turn, which may have changed the plan."""
        if self.conv_manager is not None and plan.target_date == self.current_date:
            self.day_view.refresh_plan(self.conv_manager.day_plan)
        self._push_sync()

    def _push_sync(self) -> None:
        if self.config.sync.enabled and self.config.sync.auto_push_on_change:
            try:
                self.sync_engine.push()
            except Exception as e:
                logger.debug("Auto-push sync failed: %s", e)

    # --- chat visibility ---

    def _on_chat_visibility_changed(self, split_view, _param) -> None:
        if split_view.get_show_sidebar():
            GLib.idle_add(self.chat_panel.focus_input)
        elif self.chat_panel.get_focus_child() is not None:
            self.set_focus(None)  # don't leave focus inside the hidden chat

    # --- profile & memory ---

    def _show_profile_dialog(self) -> None:
        services = self.conv_manager.tools.services if self.conv_manager else None
        dialog = ProfileMemoryDialog(
            profile=services.profile if services else None,
            memory=services.memory if services else None,
            on_changed=self._push_sync,
        )
        dialog.present(self)

    # --- navigation ---

    def _on_prev_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date - timedelta(days=1))

    def _on_next_day(self, _btn: Gtk.Button) -> None:
        self._load_day(self.current_date + timedelta(days=1))

    def _get_active_diurnal_date(self) -> date:
        gen = self.config.general
        return get_diurnal_date(
            lat=gen.latitude,
            lon=gen.longitude,
            tz_name=gen.timezone,
        )

    def _on_today_clicked(self, _btn: Gtk.Button) -> None:
        self.active_date = self._get_active_diurnal_date()
        self._load_day(self.active_date)

    def _on_calendar_day_selected(self, calendar: Gtk.Calendar) -> None:
        gdt = calendar.get_date()
        selected_date = date(gdt.get_year(), gdt.get_month(), gdt.get_day_of_month())
        if selected_date != self.current_date:
            self._load_day(selected_date)
            self.calendar_popover.popdown()
