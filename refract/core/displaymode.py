"""Compositor-side display mode control, Wayland-safe.

Extracted from sbs-display.py (which remains the standalone CLI). Under
Wayland xrandr is READ-ONLY, so mode setting goes through Mutter's
DisplayConfig D-Bus API: GetCurrentState -> swap the target's mode id ->
ApplyMonitorsConfig.

Default method is TEMPORARY: it reverts on logout (a crashed shell can never
leave the physical displays permanently wrong -- exactly what Display Handoff
wants) and Mutter asks on-screen to keep the change.
"""

import subprocess
import time

import gi                                                 # noqa: F401
from gi.repository import Gio, GLib

BUS = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"

METHOD_TEMPORARY = 1
METHOD_PERSISTENT = 2

# EDID on the Pro XR reads vendor "CVT", product "VITURE" -- match either field.
VITURE_EDID = ("VITURE", "VTR", "VIT")


def _proxy():
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
        BUS, PATH, BUS, None)


def get_state(p=None):
    p = p or _proxy()
    serial, monitors, logical, props = p.call_sync(
        "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None).unpack()
    return p, serial, monitors, logical, props


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


def glasses_connector():
    _, _, monitors, _, _ = get_state()
    return find_glasses(monitors)


def list_outputs():
    """[{connector, vendor, product, current (w,h,hz) or None, widths_1080}]"""
    _, _, monitors, _, _ = get_state()
    out = []
    for (conn, vendor, product, ser), modes, mprops in monitors:
        cur = next((m for m in modes if m[6].get("is-current")), None)
        out.append({
            "connector": conn, "vendor": vendor, "product": product,
            "current": (cur[1], cur[2], cur[3]) if cur else None,
            "widths_1080": sorted({m[1] for m in modes if m[2] == 1080}),
        })
    return out


def _pick_mode(modes, want_w, height=1080):
    """Mode id for want_w x height; widest available if want_w is None."""
    cand = [m for m in modes if m[2] == height
            and (want_w is None or m[1] == want_w)]
    if not cand:
        return None, None
    # highest refresh, then widest
    best = max(cand, key=lambda m: (m[1], m[3]))
    return best[0], (best[1], best[2], best[3])


def apply_mode(sbs, connector=None, width=None, persistent=False):
    """Drive the glasses output at its SBS (widest 1080-tall) or 2D (1920)
    mode. Returns (connector, (w, h, hz)). Raises RuntimeError when the
    wanted mode is not there -- usually the headset dimension hasn't been
    switched first (Glasses.switch_dimension / Head.set_sbs)."""
    p, serial, monitors, logical, props = get_state()

    target = connector or find_glasses(monitors)
    if not target:
        raise RuntimeError("could not identify the glasses output")

    modes = next((m for (spec, m, _) in monitors if spec[0] == target), None)
    if modes is None:
        raise RuntimeError("no such connector: %s" % target)

    want_w = width if width else (None if sbs else 1920)
    mode_id, dims = _pick_mode(modes, want_w)
    if mode_id is None:
        raise RuntimeError(
            "%s has no %s x1080 mode -- is the headset in the right "
            "dimension?" % (target, want_w if want_w else "1080-tall"))

    # rebuild every logical monitor, swapping only the target's mode
    out = []
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

    method = METHOD_PERSISTENT if persistent else METHOD_TEMPORARY
    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                        (serial, method, out, {}))
    p.call_sync("ApplyMonitorsConfig", args, Gio.DBusCallFlags.NONE, -1, None)
    return target, dims


def logical_layout():
    """[(connector, x, y, logical_w, logical_h)] for every monitor.

    Logical size is the mode divided by the scale -- that, not the pixel
    mode, is the space the pointer travels through.
    """
    _, _, monitors, logical, _ = get_state()
    modes = {}
    for (conn, vendor, product, ser), ms, _p in monitors:
        cur = next((m for m in ms if m[6].get("is-current")), None)
        if cur:
            modes[conn] = (cur[1], cur[2])
    out = []
    for (x, y, scale, transform, primary, mons, props) in logical:
        for (conn, vendor, product, ser) in mons:
            w, h = modes.get(conn, (0, 0))
            if transform in (1, 3, 5, 7):          # 90/270 degree rotations
                w, h = h, w
            out.append((conn, int(x), int(y), int(w / (scale or 1.0)),
                        int(h / (scale or 1.0))))
    return sorted(out, key=lambda r: (r[1], r[0]))


def apply_positions(positions, persistent=False):
    """Move logical monitors. positions: {connector: (x, y)}.

    Keeps every monitor's current mode and scale -- only the origin moves.
    Temporary by default, so a crash cannot leave the desktop rearranged
    past logout.
    """
    p, serial, monitors, logical, _props = get_state()
    out = []
    for (x, y, scale, transform, primary, mons, lprops) in logical:
        entries = []
        nx, ny = x, y
        for (conn, vendor, product, ser) in mons:
            if conn in positions:
                nx, ny = positions[conn]
            cur = next((m[0] for (spec, ms, _) in monitors
                        if spec[0] == conn
                        for m in ms if m[6].get("is-current")), None)
            entries.append((conn, cur, {}))
        out.append((int(nx), int(ny), scale, transform, primary, entries))

    method = METHOD_PERSISTENT if persistent else METHOD_TEMPORARY
    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                        (serial, method, out, {}))
    p.call_sync("ApplyMonitorsConfig", args, Gio.DBusCallFlags.NONE, -1, None)


def wait_for_mode(monitor, needle="3840x1080", tries=20, delay=0.5):
    """Poll xrandr (read-only is fine under Wayland) until the connector
    advertises the wanted mode -- the glasses re-enumerate after a dimension
    switch, so this is how 'did it take' is actually verified."""
    for _ in range(tries):
        geom = subprocess.run(["xrandr"], capture_output=True,
                              text=True).stdout
        if any(ln.startswith(monitor + " ") and needle in ln
               for ln in geom.splitlines()):
            return True
        time.sleep(delay)
    return False


def is_sbs(monitor):
    geom = subprocess.run(["xrandr"], capture_output=True, text=True).stdout
    line = next((ln for ln in geom.splitlines()
                 if ln.startswith(monitor + " ")), "")
    return "3840x1080" in line
