"""Main application entry point for Thirties."""

import logging
import sys
import gi

from gettext import gettext as _

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, GLib, Adw, Gdk

from thirties_core.app_core import AppCore

logger = logging.getLogger(__name__)

NOTIFICATION_IDS = {"checkin": "checkin", "reminder": "reminder", "start": "start"}


class ThirtiesApplication(Adw.Application):
    """The app: windows when you open it, and (with permission) a background service that keeps
    running after the last window closes, for reminders, check-ins, Telegram and sync."""

    def __init__(self):
        super().__init__(application_id='tech.redfoxlabs.Thirties',
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
                         resource_base_path='/tech/redfoxlabs/Thirties')
        self.version = ""  # set by main() from the build
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start in the background, without a window"), None)
        self.core = None
        self._held = False
        self.create_action('quit', lambda *_: self.quit_completely(), ['<control>q'])
        self.create_action('about', self.on_about_action)
        self.create_action('preferences', self.on_preferences_action, ['<control>comma'])
        self.create_action('show-window', lambda *_: self.activate())

    def do_startup(self):
        Adw.Application.do_startup(self)
        try:
            provider = Gtk.CssProvider()
            provider.load_from_resource('/tech/redfoxlabs/Thirties/style.css')
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
        except Exception as e:
            print("CSS load notice:", e)
        self.core = AppCore(use_keyring=True, desktop_notify=self.notify_from_background)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        if options.get("background"):
            if self.core.setup_done:
                self.run_in_background()
        else:
            self.activate()
        return 0

    def do_activate(self):
        """Opening the app: the setup assistant the first time, then the main window."""
        win = self.props.active_window
        if win:
            win.present()
            return
        if not self.core.setup_done:
            from .views.setup import SetupWindow
            SetupWindow(application=self, core=self.core, on_done=self._setup_finished).present()
            return
        self._open_main_window()

    def _open_main_window(self):
        from .window import ThirtiesWindow
        ThirtiesWindow(application=self, core=self.core).present()
        self.run_in_background()

    def _setup_finished(self):
        self.core.reload()
        self._open_main_window()

    # --- the background service ---

    def background_allowed(self) -> bool:
        return bool(self.core.settings.get("background.enabled", True))

    def run_in_background(self):
        """Start the service; with background allowed, keep running after the last window closes."""
        self.core.start_service()
        if self.background_allowed() and not self._held:
            self.hold()
            self._held = True
        elif not self.background_allowed() and self._held:
            self.release()
            self._held = False

    def quit_completely(self):
        self.core.stop_service()
        for win in list(self.get_windows()):
            win.close()
        if self._held:
            self.release()
            self._held = False
        self.quit()

    def notify_from_background(self, title: str, body: str, kind: str):
        """Called from the service's thread: show a desktop notification on the main loop."""
        def show():
            n = Gio.Notification.new(title)
            n.set_body(body)
            n.set_default_action("app.show-window")
            if kind == "reminder":
                n.set_priority(Gio.NotificationPriority.HIGH)
            self.send_notification(NOTIFICATION_IDS.get(kind, kind), n)
            return False
        GLib.idle_add(show)

    def request_background_permission(self, window=None, done=None):
        """Ask (through the Background portal) to keep running and to start at login."""
        from .portal import request_background
        request_background(_("Reminders, check-ins and your phone keep working when Thirties is closed."),
                           autostart=True, callback=done)

    # --- menus ---

    def on_about_action(self, *args):
        """Callback for the app.about action."""
        about = Adw.AboutDialog(application_name='Thirties',
                                application_icon='tech.redfoxlabs.Thirties',
                                developer_name='Matthew Samson',
                                version=self.version,
                                translator_credits=_('translator-credits'),
                                developers=['Matthew Samson'],
                                copyright='© 2026 Matthew Samson')
        about.present(self.props.active_window)

    def on_preferences_action(self, *args):
        from .views.preferences import PreferencesDialog
        PreferencesDialog(self.core, app=self).present(self.props.active_window)

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)


def main(version):
    """The application's entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = ThirtiesApplication()
    app.version = version
    return app.run(sys.argv)
