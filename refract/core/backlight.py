"""Laptop panel backlight, for working in privacy.

Desk's centre screen is a MIRROR of the laptop panel, which rules out the
obvious implementation: turning the output off would kill the very thing the
wearer is looking at, and disabling a monitor in Mutter's config leaves
anyone whose glasses then fail with no usable display at all.

Killing the BACKLIGHT instead leaves the compositor untouched. Mutter still
paints the panel and our capture still reads those pixels, so the centre
screen keeps working while the physical panel goes dark to anyone nearby.
It is also recoverable by hand: the brightness keys still work, so a crash
with the light off is an annoyance rather than a blind terminal.

Goes through the desktop's own settings service rather than
/sys/class/backlight, which is root-only on a normal install. GNOME's
settings daemon and KDE's Powerdevil expose the same capability under
different names; whichever is on the session bus is used, so the brightness
is spoken to in the desktop's own terms (0-100 on GNOME, 0-max on KDE) and
the physical panel dims the same way on both.
"""

# GNOME: org.gnome.SettingsDaemon.Power, Brightness 0-100.
GSD_BUS = "org.gnome.SettingsDaemon.Power"
GSD_PATH = "/org/gnome/SettingsDaemon/Power"
GSD_IFACE = "org.gnome.SettingsDaemon.Power.Screen"

# KDE: org.kde.ScreenBrightness, one object per display, Brightness 0-MaxBrightness.
KDE_BUS = "org.kde.ScreenBrightness"
KDE_PATH = "/org/kde/ScreenBrightness"
KDE_IFACE = "org.kde.ScreenBrightness.Display"


def _bus():
    import gi                                             # noqa: F401
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _name_on_bus(name):
    try:
        from gi.repository import Gio
        dbus = Gio.DBusProxy.new_sync(
            _bus(), Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", None)
        return name in dbus.call_sync(
            "ListNames", None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    except Exception:                                     # noqa: BLE001
        return False


def _kde_display():
    """(props-proxy, iface-proxy, connector) for the first KDE display, or
    None. KDE puts each panel on its own object under /org/kde/ScreenBrightness
    -- there is normally just the built-in one on a laptop."""
    from gi.repository import Gio
    bus = _bus()
    root = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None, KDE_BUS, KDE_PATH,
        "org.freedesktop.DBus.Introspectable", None)
    xml = root.call_sync("Introspect", None, Gio.DBusCallFlags.NONE,
                         -1, None).unpack()[0]
    import re
    for node in re.findall(r'<node name="([^"]+)"', xml):
        path = KDE_PATH + "/" + node
        props = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None, KDE_BUS, path,
            "org.freedesktop.DBus.Properties", None)
        disp = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None, KDE_BUS, path, KDE_IFACE, None)
        return props, disp, node
    return None


def _kde_max(props):
    from gi.repository import GLib
    v = props.call_sync("Get", GLib.Variant("(ss)", (
        KDE_IFACE, "MaxBrightness")), 0, -1, None).unpack()[0]
    return int(v) or 1


def _use_kde():
    return _name_on_bus(KDE_BUS) and not _name_on_bus(GSD_BUS)


def _gsd_proxy():
    from gi.repository import Gio
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
        GSD_BUS, GSD_PATH, "org.freedesktop.DBus.Properties", None)


def get():
    """Current brightness 0-100, or None if the panel has no control.

    Normalised to 0-100 on both desktops, so the rest of this module (and the
    stale-blank marker file) is unit-agnostic; the KDE scale is converted back
    to raw counts only at the D-Bus boundary in set_level().
    """
    try:
        from gi.repository import GLib
        if _use_kde():
            props, _disp, _c = _kde_display()
            raw = props.call_sync("Get", GLib.Variant("(ss)", (
                KDE_IFACE, "Brightness")), 0, -1, None).unpack()[0]
            return int(round(100.0 * int(raw) / _kde_max(props)))
        p = _gsd_proxy()
        v = p.call_sync("Get", GLib.Variant("(ss)", (GSD_IFACE, "Brightness")),
                        0, -1, None).unpack()[0]
        return int(v)
    except Exception:                                     # noqa: BLE001
        return None


def set_level(value):
    try:
        from gi.repository import GLib
        if _use_kde():
            props, disp, _c = _kde_display()
            raw = int(round(_kde_max(props) * max(0, min(100, value)) / 100.0))
            # SetBrightness(raw, flags) -- flags 0, no on-screen OSD wanted
            disp.call_sync("SetBrightness", GLib.Variant("(iu)", (raw, 0)),
                           0, -1, None)
            return True
        p = _gsd_proxy()
        p.call_sync("Set", GLib.Variant("(ssv)", (
            GSD_IFACE, "Brightness", GLib.Variant("i", int(value)))),
            0, -1, None)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _marker_path():
    import os

    from refract.core.config import runtime_dir
    return os.path.join(runtime_dir(), "refract.backlight")


def recover_stale():
    """Undo a blanking that outlived the process that did it.

    A normal exit, an exception and now SIGTERM all restore the panel. A
    SIGKILL cannot: no code of ours runs. So blanking also drops a marker
    recording the previous level, and this puts it back the next time
    Refract starts. The brightness keys are the other way out, and they keep
    working throughout -- that is the reason this feature dims the panel
    instead of switching the output off, which would leave no way back.
    """
    import os
    path = _marker_path()
    try:
        with open(path) as f:
            level = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.unlink(path)
    except OSError:
        pass
    if get() == 0 and level > 0:
        set_level(level)
        print("  backlight: restored to %d%% after an unclean exit" % level,
              flush=True)
        return True
    return False


class Backlight:
    """Blank and restore, remembering what it was.

    Restoring is the important half: never leave someone staring at a dark
    laptop because a scene exited badly. exit(), park(), quit and SIGTERM
    all call restore(), and it is safe to call when nothing was blanked.
    """

    def __init__(self):
        self.saved = None

    @property
    def blanked(self):
        return self.saved is not None

    def blank(self):
        if self.blanked:
            return True
        level = get()
        if level is None:
            print("  backlight: no brightness control on this panel",
                  flush=True)
            return False
        self.saved = level
        if not set_level(0):
            self.saved = None
            return False
        try:                      # so a SIGKILL can still be recovered from
            with open(_marker_path(), "w") as f:
                f.write(str(level))
        except OSError:
            pass
        print("  backlight: laptop panel blanked (was %d%%)" % level,
              flush=True)
        return True

    def restore(self):
        if not self.blanked:
            return False
        level, self.saved = self.saved, None
        set_level(level)
        try:
            import os
            os.unlink(_marker_path())
        except OSError:
            pass
        print("  backlight: laptop panel restored to %d%%" % level,
              flush=True)
        return True
