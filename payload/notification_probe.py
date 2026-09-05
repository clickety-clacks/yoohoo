"""Passive notification identity diagnostic. Does not log message content.

Run with python; Ctrl+C stops it. Uses a dedicated monitor connection and a
separate normal bus connection for sender credentials. Never invokes actions.
"""
import json
import subprocess
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
lookup = dbus.SessionBus(private=True)
monitor = dbus.SessionBus(private=True)
credentials = dbus.Interface(lookup.get_object(
    "org.freedesktop.DBus", "/org/freedesktop/DBus"), "org.freedesktop.DBus")


def received(connection, message):
    if (message.get_interface() != "org.freedesktop.Notifications"
            or message.get_member() != "Notify"):
        return
    sender = message.get_sender()
    record = {"at": time.time(), "sender": sender}
    try:
        pid = int(credentials.GetConnectionUnixProcessID(sender))
        clients = json.loads(subprocess.check_output(
            ["hyprctl", "-j", "clients"], text=True, timeout=2))
        matches = [c["address"] for c in clients if c.get("pid") == pid]
        record.update(pid=pid, matches=matches,
                      resolution="unique" if len(matches) == 1 else "unresolved")
    except Exception as error:
        record["error"] = str(error)
    print(json.dumps(record), flush=True)


monitor.add_message_filter(received)
monitor.call_blocking("org.freedesktop.DBus", "/org/freedesktop/DBus",
                      "org.freedesktop.DBus.Monitoring", "BecomeMonitor", "asu",
                      (["type='method_call',interface='org.freedesktop.Notifications',member='Notify'"], 0))
GLib.MainLoop().run()
