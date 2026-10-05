#!/usr/bin/env python3
"""
sbs-display -- switch the glasses' output between 1920x1080 (2D) and
3840x1080 (3D side-by-side) on Wayland.

`viture-hw.py 3d on` flips the GLASSES into SBS mode; the compositor then has
to actually drive the 3840x1080 mode it starts advertising. xrandr cannot set
modes under Wayland, so this talks to Mutter's DisplayConfig D-Bus API
directly.

    ./sbs-display.py list                 # connectors and their modes
    ./sbs-display.py on                   # widest 1080-tall mode on the glasses
    ./sbs-display.py off                  # back to 1920x1080
    ./sbs-display.py on --connector DP-2 --persistent

Default config method is TEMPORARY: it reverts on logout, and Mutter asks for
confirmation. Pass --persistent to write it into the session config.

Uses system python3 + python3-gi, no venv needed.
"""

import argparse
import sys

import gi
from gi.repository import GLib, Gio

BUS = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"

METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2

# EDID on the Pro XR reads vendor "CVT", product "VITURE" -- match either field.
VITURE_EDID = ("VITURE", "VTR", "VIT")


def proxy():
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
        BUS, PATH, BUS, None)


def state(p):
    serial, monitors, logical, props = p.call_sync(
        "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None).unpack()
    return serial, monitors, logical, props


def find_glasses(monitors):
    """Connector of the VITURE display: by EDID vendor, else the small panel."""
    for (conn, vendor, product, ser), modes, mprops in monitors:
        if any(tag in (vendor + " " + product).upper() for tag in VITURE_EDID):
            return conn
    # fall back: external, physically tiny, 1080-capable
    for (conn, vendor, product, ser), modes, mprops in monitors:
        if conn.startswith(("DP-", "HDMI-")) and any(
                m[2] == 1080 for m in modes):
            return conn
    return None


def pick_mode(modes, want_w, height=1080):
    """Mode id for want_w x height; widest available if want_w is None."""
    cand = [m for m in modes if m[2] == height
            and (want_w is None or m[1] == want_w)]
    if not cand:
        return None, None
    # highest refresh, then widest
    best = max(cand, key=lambda m: (m[1], m[3]))
    return best[0], (best[1], best[2], best[3])


def cmd_list(p):
    serial, monitors, logical, props = state(p)
    for (conn, vendor, product, ser), modes, mprops in monitors:
        cur = next((m for m in modes if m[6].get("is-current")), None)
        print("  %-8s %s %s" % (conn, vendor, product))
        print("    current: %s" % (
            "%dx%d @%.2f" % (cur[1], cur[2], cur[3]) if cur else "none"))
        widths = sorted({m[1] for m in modes if m[2] == 1080})
        print("    1080-tall widths: %s" % (
            ", ".join(str(w) for w in widths) or "none"))
    print("  glasses detected as: %s" % find_glasses(monitors))


def apply(p, connector, want_w, method):
    serial, monitors, logical, props = state(p)

    target = connector or find_glasses(monitors)
    if not target:
        sys.exit("could not identify the glasses output; pass --connector")

    modes = next((m for (spec, m, _) in monitors if spec[0] == target), None)
    if modes is None:
        sys.exit("no such connector: %s" % target)

    mode_id, dims = pick_mode(modes, want_w)
    if mode_id is None:
        sys.exit("%s has no %s x1080 mode -- is the headset in the right "
                 "dimension? (viture-hw.py 3d on)"
                 % (target, want_w if want_w else "1080-tall"))

    # rebuild every logical monitor, swapping only the target's mode
    out = []
    x_cursor = None
    for (x, y, scale, transform, primary, mons, lprops) in logical:
        entries = []
        for (conn, vendor, product, ser) in mons:
            if conn == target:
                entries.append((conn, mode_id, {}))
            else:
                cur = next((m[0] for (spec, ms, _) in monitors
                            if spec[0] == conn
                            for m in ms if m[6].get("is-current")), None)
                entries.append((conn, cur, {}))
        out.append((x, y, scale, transform, primary, entries))

    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                        (serial, method, out, {}))
    p.call_sync("ApplyMonitorsConfig", args, Gio.DBusCallFlags.NONE, -1, None)
    print("  %s -> %dx%d @%.2f  (%s)" % (
        target, dims[0], dims[1], dims[2],
        "persistent" if method == METHOD_PERSISTENT else "temporary"))
    if method == METHOD_TEMPORARY:
        print("  Mutter will ask on-screen to keep the change.")


def main():
    ap = argparse.ArgumentParser(description="glasses SBS mode, Wayland")
    ap.add_argument("cmd", choices=["list", "on", "off"])
    ap.add_argument("--connector", help="e.g. DP-2 (default: auto-detect)")
    ap.add_argument("--width", type=int,
                    help="explicit width instead of widest (on) / 1920 (off)")
    ap.add_argument("--persistent", action="store_true",
                    help="save into the session config instead of temporary")
    a = ap.parse_args()

    p = proxy()
    if a.cmd == "list":
        return cmd_list(p)

    method = METHOD_PERSISTENT if a.persistent else METHOD_TEMPORARY
    want = a.width if a.width else (None if a.cmd == "on" else 1920)
    apply(p, a.connector, want, method)


if __name__ == "__main__":
    sys.exit(main())
