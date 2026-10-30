"""Preference groups shared by the first-run setup and the Preferences dialog.

Every change is saved at once (GNOME's instant-apply style) and, after a short
pause, applied to the running app through ``AppCore.reload``.
"""

import random
import threading
from datetime import datetime, time as dtime

from gi.repository import Adw, GLib, Gtk

from thirties_core.clock import format_clock
from thirties_core.config import load_config

DAY_NAMES = [("mon", "M"), ("tue", "T"), ("wed", "W"), ("thu", "T"), ("fri", "F"), ("sat", "S"), ("sun", "S")]
TIMES = [format_clock(datetime.combine(datetime.today(), dtime(i // 2, 30 * (i % 2)))) for i in range(48)]


class SettingsContext:
    def __init__(self, core, toaster=None, app=None, parent=None):
        self.core = core
        self.s = core.settings
        self.toaster = toaster
        self.app = app
        self.parent = parent
        self._pending = 0

    def get(self, key, default=None):
        return self.s.get(key, default)

    def set(self, key, value):
        self.s.set(key, value)
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(700, self._apply)

    def _apply(self):
        self._pending = 0
        self.core.reload()
        return False

    def flush(self):
        if self._pending:
            GLib.source_remove(self._pending)
            self._apply()

    def config(self):
        """A fresh config with the current settings, for checks."""
        return self.s.apply(load_config())

    def toast(self, text):
        if self.toaster is not None:
            self.toaster.add_toast(Adw.Toast(title=text, timeout=3))

    @staticmethod
    def run_async(fn, done):
        def work():
            try:
                result, error = fn(), None
            except Exception as e:  # shown to the user by ``done``
                result, error = None, e
            GLib.idle_add(lambda: done(result, error) and False)
        threading.Thread(target=work, daemon=True).start()


def save_as_you_type(row, save, delay=600):
    """Entry rows save after a pause in typing and when they lose focus; no ✓ to press."""
    pending = {"id": 0}

    def fire():
        pending["id"] = 0
        save(row.get_text().strip())
        return False

    def changed(*_):
        if pending["id"]:
            GLib.source_remove(pending["id"])
        pending["id"] = GLib.timeout_add(delay, fire)

    def left(*_):
        if pending["id"]:
            GLib.source_remove(pending["id"])
            fire()
    row.connect("changed", changed)
    focus = Gtk.EventControllerFocus()
    focus.connect("leave", left)
    row.add_controller(focus)


def _time_row(title, index):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(TIMES))
    row.set_selected(index % 48)
    return row


def _label(text, css=("dim-label",)):
    lbl = Gtk.Label(label=text, wrap=True, xalign=0)
    for c in css:
        lbl.add_css_class(c)
    return lbl


# --- where and when ---

def location_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="Location",
                                 description="Sunrise and sunset shape your day, so Thirties needs to know roughly where you are.")
    cfg = ctx.config()
    current = Adw.ActionRow(title="Current location")
    current.set_subtitle(cfg.general.place_name or f"{cfg.general.latitude:.3f}, {cfg.general.longitude:.3f}"
                         if ctx.s.is_set("general.latitude") else "Not set yet")
    detect = Gtk.Button(label="Detect", valign=Gtk.Align.CENTER)
    current.add_suffix(detect)
    group.add(current)
    search = Adw.EntryRow(title="Search for a city", show_apply_button=True)
    group.add(search)
    results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    results.add_css_class("boxed-list")
    results.set_visible(False)
    results.set_margin_top(6)
    group.add(results)

    def use(name, lat, lon):
        from thirties_core.location import system_timezone
        ctx.set("general.latitude", lat)
        ctx.set("general.longitude", lon)
        ctx.set("general.place_name", name)
        ctx.set("general.timezone", system_timezone())
        current.set_subtitle(name)
        results.set_visible(False)

    def on_search(_row):
        query = search.get_text().strip()
        if not query:
            return
        from thirties_core.location import search_places

        def done(places, error):
            while (child := results.get_first_child()) is not None:
                results.remove(child)
            if error or not places:
                ctx.toast("No places found" if not error else "Couldn't search (are you online?)")
                return
            for p in places:
                row = Adw.ActionRow(title=p.name, subtitle=f"{p.latitude:.3f}, {p.longitude:.3f}", activatable=True)
                row.connect("activated", lambda _r, p=p: use(p.name, p.latitude, p.longitude))
                results.append(row)
            results.set_visible(True)
        ctx.run_async(lambda: search_places(query), done)

    def on_detect(_btn):
        detect.set_sensitive(False)
        try:
            import gi
            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue
        except (ImportError, ValueError):
            ctx.toast("Location services aren't available; search for your city instead.")
            detect.set_sensitive(True)
            return

        def got(_src, result):
            detect.set_sensitive(True)
            try:
                simple = Geoclue.Simple.new_finish(result)
                loc = simple.get_location()
                lat, lon = loc.get_property("latitude"), loc.get_property("longitude")
            except GLib.Error:
                ctx.toast("Couldn't detect your location; search for your city instead.")
                return
            from thirties_core.location import place_name
            ctx.run_async(lambda: place_name(lat, lon), lambda name, _e: use(name or f"{lat:.3f}, {lon:.3f}", lat, lon))
        Geoclue.Simple.new("tech.redfoxlabs.Thirties", Geoclue.AccuracyLevel.CITY, None, got)

    search.connect("apply", on_search)
    detect.connect("clicked", on_detect)
    units = ["", "fahrenheit", "celsius"]
    unit = Adw.ComboRow(title="Temperatures", subtitle="For the weather the assistant plans around",
                        model=Gtk.StringList.new(["Automatic", "Fahrenheit", "Celsius"]))
    unit.set_selected(units.index(cfg.general.temperature_unit) if cfg.general.temperature_unit in units else 0)
    unit.connect("notify::selected", lambda r, _p: ctx.set("general.temperature_unit", units[r.get_selected()]))
    group.add(unit)
    return group


def schedule_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="Your usual week",
                                 description="Work and sleep are placed on these days automatically; you can always change a single day.")
    cfg = ctx.config().general
    days_row = Adw.ActionRow(title="Work days")
    box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
    buttons = {}
    for key, letter in DAY_NAMES:
        b = Gtk.ToggleButton(label=letter, active=key in cfg.work_days, tooltip_text=key.capitalize())
        b.add_css_class("circular")
        b.add_css_class("day-toggle")
        buttons[key] = b
        box.append(b)

    def on_days(*_):
        ctx.set("general.work_days", [k for k, _ in DAY_NAMES if buttons[k].get_active()])
    for b in buttons.values():
        b.connect("toggled", on_days)
    days_row.add_suffix(box)
    group.add(days_row)
    start = _time_row("Work starts", cfg.work_start_thirty)
    end = _time_row("Work ends", cfg.work_end_thirty + 1)
    bed = _time_row("Bedtime", cfg.sleep_start_thirty)
    wake = _time_row("Wake up", cfg.sleep_end_thirty)
    start.connect("notify::selected", lambda r, _p: ctx.set("general.work_start_thirty", r.get_selected()))
    end.connect("notify::selected", lambda r, _p: ctx.set("general.work_end_thirty", (r.get_selected() - 1) % 48))
    bed.connect("notify::selected", lambda r, _p: ctx.set("general.sleep_start_thirty", r.get_selected()))
    wake.connect("notify::selected", lambda r, _p: ctx.set("general.sleep_end_thirty", r.get_selected()))
    for row in (start, end, bed, wake):
        group.add(row)
    return group


# --- the assistant ---

def brain_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    from thirties_core.brains import brain_unavailable_reason
    from thirties_core.brains.slm.runtime import find_model
    group = Adw.PreferencesGroup(title="Assistant",
                                 description="Gemma runs privately on this computer. Nothing you say leaves it.")
    chosen = ctx.get("assistant.brain") or "slm"  # Gemma unless the user chose otherwise
    gemma = Adw.ActionRow(title="Gemma (on this computer)")
    gemma_check = Gtk.CheckButton(active=chosen == "slm", valign=Gtk.Align.CENTER)
    gemma.add_prefix(gemma_check)
    gemma.set_activatable_widget(gemma_check)
    progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, visible=False, show_text=True)
    progress.set_size_request(160, -1)
    download = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
    download.add_css_class("suggested-action")
    gemma.add_suffix(progress)
    gemma.add_suffix(download)
    group.add(gemma)

    gemini = Adw.ActionRow(title="Gemini (cloud, for developers)")
    gemini_check = Gtk.CheckButton(group=gemma_check, active=chosen == "frontier", valign=Gtk.Align.CENTER)
    gemini.add_prefix(gemini_check)
    gemini.set_activatable_widget(gemini_check)
    group.add(gemini)

    def refresh():
        have = find_model(ctx.config().assistant.slm_model_path) is not None
        gemma.set_subtitle("Ready" if have else "Needs a one-time download (about 3 GB)")
        download.set_visible(not have and not progress.get_visible())
        why = brain_unavailable_reason("frontier", ctx.config())
        gemini.set_subtitle(why[0].upper() + why[1:] if why else "Ready (see Developer options below)")
        gemini.set_sensitive(why is None or gemini_check.get_active())

    def pick(check, name):
        if check.get_active():
            ctx.set("assistant.brain", name)

    gemma_check.connect("toggled", pick, "slm")
    gemini_check.connect("toggled", pick, "frontier")
    cancel = threading.Event()

    def on_download(_btn):
        from thirties_core.brains.slm.model_download import download_model
        download.set_visible(False)
        progress.set_visible(True)

        def report(done, total):
            GLib.idle_add(lambda: progress.set_fraction(done / total) or
                          progress.set_text(f"{done / 1e9:.1f} of {total / 1e9:.1f} GB") or False)

        def finished(_path, error):
            progress.set_visible(False)
            if error:
                ctx.toast(f"The download stopped: {error}. Press Download to resume.")
            else:
                ctx.toast("Gemma is ready")
                if gemma_check.get_active():
                    ctx.set("assistant.brain", "slm")
            refresh()
        ctx.run_async(lambda: download_model(progress=report, cancel=cancel), finished)

    download.connect("clicked", on_download)
    group.connect("destroy", lambda *_: cancel.set())
    group.refresh = refresh
    refresh()
    return group


