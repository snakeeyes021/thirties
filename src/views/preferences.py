"""Preferences: everything chosen at setup, changeable later (saved and applied at once)."""

from gi.repository import Adw

from . import settings_rows as rows


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, core, app=None):
        super().__init__(title="Preferences", search_enabled=True)
        ctx = rows.SettingsContext(core, toaster=self, app=app, parent=app.props.active_window if app else None)
        self.ctx = ctx
        pages = [
            ("General", "preferences-system-time-symbolic", [rows.location_group, rows.schedule_group, rows.week_group,
                                                             rows.timeline_group, rows.calendars_group]),
            ("Assistant", "user-available-symbolic", [rows.brain_group]),
            ("Tasks", "checkbox-checked-symbolic", [rows.joplin_group, rows.joplin_peer_group, rows.notebooks_group,
                                                    rows.wishlists_group]),
            ("Sync", "emblem-synchronizing-symbolic", [rows.sync_group]),
            ("Phone & Messages", "phone-symbolic", [rows.phone_group, rows.messages_group]),
        ]
        for title, icon, groups in pages:
            page = Adw.PreferencesPage(title=title, icon_name=icon)
            for build in groups:
                page.add(build(ctx))
            if title == "Assistant":
                page.add(rows.gemini_group(ctx))
            self.add(page)
        self.connect("closed", lambda *_: ctx.flush())
