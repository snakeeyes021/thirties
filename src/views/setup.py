"""First-run setup: a few pages, then the main window. Everything here is also in Preferences."""

from gi.repository import Adw, Gtk

from . import settings_rows as rows


def _page(title, description, groups, ctx, next_label="Next", on_next=None):
    page = Adw.PreferencesPage()
    if description:
        intro = Adw.PreferencesGroup(description=description)
        page.add(intro)
    for build in groups:
        page.add(build(ctx))
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(page)
    bottom = Gtk.Box(halign=Gtk.Align.END, margin_top=6, margin_bottom=12, margin_end=12)
    button = Gtk.Button(label=next_label)
    button.add_css_class("suggested-action")
    button.add_css_class("pill")
    bottom.append(button)
    toolbar.add_bottom_bar(bottom)
    nav = Adw.NavigationPage(title=title, child=toolbar)
    nav.button = button
    return nav


class SetupWindow(Adw.ApplicationWindow):
    def __init__(self, application, core, on_done):
        super().__init__(application=application, title="Welcome to Thirties", default_width=640, default_height=760)
        self.core, self.on_done, self.app = core, on_done, application
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        ctx = rows.SettingsContext(core, toaster=self.toasts, app=application, parent=self)
        self.ctx = ctx

        welcome = Adw.StatusPage(icon_name="tech.redfoxlabs.Thirties", title="Welcome to Thirties",
                                 description="Thirties plans your day in thirty-minute “thirties”, shaped around "
                                             "daylight and dark, and takes care of the organizing for you: what to do "
                                             "next, what slipped, and where it should go.\n\nA few questions first. "
                                             "You can change every answer later in Preferences.")
        start = Gtk.Button(label="Get Started", halign=Gtk.Align.CENTER)
        start.add_css_class("suggested-action")
        start.add_css_class("pill")
        welcome.set_child(start)
        welcome_view = Adw.ToolbarView()
        welcome_view.add_top_bar(Adw.HeaderBar(show_title=False))
        welcome_view.set_content(welcome)
        welcome_page = Adw.NavigationPage(title="Welcome", child=welcome_view)

        pages = [
            _page("Your Day", "", [rows.location_group, rows.schedule_group], ctx),
            _page("Assistant", "", [rows.brain_group, rows.gemini_group], ctx),
            _page("Tasks", "", [rows.joplin_group, rows.wishlists_group], ctx),
            _page("Sync", "", [rows.sync_group], ctx),
            _page("Phone & Messages", "", [rows.phone_group, rows.messages_group], ctx, next_label="Finish"),
        ]
        self.nav.add(welcome_page)
        start.connect("clicked", lambda *_: self._go(pages[0]))
        for page, following in zip(pages, pages[1:]):
            page.button.connect("clicked", lambda *_, f=following: self._go(f))
        pages[-1].button.connect("clicked", self._finish)
        self._pages = pages

    def _go(self, page):
        if page is self._pages[1] and not self.core.settings.is_set("general.latitude"):
            self.toasts.add_toast(Adw.Toast(title="Set your location first (search for your city)", timeout=3))
            self.nav.pop_to_page(self._pages[0])
            return
        self.nav.push(page)

    def _finish(self, _btn):
        settings = self.core.settings
        if not settings.is_set("general.latitude"):
            self.toasts.add_toast(Adw.Toast(title="Set your location first (search for your city)", timeout=3))
            self.nav.pop_to_page(self._pages[0])
            return
        if not settings.is_set("assistant.brain"):
            settings.set("assistant.brain", "slm")
        if settings.get("background.enabled", True):
            self.app.request_background_permission()
        settings.set("setup.done", True)
        self.ctx.flush()
        self.close()
        self.on_done()
