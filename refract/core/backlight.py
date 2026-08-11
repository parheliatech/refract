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

Goes through GNOME's settings daemon rather than /sys/class/backlight,
which is root-only on a normal install.
"""

BUS = "org.gnome.SettingsDaemon.Power"
PATH = "/org/gnome/SettingsDaemon/Power"
IFACE = "org.gnome.SettingsDaemon.Power.Screen"


def _proxy():
    import gi                                             # noqa: F401
    from gi.repository import Gio
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
        BUS, PATH, "org.freedesktop.DBus.Properties", None)


def get():
    """Current brightness 0-100, or None if the panel has no control."""
    try:
        from gi.repository import GLib
        p = _proxy()
        v = p.call_sync("Get", GLib.Variant("(ss)", (IFACE, "Brightness")),
                        0, -1, None).unpack()[0]
        return int(v)
    except Exception:                                     # noqa: BLE001
        return None


def set_level(value):
    try:
        from gi.repository import GLib
        p = _proxy()
        p.call_sync("Set", GLib.Variant("(ssv)", (
            IFACE, "Brightness", GLib.Variant("i", int(value)))),
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