def gemini_group(ctx: SettingsContext, on_change=None) -> Adw.PreferencesGroup:
    from thirties_core.inference import gemini_sources_status
    group = Adw.PreferencesGroup(title="Developer options",
                                 description="Gemini runs in Google's cloud. It needs the Antigravity CLI (installed as "
                                             "`agy` and signed in) and/or a Gemini API key.")
    cfg = ctx.config()
    status = gemini_sources_status(cfg)
    agy = Adw.SwitchRow(title="Use the subscription (Antigravity CLI)",
                        subtitle="Found" if status["agy"] else "Not found: install the Antigravity CLI as `agy`",
                        active="agy" in cfg.inference.sources)
    key = Adw.PasswordEntryRow(title="Gemini API key")
    key.set_text(ctx.get("inference.gemini_api_key", "") or "")
    api = Adw.SwitchRow(title="Use the API key", active="api" in cfg.inference.sources)
    first = Adw.ComboRow(title="Try first", model=Gtk.StringList.new(["Subscription", "API key"]))
    order = [x for x in cfg.inference.sources if x in ("agy", "api")]
    first.set_selected(1 if order[:1] == ["api"] else 0)

    def save(*_):
        chosen = [x for x, sw in (("agy", agy), ("api", api)) if sw.get_active()]
        if len(chosen) == 2 and first.get_selected() == 1:
            chosen = ["api", "agy"]
        ctx.set("inference.sources", chosen)
        first.set_sensitive(len(chosen) == 2)
        if on_change:
            GLib.timeout_add(800, lambda: on_change() and False)

    def save_key(value):
        if value == (ctx.get("inference.gemini_api_key", "") or ""):
            return
        ctx.set("inference.gemini_api_key", value)
        save()

    for w in (agy, api):
        w.connect("notify::active", save)
    first.connect("notify::selected", save)
    save_as_you_type(key, save_key)
    for row in (agy, key, api, first):
        group.add(row)
    first.set_sensitive(agy.get_active() and api.get_active())
    return group


