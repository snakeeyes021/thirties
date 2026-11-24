"""A stand-in desktop notification server for tests: owns org.freedesktop.Notifications on the session bus it is
started on and writes each Notify call it gets, as one JSON line, to the file named on the command line.

    python3 tests/fake_notifications.py <record.jsonl>

Only for a private test session (tests/headless.py): it refuses to run on a bus under /run/user/, which is where a
real desktop's session bus lives.
"""

from __future__ import annotations

import json
import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

NAME = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
XML = """
<node>
  <interface name="org.freedesktop.Notifications">
    <method name="Notify">
      <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
      <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
    </method>
    <method name="CloseNotification"><arg type="u" direction="in"/></method>
    <method name="GetCapabilities"><arg type="as" direction="out"/></method>
    <method name="GetServerInformation">
      <arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/>
      <arg type="s" direction="out"/>
    </method>
    <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
    <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
  </interface>
</node>
"""


def main(record: str) -> int:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address or "/run/user/" in address:
        print("not a private test bus", file=sys.stderr)
        return 2
    loop = GLib.MainLoop()
    counter = [0]

    def call(conn, sender, path, iface, method, params, invocation):
        if method == "Notify":
            app, _replaces, _icon, summary, body, _actions, _hints, _timeout = params.unpack()
            counter[0] += 1
            with open(record, "a", encoding="utf-8") as f:
                f.write(json.dumps({"app": app, "summary": summary, "body": body}) + "\n")
            invocation.return_value(GLib.Variant("(u)", (counter[0],)))
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "actions"],)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant("(ssss)", ("test-notifications", "tests", "1", "1.2")))
        else:
            invocation.return_value(None)

    def acquired(conn, name):
        conn.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], call, None, None)

    def ready(conn, name):
        with open(record + ".ready", "w", encoding="utf-8") as f:
            f.write("1")

    Gio.bus_own_name(Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE, acquired, ready, lambda *a: loop.quit())
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
