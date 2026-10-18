"""The XDG Background portal over D-Bus (libportal isn't in the GNOME runtime).

RequestBackground asks the user (once) to let the app run in the background and,
with autostart, adds it to their login items. The user can change both later in
GNOME Settings → Apps → Thirties.
"""

import logging
import random

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"


def request_background(reason: str, autostart: bool = True, callback=None, parent_window: str = "") -> None:
    """callback(granted: bool, autostart: bool) runs on the main loop when the user answers."""
    def finish(granted: bool, auto: bool) -> None:
        if callback is not None:
            callback(granted, auto)

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as e:
        logger.warning("No session bus for the Background portal: %s", e)
        finish(False, False)
        return
    token = f"thirties{random.randint(0, 1 << 30)}"
    sender = bus.get_unique_name().lstrip(":").replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def on_response(_conn, _sender, _path, _iface, _signal, params, sub_id_box):
        bus.signal_unsubscribe(sub_id_box[0])
        response, results = params.unpack()
        finish(response == 0 and bool(results.get("background", False)), bool(results.get("autostart", False)))

    sub_id_box = [0]
    sub_id_box[0] = bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", handle, None,
                                         Gio.DBusSignalFlags.NO_MATCH_RULE, on_response, sub_id_box)
    options = {
        "handle_token": GLib.Variant("s", token),
        "reason": GLib.Variant("s", reason),
        "autostart": GLib.Variant("b", autostart),
        "commandline": GLib.Variant("as", ["thirties", "--background"]),
        "dbus-activatable": GLib.Variant("b", False),
    }

    def on_called(source, result):
        try:
            source.call_finish(result)
        except GLib.Error as e:
            logger.warning("The Background portal isn't available: %s", e)
            bus.signal_unsubscribe(sub_id_box[0])
            finish(False, False)

    bus.call(PORTAL, PATH, "org.freedesktop.portal.Background", "RequestBackground",
             GLib.Variant("(sa{sv})", (parent_window, options)), None, Gio.DBusCallFlags.NONE, -1, None, on_called)