# --- tasks ---

def joplin_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    from thirties_core import joplin_writeback as jw
    group = Adw.PreferencesGroup(title="Joplin",
                                 description="Thirties reads your to-do lists from Joplin. To let it also check things "
                                             "off: in Joplin, open Tools → Options → Web Clipper and enable the Web "
                                             "Clipper Service, then press Connect and click Grant in Joplin.")
    cfg = ctx.config()
    found = cfg.resolve_joplin_db_path() is not None
    conn = Adw.ActionRow(title="Connection")
    connect = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
    conn.add_suffix(connect)
    group.add(conn)

    def show_status():
        token = ctx.get("joplin.api_token")
        if token:
            conn.set_subtitle("Connected")
            connect.set_label("Reconnect")
        else:
            conn.set_subtitle("Reading your lists" if found else "Joplin wasn't found on this computer")
            connect.set_label("Connect")
    show_status()

    def on_connect(_btn):
        connect.set_sensitive(False)
        conn.set_subtitle("Waiting for you to click Grant in Joplin…")

        def ask():
            if not jw.clipper_running():
                raise RuntimeError("Joplin's Web Clipper Service isn't running. Enable it in Joplin first.")
            auth = jw.request_access()
            for _ in range(90):
                status, token = jw.check_access(auth)
                if status == "accepted":
                    return token
                if status == "rejected":
                    raise RuntimeError("Access was declined in Joplin.")
                threading.Event().wait(2)
            raise RuntimeError("Joplin didn't answer in time.")

        def done(token, error):
            connect.set_sensitive(True)
            if error:
                ctx.toast(str(error))
            else:
                ctx.set("joplin.api_token", token)
                ctx.toast("Connected to Joplin")
            show_status()
        ctx.run_async(ask, done)

    connect.connect("clicked", on_connect)
    write = Adw.SwitchRow(title="Let Thirties update Joplin",
                          subtitle="Check off finished tasks, add tasks, and add steps when you break one down. "
                                   "It always asks before adding or breaking down.",
                          active=bool(cfg.joplin.write_back))
    write.connect("notify::active", lambda r, _p: ctx.set("joplin.write_back", r.get_active()))
    group.add(write)
    tray = jw.tray_available()
    launch = Adw.SwitchRow(title="Start Joplin when needed",
                           subtitle="If Joplin is closed when Thirties has a change for it. Turn on Joplin's "
                                    "“Start application minimised in the tray icon” option to keep it out of the way."
                           if tray else "If Joplin is closed when Thirties has a change for it. Your desktop shows no "
                                        "tray icons, so Joplin will open as a normal window.",
                           active=bool(cfg.joplin.launch_when_needed))
    launch.connect("notify::active", lambda r, _p: ctx.set("joplin.launch_when_needed", r.get_active()))
    group.add(launch)
    routines = Adw.SwitchRow(title="Untick routines each morning",
                             subtitle="Your routine notes start each day with every item open again.",
                             active=bool(getattr(cfg.joplin, "reset_routines", True)))
    routines.connect("notify::active", lambda r, _p: ctx.set("joplin.reset_routines", r.get_active()))
    group.add(routines)
    return group


