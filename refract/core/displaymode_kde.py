"""Display control through KScreen (kscreen-doctor), for KDE and other
non-GNOME Wayland sessions.

The KDE counterpart to the Mutter DisplayConfig code in
refract.core.displaymode: same questions -- which output is the glasses, is
it in side-by-side, lay the monitors out in a row -- answered through
`kscreen-doctor` instead of org.gnome.Mutter.DisplayConfig. displaymode.py
dispatches to here when the session is not GNOME; nothing else imports it.

Two differences from the Mutter path are worth stating plainly:

  * kscreen-doctor's JSON carries no EDID, so the glasses cannot be matched
    by vendor there. This reads the raw EDID out of sysfs instead (DE-
    independent and exactly what identifies the panel), and falls back to
    the same heuristic the Mutter path uses -- an external output advertising
    a 1080-tall mode -- when sysfs is unreadable.

  * KScreen applies layout changes to its stored config, where Mutter's
    TEMPORARY method reverts them on logout. Desk already snapshots and
    restores monitor positions itself (DeskScene._saved_positions), so the
    normal path puts everything back; the residual gap is a crash between
    rearrange and restore, which a wearer recovers from Display settings.
    Positioning stays opt-in ("Match desktop layout") for that reason.
"""

import glob
import json
import os
import subprocess
import time

# EDID/PNP tags the Pro XR panel reports -- same set the Mutter path matches.
VITURE_EDID = ("VITURE", "VTR", "VIT", "CVT")

# KScreen's Output::Type is a numeric enum; 7 is the built-in Panel (eDP/
# LVDS/DSI), never the glasses. Connector-name prefixes catch the same
# thing DE-independently, for when the enum ever moves.
INTERNAL_TYPE = 7
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")


def _run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout)


def _state():
    """Parsed `kscreen-doctor -j`, or None if it cannot be read."""
    try:
        out = _run(["kscreen-doctor", "-j"]).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _current_mode(output):
    mid = output.get("currentModeId")
    for m in output.get("modes", []):
        if m.get("id") == mid:
            return m
    return None


def _mode_size(output):
    m = _current_mode(output)
    if not m:
        return (0, 0)
    return (m["size"]["width"], m["size"]["height"])


def _pnp_id(edid):
    """Three-letter manufacturer id from EDID bytes 8-9 (the standard 5-bit
    packed encoding)."""
    if len(edid) < 10:
        return ""
    v = (edid[8] << 8) | edid[9]
    return "".join(chr(((v >> s) & 0x1F) + 0x40) for s in (10, 5, 0))


def _edid_model(edid):
    """The monitor-name string from an EDID descriptor block (tag 0xFC), or
    empty. The four 18-byte descriptors start at byte 54."""
    for off in (54, 72, 90, 108):
        blk = edid[off:off + 18]
        if len(blk) == 18 and blk[0:2] == b"\x00\x00" and blk[3] == 0xFC:
            return blk[5:].split(b"\n", 1)[0].decode("ascii", "ignore").strip()
    return ""


def _edids():
    """{connector-name: (vendor, product)} read from sysfs, so the glasses can
    be matched by EDID without a compositor that exposes it.

    DRM connector directories are named cardN-DP-1; KScreen names the same
    output DP-1, so the cardN- prefix is stripped to line them up.
    """
    out = {}
    for path in glob.glob("/sys/class/drm/card*-*/edid"):
        try:
            with open(path, "rb") as f:
                edid = f.read()
        except OSError:
            continue
        if len(edid) < 128:
            continue
        conn = os.path.basename(os.path.dirname(path))
        conn = conn.split("-", 1)[1] if "-" in conn else conn
        out[conn] = (_pnp_id(edid), _edid_model(edid))
    return out


def _is_internal(output):
    name = output.get("name", "")
    return (output.get("type") == INTERNAL_TYPE
            or name.startswith(INTERNAL_PREFIXES))


def glasses_connector():
    """KScreen name of the VITURE output: by EDID vendor, else the external
    output that looks like the glasses (non-internal, 1080-capable).

    Matched against CONNECTED outputs, not just enabled ones -- same as the
    Mutter path, whose GetCurrentState lists every attached monitor. The
    desktop may have the glasses' display disabled (a leftover config, or
    mid-handoff); they are still the glasses, and skipping them here would
    hand the fallback below to whatever ordinary monitor is also attached.
    """
    state = _state()
    if not state:
        return None
    outputs = [o for o in state.get("outputs", []) if o.get("connected")]
    names = {o["name"] for o in outputs}

    for conn, (vendor, model) in _edids().items():
        tag = ("%s %s" % (vendor, model)).upper()
        if conn in names and any(t in tag for t in VITURE_EDID):
            return conn

    for o in outputs:
        if _is_internal(o):
            continue
        heights = {m["size"]["height"] for m in o.get("modes", [])}
        if 1080 in heights:
            return o["name"]
    return None


