"""Main application entry point for Thirties."""

import sys
import gi

from gettext import gettext as _

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gio, Adw, Gdk
from .window import ThirtiesWindow


class ThirtiesApplication(Adw.Application):
    """The main application singleton class."""

    def __init__(self):
        super().__init__(application_id='tech.redfoxlabs.Thirties',
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
                         resource_base_path='/tech/redfoxlabs/Thirties')
        self.create_action('quit', lambda *_: self.quit(), ['<control>q'])
        self.create_action('about', self.on_about_action)
        self.create_action('preferences', self.on_preferences_action)

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

    def do_activate(self):
        """Called when the application is activated."""
        win = self.props.active_window
        if not win:
            win = ThirtiesWindow(application=self)
        win.present()

    def on_about_action(self, *args):
        """Callback for the app.about action."""
        about = Adw.AboutDialog(application_name='Thirties',
                                application_icon='tech.redfoxlabs.Thirties',
                                developer_name='Matthew Samson',
                                version='0.1.0',
                                translator_credits=_('translator-credits'),
                                developers=['Matthew Samson'],
                                copyright='© 2026 Matthew Samson')
        about.present(self.props.active_window)

    def on_preferences_action(self, widget, _):
        """Callback for the app.preferences action."""
        print('app.preferences action activated')

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)


def main(version):
    """The application's entry point."""
    app = ThirtiesApplication()
    return app.run(sys.argv)