def notebooks_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="What to read")
    cfg = ctx.config()
    names = ctx.core.joplin_engine.list_notebooks()
    expander = Adw.ExpanderRow(title="Notebooks", subtitle="All notebooks" if not cfg.joplin.allowed_notebooks else
                               ", ".join(cfg.joplin.allowed_notebooks))
    switches = {}

    def save(*_):
        on = [n for n, sw in switches.items() if sw.get_active()]
        value = [] if len(on) == len(switches) else on
        ctx.set("joplin.allowed_notebooks", value)
        expander.set_subtitle("All notebooks" if not value else ", ".join(value))
    for name in names:
        sw = Adw.SwitchRow(title=name, active=not cfg.joplin.allowed_notebooks or name in cfg.joplin.allowed_notebooks)
        sw.connect("notify::active", save)
        switches[name] = sw
        expander.add_row(sw)
    if names:
        group.add(expander)
    someday = Adw.EntryRow(title="“Someday” headings (shown, never suggested)")
    someday.set_text(", ".join(cfg.joplin.someday_sections))
    save_as_you_type(someday, lambda text: ctx.set("joplin.someday_sections",
                                                   [x.strip() for x in text.split(",") if x.strip()]))
    group.add(someday)
    return group


def wishlists_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="Reading and watch lists",
                                 description="Optional. Thirties can suggest from your public want-to-read and "
                                             "watchlists. Your profile must be public; no passwords needed.")
    media = ctx.config().media
    for key, title, value in (("media.goodreads_user_id", "Goodreads user ID (the number in your profile link)",
                               media.goodreads_user_id),
                              ("media.letterboxd_username", "Letterboxd username", media.letterboxd_username),
                              ("media.serializd_username", "Serializd username", media.serializd_username)):
        row = Adw.EntryRow(title=title)
        row.set_text(value or "")
        save_as_you_type(row, lambda text, key=key: ctx.set(key, text) if text != (ctx.get(key, "") or "") else None)
        group.add(row)
    return group


# --- sync ---