def list_outputs():
    """[{connector, vendor, product, current (w,h,hz) or None, widths_1080}].

    vendor/product come from the sysfs EDID where available (KScreen's JSON
    has none), so the shape matches the Mutter path's list_outputs.
    """
    state = _state()
    if not state:
        return []
    edids = _edids()
    out = []
    for o in state.get("outputs", []):
        if not o.get("connected"):
            continue
        cur = _current_mode(o)
        vendor, product = edids.get(o["name"], ("", ""))
        out.append({
            "connector": o["name"],
            "vendor": vendor,
            "product": product,
            "current": (cur["size"]["width"], cur["size"]["height"],
                        round(cur.get("refreshRate", 0))) if cur else None,
            "widths_1080": sorted({m["size"]["width"]
                                   for m in o.get("modes", [])
                                   if m["size"]["height"] == 1080}),
        })
    return out


def logical_layout():
    """[(connector, x, y, logical_w, logical_h)] for every enabled output.

    Logical size is the mode divided by the scale -- the space the pointer
    travels through, not the pixel mode -- with rotation swapping w/h.

    KWin names every portal virtual output after the app that asked for it,
    so two virtual monitors from one process carry the SAME name. Names that
    collide get their KScreen id appended (`name@id`) so callers can tell
    them apart and apply_positions can address exactly one of them; unique
    names stay plain, so DP-1 is still DP-1 everywhere.
    """
    state = _state()
    if not state:
        return []
    enabled = [o for o in state.get("outputs", []) if o.get("enabled")]
    dupes = {n for n in (o["name"] for o in enabled)
             if sum(1 for o in enabled if o["name"] == n) > 1}
    out = []
    for o in enabled:
        w, h = _mode_size(o)
        scale = o.get("scale") or 1.0
        # KScreen rotation: 1 none, 2 left(90), 4 inverted, 8 right(270)
        if o.get("rotation") in (2, 8):
            w, h = h, w
        pos = o.get("pos", {"x": 0, "y": 0})
        name = o["name"]
        if name in dupes:
            name = "%s@%d" % (name, o["id"])
        out.append((name, int(pos["x"]), int(pos["y"]),
                    int(w / scale), int(h / scale)))
    return sorted(out, key=lambda r: (r[1], r[0]))


def apply_positions(positions, persistent=False):
    """Move logical monitors. positions: {connector: (x, y)}.

    One kscreen-doctor invocation carrying every origin change; only the
    positions move, modes and scales are left alone. A `name@id` connector
    (see logical_layout) is addressed by its numeric KScreen id, which is
    the only unambiguous handle when names collide. `persistent` is accepted
    for signature-parity with the Mutter path -- KScreen writes its config
    either way, so it makes no difference here (see the module header on how
    Desk restores the layout regardless)."""
    args = ["kscreen-doctor"]
    for conn, (x, y) in positions.items():
        name, _, oid = conn.rpartition("@")
        sel = oid if name and oid.isdigit() else conn
        args.append("output.%s.position.%d,%d" % (sel, int(x), int(y)))
    if len(args) > 1:
        _run(args)


def _connector_size(connector):
    state = _state()
    if not state:
        return (0, 0)
    for o in state.get("outputs", []):
        if o.get("name") == connector:
            return _mode_size(o)
    return (0, 0)


def is_sbs(connector):
    """Is `connector` currently in the wide side-by-side mode?"""
    return _connector_size(connector)[0] >= 3840


def wait_for_mode(monitor, needle="3840x1080", tries=20, delay=0.5):
    """Poll KScreen until the connector reports the wanted mode -- the glasses
    re-enumerate after a dimension switch, so this is how 'did it take' is
    verified (the Mutter path polls xrandr; KScreen is the Wayland-native
    equivalent)."""
    try:
        want_w, want_h = (int(v) for v in needle.split("x"))
    except ValueError:
        want_w, want_h = 3840, 1080
    for _ in range(tries):
        if _connector_size(monitor) == (want_w, want_h):
            return True
        time.sleep(delay)
    return False
