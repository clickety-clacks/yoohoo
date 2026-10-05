#!/usr/bin/env python3
"""Run under dbus-run-session on Testbed; never suspends the real machine.

The private session bus stands in for the system bus in this child process.
An owned login1 name emits actual D-Bus signals to the production Gio watcher.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import time


def wait_for(predicate, description, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(description)


def main():
    address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    # Applied before any Gio connection in this disposable test child only.
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = address
    from gi.repository import Gio, GLib

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("hub_sleep_bus_client", root / "payload/agentd_hub.py")
    hub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub)
    service = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None,
    )
    reply = service.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
        GLib.Variant("(su)", ("org.freedesktop.login1", 0)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None,
    )
    assert reply.unpack() == (1,), "fixture did not own private login1 name"
    with tempfile.TemporaryDirectory(prefix="yoohoo-sleep-bus-") as directory:
        client = hub.AgentdHub({"agentd_hub": {"enabled": True, "url": "http://127.0.0.1:1"}}, Path(directory))
        # Only the real watcher runs; there is no network or notification loop.
        client._start_sleep_watcher()
        try:
            wait_for(lambda: client._sleep_bus is not None, "sleep watcher did not subscribe")
            # Round trip on subscriber connection orders its AddMatch before
            # the fixture emits signals from the separate service connection.
            subscriber = client._sleep_bus[0]
            subscriber.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus", "GetId", None, None,
                                 Gio.DBusCallFlags.NONE, 1000, None)

            def emit(sleeping):
                service.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                                    "PrepareForSleep", GLib.Variant("(b)", (sleeping,)))
                service.flush_sync(None)

            emit(True)
            wait_for(lambda: client.status()["status"] == "suspended", "real sleep signal was not handled")
            assert not client.connected
            emit(False)
            wait_for(lambda: client.status()["status"] == "reconnecting", "real resume signal was not handled")
            assert not client.connected, "resume alone must not claim a fresh snapshot"
            watcher = client._sleep_thread
            client.stop()
            assert not watcher.is_alive(), "sleep watcher leaked after stop"
            stopped = client.status().copy()
            emit(True)
            time.sleep(0.1)
            assert client.status() == stopped, "late signal changed stopped status"
            print("HUB_SLEEP_PRIVATE_BUS_PASS", flush=True)
        finally:
            client.stop()
            service.close_sync(None)


if __name__ == "__main__":
    main()