def sync_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="Sync between computers",
                                 description="Use a folder that your cloud storage already syncs (Google Drive, "
                                             "Dropbox, Nextcloud…), or a note in Joplin if you have nothing else.")
    cfg = ctx.config().sync
    modes = ["off", "folder", "joplin"]
    mode = Adw.ComboRow(title="Sync", model=Gtk.StringList.new(["Off", "Through a folder", "Through a Joplin note"]))
    mode.set_selected(modes.index(cfg.mode) if cfg.mode in modes else 0)
    folder = Adw.ActionRow(title="Folder", subtitle=cfg.folder or "None chosen")
    choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
    folder.add_suffix(choose)

    def on_mode(row, _p):
        ctx.set("sync.mode", modes[row.get_selected()])
        folder.set_visible(modes[row.get_selected()] == "folder")

    def on_choose(_btn):
        dialog = Gtk.FileDialog(title="Choose the sync folder")

        def chosen(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                ctx.set("sync.folder", f.get_path())
                folder.set_subtitle(f.get_path())
        dialog.select_folder(ctx.parent, None, chosen)

    mode.connect("notify::selected", on_mode)
    choose.connect("clicked", on_choose)
    group.add(mode)
    group.add(folder)
    folder.set_visible(cfg.mode == "folder")
    return group


# --- phone and messages ---

PHONE_STEPS = ("On your phone: install Telegram and sign in.\n"
               "In Telegram, search for @BotFather, send /newbot, and answer its two questions (any name, then a "
               "username ending in “bot”).\n"
               "BotFather replies with a token like 123456:ABC-DEF…; copy it into the box below.\n"
               "Press Pair, then send the code it shows to your new bot.")


def phone_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    from thirties_core.telegram_bridge import TelegramClient, pair_by_code
    steps = "\n".join(f"{i}. {line}" for i, line in enumerate(PHONE_STEPS.split("\n"), 1))
    group = Adw.PreferencesGroup(title="Your phone (Telegram)",
                                 description="Optional. Plan from your phone and get check-ins and reminders there.\n\n"
                                             + steps)
    token = Adw.PasswordEntryRow(title="Bot token")
    token.set_text(ctx.get("telegram.bot_token", "") or "")
    pair = Adw.ActionRow(title="Pair your phone")
    pair_btn = Gtk.Button(label="Pair", valign=Gtk.Align.CENTER)
    pair.add_suffix(pair_btn)
    here = Adw.SwitchRow(title="Run the phone connection on this computer",
                         subtitle="Only one computer should do this.",
                         active=bool(ctx.get("telegram.enabled", False)))
    bot = {"name": ""}

    def show():
        chats = ctx.get("telegram.allowed_chat_ids") or []
        pair.set_subtitle(f"Paired{' with @' + bot['name'] if bot['name'] else ''}" if chats else "Not paired yet")
        pair_btn.set_sensitive(bool(token.get_text().strip()))

    def on_token(value):
        if not value or value == (ctx.get("telegram.bot_token", "") or ""):
            return

        def done(me, error):
            if error:
                ctx.toast("Telegram didn't accept that token")
                return
            ctx.set("telegram.bot_token", value)
            bot["name"] = me.get("username", "")
            ctx.toast(f"Token works: @{bot['name']}")
            show()
        ctx.run_async(lambda: TelegramClient(value).get_me(), done)

    def on_pair(_btn):
        value = token.get_text().strip()
        code = f"{random.randint(1000, 9999)}"
        service = ctx.core.service
        bridge_was_on = service is not None and service.bridge is not None
        if bridge_was_on:
            ctx.core.stop_service()  # one poller at a time
        pair_btn.set_sensitive(False)
        pair.set_subtitle(f"Now send {code} to your bot from your phone…")

        def done(chat, error):
            pair_btn.set_sensitive(True)
            if chat:
                ctx.set("telegram.allowed_chat_ids", [chat["id"]])
                ctx.set("telegram.enabled", True)
                here.set_active(True)
                ctx.toast(f"Paired with {chat['name']}")
            else:
                ctx.toast("Pairing timed out" if not error else f"Pairing failed: {error}")
            if bridge_was_on:
                ctx.core.start_service()
            show()
        ctx.run_async(lambda: pair_by_code(TelegramClient(value), code, timeout=300), done)

    save_as_you_type(token, on_token, delay=900)
    pair_btn.connect("clicked", on_pair)
    here.connect("notify::active", lambda r, _p: ctx.set("telegram.enabled", r.get_active()))
    token.connect("changed", lambda *_: show())
    for row in (token, pair, here):
        group.add(row)
    show()
    return group


def messages_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title="Messages",
                                 description="Sent to your desktop and, if paired, your phone, never while you're "
                                             "asleep or in a calendar event. Desktop notifications can be switched off "
                                             "in GNOME Settings → Apps → Thirties.")
    cfg = ctx.config()
    levels = [("light", "Light: before calendar events", "A heads-up shortly before each calendar event"),
              ("medium", "Medium: a few times a day", "What's planned at the start of each stretch of the day, and "
                                                      "one check-in at its end (plus heads-ups before calendar events)"),
              ("heavy", "High: for every item", "When each thing starts, and a check-in after each one"),
              ("off", "Off: never", "Only the pings you ask for")]
    level = Adw.ComboRow(title="How often Thirties messages you",
                         model=Gtk.StringList.new([name for _, name, _ in levels]))
    current = getattr(cfg.assistant, "message_level", "medium")
    level.set_selected(next((i for i, (k, _, _) in enumerate(levels) if k == current), 1))
    level.set_subtitle(levels[level.get_selected()][2])

    def on_level(row, _p):
        key, _, note = levels[row.get_selected()]
        row.set_subtitle(note)
        ctx.set("assistant.message_level", key)
    level.connect("notify::selected", on_level)
    delay = Adw.SpinRow.new_with_range(0, 120, 5)
    delay.set_title("Check in this many minutes after")
    delay.set_value(cfg.assistant.checkin_after_minutes)
    background = Adw.SwitchRow(title="Keep running in the background",
                               subtitle="So reminders, check-ins and your phone keep working when Thirties is closed. "
                                        "Quit (Ctrl+Q) stops it until you open Thirties again.",
                               active=bool(ctx.get("background.enabled", True)))

    def on_background(row, _p):
        ctx.set("background.enabled", row.get_active())
        if row.get_active() and ctx.app is not None:
            ctx.app.request_background_permission()
        if ctx.app is not None:
            ctx.app.run_in_background()

    delay.connect("notify::value", lambda r, _p: ctx.set("assistant.checkin_after_minutes", int(r.get_value())))
    background.connect("notify::active", on_background)
    for row in (level, delay, background):
        group.add(row)
    return group


