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


def _lists_page(core):
    """Your Lists: the model reads the notes and guesses what each is for, then you check its guesses.
    (How pressing each item is gets guessed afterwards, in the background.)"""
    import threading
    from gi.repository import GLib
    from thirties_core.lists import ListCatalog, sort_lists
    from .lists_page import ListsPage

    catalog = ListCatalog(core.scheduler.state_db)
    lists = ListsPage(catalog, lambda: (core.joplin_engine.fetch_active_notes(), core.joplin_engine.fetch_tasks()))
    status = Adw.Banner(title="Reading your lists…", revealed=True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(status)
    lists.set_vexpand(True)
    box.append(lists)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(box)
    bottom = Gtk.Box(halign=Gtk.Align.END, margin_top=6, margin_bottom=12, margin_end=12)
    button = Gtk.Button(label="Next")
    button.add_css_class("suggested-action")
    button.add_css_class("pill")
    bottom.append(button)
    toolbar.add_bottom_bar(bottom)
    nav = Adw.NavigationPage(title="Your Lists", child=toolbar)
    nav.button, nav.lists, nav.status = button, lists, status
    started = []

    def done(message):
        status.set_title(message)
        lists.reload()
        return False

    def sort():
        try:
            from thirties_core.astronomy import get_diurnal_date
            from thirties_core.brains.list_sorter import list_sorter_for
            engine, gen = core.joplin_engine, core.config.general
            notes = engine.fetch_active_notes()
            if not notes:
                message = "No notes found yet. Connect Joplin on the previous page, or skip this for now."
            else:
                sorter = list_sorter_for(core.config, core.scheduler.state_db, core.inference_engine)
                if sorter is None:
                    message = ("Thirties will sort your lists once the assistant is ready. "
                               "Check them any time in Profile → Lists.")
                else:
                    today = get_diurnal_date(lat=gen.latitude, lon=gen.longitude, tz_name=gen.timezone)
                    sort_lists(catalog, notes, engine.fetch_tasks(), sorter, today, with_items=False)
                    message = ("Here's what Thirties thinks each note is for. Change anything that's off; "
                               "how pressing each item is gets sorted out next, in Profile → Lists.")
        except Exception as e:
            message = f"Couldn't read your lists: {e}"
        GLib.idle_add(done, message)

    def on_shown(*_):
        if not started:
            started.append(True)
            threading.Thread(target=sort, daemon=True, name="SetupSortLists").start()
    nav.connect("shown", on_shown)
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
            _lists_page(core),
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
