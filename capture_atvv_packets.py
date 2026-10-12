"""Diagnostic packet capture for the paired RC003-MS; no audio playback.

Uses system Python's dbus-python and PyGObject. Records ATVV notifications
in one D-Bus callback order, before ADPCM decoding and PipeWire buffering.
"""
import argparse
import json
import signal
import time
from pathlib import Path

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

CHAR = "org.bluez.GattCharacteristic1"
PROPS = "org.freedesktop.DBus.Properties"
BASE = "-5a21-4f05-bc7d-af01f617b664"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--sessions", type=int, default=0,
                        help="Stop automatically after this many completed sessions; 0 waits for timeout")
    parser.add_argument("--skip-caps", action="store_true",
                        help="Reuse the existing connected device's negotiated capabilities without GET_CAPS")
    args = parser.parse_args()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    manager = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    objects = manager.GetManagedObjects()
    suffix = "/dev_" + args.device.upper().replace(":", "_")
    devices = [str(path) for path, interfaces in objects.items()
               if str(path).endswith(suffix) and "org.bluez.Device1" in interfaces]
    if len(devices) != 1:
        raise RuntimeError("Expected one already-paired device")
    device = devices[0]
    state = objects[dbus.ObjectPath(device)]["org.bluez.Device1"]
    if not state.get("Connected") or not state.get("ServicesResolved"):
        raise RuntimeError("Device must be connected with resolved services")
    paths = {}
    for path, interfaces in objects.items():
        props = interfaces.get(CHAR, {})
        uuid = str(props.get("UUID", "")).lower()
        if str(path).startswith(device + "/"):
            for kind, short in (("tx", "ab5e0002"), ("rx", "ab5e0003"), ("ctl", "ab5e0004")):
                if uuid == short + BASE:
                    paths[kind] = str(path)
    if set(paths) != {"tx", "rx", "ctl"}:
        raise RuntimeError("ATVV characteristics not found")
    interfaces = {kind: dbus.Interface(bus.get_object("org.bluez", path), CHAR)
                  for kind, path in paths.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier capture.
    output = args.output.open("x")
    started = time.monotonic_ns()
    subscribed = []
    streaming = False
    stream_id = 0
    counts = {"rx": 0, "ctl": 0}
    completed_sessions = 0
    loop = GLib.MainLoop()

    def record(kind, data=b"", **extra):
        output.write(json.dumps({"t_ns": time.monotonic_ns() - started,
                                 "kind": kind, "hex": data.hex(), **extra}) + "\n")
        output.flush()

    def command(data):
        interfaces["tx"].WriteValue(dbus.ByteArray(data), {"type": "command"})
        record("tx", data)

    def changed(iface, changed_props, invalidated, path=None):
        nonlocal streaming, stream_id, completed_sessions
        if iface != CHAR or "Value" not in changed_props:
            return
        kind = next((kind for kind in ("rx", "ctl") if str(path) == paths[kind]), None)
        if kind is None:
            return
        data = bytes(changed_props["Value"])
        counts[kind] += 1
        record(kind, data)
        if kind == "ctl":
            print("CTL", data.hex(), flush=True)
            if data[:1] == b"\x04" and len(data) >= 4:
                streaming, stream_id = True, data[3]
            elif data[:1] == b"\x00":
                if streaming:
                    completed_sessions += 1
                    print("COMPLETED SESSION", completed_sessions, flush=True)
                streaming = False
                if args.sessions and completed_sessions >= args.sessions:
                    loop.quit()
            elif data[:1] == b"\x08" and not streaming:
                command(b"\x0c\x00")

    match = bus.add_signal_receiver(changed, signal_name="PropertiesChanged",
                                    dbus_interface=PROPS, bus_name="org.bluez", path_keyword="path")
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    try:
        for kind in ("ctl", "rx"):
            interfaces[kind].StartNotify()
            subscribed.append(kind)
        record("metadata", device=args.device, capture="ordered-dbus-notifications", skip_caps=args.skip_caps)
        if not args.skip_caps:
            command(bytes.fromhex("0a0100000303"))
        GLib.timeout_add_seconds(args.seconds, lambda: (loop.quit(), False)[1])
        print("READY: ATVV packet capture; no PipeWire stream", flush=True)
        loop.run()
    finally:
        if streaming:
            try:
                command(bytes((0x0D, stream_id)))
            except dbus.DBusException as error:
                record("cleanup_error", error=str(error))
        for kind in reversed(subscribed):
            try:
                interfaces[kind].StopNotify()
            except dbus.DBusException as error:
                record("cleanup_error", error=str(error))
        match.remove()
        record("end", counts=counts)
        output.close()
        print("STOPPED", counts, flush=True)


if __name__ == "__main__":
    main()