# --- calendars ---

CALENDAR_CHOICES = [("mine", "Mine", "Your commitments: planned around"),
                    ("", "Ask about its events", "Thirties asks once per event (once per series if it repeats)"),
                    ("show", "Show only", "On the Day page as someone else's; takes no time, never asked about"),
                    ("ignore", "Ignore", "Never read")]


def calendars_group(ctx: SettingsContext) -> Adw.PreferencesGroup:
    """Per calendar: mine, show only or ignore (O1: a partner's shared calendar kept coming up). Keyed by the
    calendar's name, which is the same on every computer (EDS uids aren't)."""
    group = Adw.PreferencesGroup(title="Calendars",
                                 description="What each calendar from GNOME Online Accounts is to you.")
    engine = getattr(getattr(ctx.core, "scheduler", None), "calendar_engine", None)
    placeholder = Adw.ActionRow(title="Looking for calendars…")
    group.add(placeholder)

    def save(name, key):
        modes = dict(ctx.config().calendar.modes or {})
        if key:
            modes[name.strip().lower()] = key
        else:
            modes.pop(name.strip().lower(), None)
        ctx.set("calendar.modes", modes)

    def fill(calendars, error):
        group.remove(placeholder)
        if error or not calendars:
            group.add(Adw.ActionRow(title="No calendars found",
                                    subtitle="Add one in GNOME Settings → Online Accounts."))
            return
        for cal in calendars:
            choices = CALENDAR_CHOICES if not cal["mine"] else [c for c in CALENDAR_CHOICES if c[0] != ""]
            row = Adw.ComboRow(title=cal["name"], model=Gtk.StringList.new([label for _, label, _ in choices]))
            current = cal["mode"] or ("mine" if cal["mine"] else "")
            index = next((i for i, (k, _, _) in enumerate(choices) if k == current), 0)
            row.set_selected(index)
            row.set_subtitle(choices[index][2])

            def on_pick(r, _p, name=cal["name"], choices=choices):
                key, _, note = choices[r.get_selected()]
                r.set_subtitle(note)
                save(name, key)
            row.connect("notify::selected", on_pick)
            group.add(row)
    lister = getattr(engine, "list_calendars", None)
    ctx.run_async(lister if lister else (lambda: []), fill)
    return group
