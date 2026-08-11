#!/usr/bin/env python3
"""
virtual-monitors -- create real virtual monitors in the running GNOME session.

These are genuine outputs as far as Mutter is concerned, which is the whole
point: the pointer crosses into them, windows maximise on them, and the
clipboard is simply the session clipboard. Nothing here reimplements a desktop.
Breezy Desktop then renders them in the headset.

    ./virtual-monitors.py                 # two 1920x1080 monitors
    ./virtual-monitors.py -n 3            # three
    ./virtual-monitors.py --ultrawide     # one 5760x1080
    ./virtual-monitors.py -n 2 --size 2560x1440

Monitors exist for as long as this runs; Ctrl-C removes them.

How it works, and why it is not simply a D-Bus call. Three things must all be
true before Mutter will hand you a monitor, and missing any one of them fails
SILENTLY -- the call succeeds and no monitor appears:

  1. The ScreenCast session must be bound to a RemoteDesktop session via
     `remote-desktop-session-id`. This is the one that is easy to miss:
     RecordVirtual on a bare ScreenCast session returns a valid stream path and
     produces nothing. The binding is also what makes pointer input land on the
     monitor rather than passing through it.
  2. Something must CONSUME each PipeWire stream. The negotiated caps become
     the monitor's resolution, so with no consumer there is no resolution and
     no monitor. That is normally gnome-remote-desktop's job; here it is a
     gstreamer pipeline into fakesink.
  3. The RemoteDesktop session -- not the ScreenCast one -- gets Start().
"""

import argparse
import signal
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst        # noqa: E402

SC_NAME = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
RD_NAME = "org.gnome.Mutter.RemoteDesktop"
RD_PATH = "/org/gnome/Mutter/RemoteDesktop"


class VirtualMonitors:
    def __init__(self, sizes, refresh=60):
        self.sizes = sizes
        self.refresh = refresh
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.pipelines = []
        self.rd_session = None
        self.sc_session = None
        self.pending = {}          # stream path -> (w, h)

    def _proxy(self, name, path, iface):
        return Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None,
                                      name, path, iface, None)

    def start(self):
        rd = self._proxy(RD_NAME, RD_PATH, RD_NAME)
        rd_path = rd.call_sync("CreateSession", None,
                               Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
        self.rd_session = self._proxy(RD_NAME, rd_path, RD_NAME + ".Session")
        rd_id = self.rd_session.get_cached_property("SessionId").unpack()

        sc = self._proxy(SC_NAME, SC_PATH, SC_NAME)
        sc_path = sc.call_sync(
            "CreateSession", GLib.Variant("(a{sv})", (
                {"remote-desktop-session-id": GLib.Variant("s", rd_id)},)),
            Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
        self.sc_session = self._proxy(SC_NAME, sc_path, SC_NAME + ".Session")

        for w, h in self.sizes:
            props = {"cursor-mode": GLib.Variant("u", 1)}
            stream_path = self.sc_session.call_sync(
                "RecordVirtual", GLib.Variant("(a{sv})", (props,)),
                Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
            self.pending[stream_path] = (w, h)
            self.bus.signal_subscribe(
                SC_NAME, SC_NAME + ".Stream", "PipeWireStreamAdded",
                stream_path, None, Gio.DBusSignalFlags.NONE,
                self._on_stream_added, None)
            print("  requested %dx%d" % (w, h))

        # Start the REMOTE DESKTOP session, not the screencast one.
        self.rd_session.call_sync("Start", None, Gio.DBusCallFlags.NONE,
                                  -1, None)
        print("  session started; waiting for PipeWire nodes")

    def _on_stream_added(self, conn, sender, path, iface, signal, params,
                         user_data):
        node_id = params.unpack()[0]
        w, h = self.pending.get(path, (1920, 1080))
        # The caps we negotiate here BECOME the monitor's resolution.
        desc = ("pipewiresrc path=%d ! "
                "video/x-raw,width=%d,height=%d ! "
                "fakesink sync=false" % (node_id, w, h))
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        self.pipelines.append(pipe)
        print("  node %d consuming at %dx%d -> monitor should appear"
              % (node_id, w, h))

    def stop(self):
        for p in self.pipelines:
            p.set_state(Gst.State.NULL)
        self.pipelines = []
        if self.rd_session:
            try:
                self.rd_session.call_sync("Stop", None, Gio.DBusCallFlags.NONE,
                                          -1, None)
            except Exception:
                pass
            self.rd_session = None
        print("\n  virtual monitors removed")


def main():
    ap = argparse.ArgumentParser(description="virtual monitors for GNOME")
    ap.add_argument("-n", "--count", type=int, default=2,
                    help="how many virtual monitors (default 2)")
    ap.add_argument("--size", default="1920x1080", help="WxH per monitor")
    ap.add_argument("--ultrawide", action="store_true",
                    help="one very wide monitor instead of several")
    ap.add_argument("--ultrawide-size", default="5760x1080")
    ap.add_argument("--refresh", type=int, default=60)
    a = ap.parse_args()

    Gst.init(None)
    if a.ultrawide:
        w, h = (int(v) for v in a.ultrawide_size.lower().split("x"))
        sizes = [(w, h)]
    else:
        w, h = (int(v) for v in a.size.lower().split("x"))
        sizes = [(w, h)] * max(1, a.count)

    vm = VirtualMonitors(sizes, a.refresh)
    vm.start()

    loop = GLib.MainLoop()

    def quit_(*_):
        vm.stop()
        loop.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, quit_)
    try:
        loop.run()
    finally:
        vm.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
