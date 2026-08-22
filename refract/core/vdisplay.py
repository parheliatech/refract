#!/usr/bin/env python3
"""
vdisplay -- virtual monitors in the running GNOME session, with their pixels.

Genuine Mutter outputs, not a simulation. That distinction is the whole design:
the pointer crosses into them, windows maximise on them, and the clipboard is
just the session clipboard. None of that has to be implemented.

Depends only on GNOME (Mutter), PipeWire and GStreamer. No XR driver, no
desktop extension.

Three conditions must ALL hold before Mutter produces a monitor, and every
failure is silent -- the calls succeed and nothing appears:

  1. The ScreenCast session must be bound to a RemoteDesktop session through
     `remote-desktop-session-id`. A bare ScreenCast session hands back valid
     stream paths and creates no monitors. That binding is also what makes
     pointer input land on the monitor instead of passing through it.
  2. Something must CONSUME each PipeWire stream: the negotiated caps become
     the monitor's resolution, so no consumer means no monitor.
  3. Start() goes to the RemoteDesktop session, not the ScreenCast one.

ScreenCapture generalises this to a mixed set of streams in ONE session:
`("virtual", (w, h))` creates a new monitor, `("monitor", "eDP-1")` mirrors
an existing output. Refract Desk needs both at once -- side monitors that
are real extended displays, and a centre screen mirroring the laptop panel
-- and one session means one Start(), one lifetime, one thing to tear down.

The two kinds differ in one important way: a virtual stream's caps DEFINE
the monitor's resolution, so width/height must be forced. A mirror's
resolution comes from the output being mirrored, so forcing a size there
would silently rescale it -- let it negotiate and read the size back off the
caps instead.
"""

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gio, GLib, Gst, GstApp        # noqa: E402,F401

from . import fastblit                                  # noqa: E402

SC_NAME = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
RD_NAME = "org.gnome.Mutter.RemoteDesktop"
RD_PATH = "/org/gnome/Mutter/RemoteDesktop"

# what Mutter names them; useful for spotting them in a monitor list
VIRTUAL_PRODUCT = "Virtual remote monitor"

BTN_LEFT = 0x110        # evdev code, which is what NotifyPointerButton wants


class ScreenCapture:
    """A mixed set of streams in one Mutter session.

    specs: list of ("virtual", (w, h)) -- a NEW monitor
                   ("monitor", connector) -- a mirror of an existing output

    With capture=True the pixels are readable; capture=False just
    materialises the virtual monitors (fakesink) -- cheaper when something
    else is doing the drawing.
    """

    def __init__(self, specs, capture=True, cursor=True):
        self.specs = [(kind, arg) for kind, arg in specs]
        self.capture = capture
        # Mutter's CursorMode: 0 HIDDEN, 1 EMBEDDED, 2 METADATA.
        # EMBEDDED draws the pointer INTO the frame, which is the only way it
        # can be seen on a screen we are re-rendering in 3D. METADATA sends
        # the position out-of-band for a client to draw itself -- we were
        # asking for that, so the pointer was never in the pixels and the
        # virtual monitors looked unusable.
        self.cursor_mode = 1 if cursor else 0
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.rd_session = None
        self.sc_session = None
        self.pipelines = []
        self.sinks = [None] * len(self.specs)
        self._pending = {}                      # stream path -> index
        self.stream_paths = [None] * len(self.specs)
        self.started = False

    @property
    def sizes(self):
        """Declared sizes; None for mirrors, whose size the output decides."""
        return [arg if kind == "virtual" else None
                for kind, arg in self.specs]

    def _proxy(self, name, path, iface):
        return Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None,
                                      name, path, iface, None)

    def start(self):
        if not Gst.is_initialized():
            Gst.init(None)

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

        props = {"cursor-mode": GLib.Variant("u", self.cursor_mode)}
        for i, (kind, arg) in enumerate(self.specs):
            if kind == "virtual":
                stream = self.sc_session.call_sync(
                    "RecordVirtual", GLib.Variant("(a{sv})", (props,)),
                    Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
            else:
                stream = self.sc_session.call_sync(
                    "RecordMonitor", GLib.Variant("(sa{sv})", (arg, props)),
                    Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
            self._pending[stream] = i
            self.stream_paths[i] = stream
            self.bus.signal_subscribe(
                SC_NAME, SC_NAME + ".Stream", "PipeWireStreamAdded",
                stream, None, Gio.DBusSignalFlags.NONE, self._on_stream, None)

        self.rd_session.call_sync("Start", None, Gio.DBusCallFlags.NONE,
                                  -1, None)
        self.started = True

    def _on_stream(self, conn, sender, path, iface, signal, params, _ud):
        idx = self._pending.get(path, 0)
        kind, arg = self.specs[idx]
        node = params.unpack()[0]
        if kind == "virtual":
            # these caps DEFINE the monitor's resolution
            caps = "video/x-raw,format=RGBA,width=%d,height=%d" % arg
        else:
            # a mirror's size comes from the output; forcing one rescales it
            caps = "video/x-raw,format=RGBA"
        if self.capture:
            desc = ("pipewiresrc path=%d ! videoconvert ! %s ! "
                    "appsink name=out max-buffers=2 drop=true sync=false"
                    % (node, caps))
        else:
            desc = ("pipewiresrc path=%d ! %s ! fakesink sync=false"
                    % (node, caps))
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        self.pipelines.append(pipe)
        if self.capture:
            self.sinks[idx] = pipe.get_by_name("out")

    def pump(self, seconds=0.0):
        """Let D-Bus signals land. Call until ready() or the monitors never
        appear -- stream creation is asynchronous."""
        ctx = GLib.MainContext.default()
        end = GLib.get_monotonic_time() + seconds * 1e6
        while True:
            while ctx.pending():
                ctx.iteration(False)
            if GLib.get_monotonic_time() >= end:
                return self.ready()

    def ready(self):
        return len(self.pipelines) == len(self.specs)

    def latest(self, index):
        """Newest frame for a monitor as (bytes, w, h), or None."""
        sink = self.sinks[index] if index < len(self.sinks) else None
        if sink is None:
            return None
        sample = sink.try_pull_sample(0)
        if sample is None:
            return None
        caps = sample.get_caps().get_structure(0)
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return (bytes(info.data), caps.get_value("width"),
                    caps.get_value("height"))
        finally:
            buf.unmap(info)

    def blit_into(self, index, texture_glo, w, h):
        """Newest frame straight into a GL texture, skipping the copy that
        `latest()` cannot avoid. Returns (rc, width, height) -- see
        refract.core.fastblit for the codes.

        The caller must have the GL context current, and must be prepared to
        fall back to `latest()`: rc is ERR_INIT when the fast path is not
        built, which is a normal state, not a failure.
        """
        sink = self.sinks[index] if index < len(self.sinks) else None
        if sink is None:
            # A stream whose sink has not been created yet has no frame --
            # which is what latest() reports here too. NOT an error: the
            # caller retires the fast path on errors, and the mirror's sink
            # legitimately appears a moment after the session does (it is
            # built in the PipeWireStreamAdded handler), so reporting a
            # failure here would disable the fast path for the whole run
            # every time the bring-up order went the other way.
            return fastblit.NO_FRAME, 0, 0
        return fastblit.blit(sink, texture_glo, w, h)

    def move_pointer(self, index, x, y):
        """Warp the pointer to (x, y) on one of our streams.

        The RemoteDesktop session that makes the virtual monitors real also
        carries input, so the pointer can be placed on a stream directly --
        which is how the pointer reaches a virtual monitor at all.
        """
        path = self.stream_paths[index] if index < len(self.stream_paths) \
            else None
        if not (self.rd_session and path):
            return False
        self.rd_session.call_sync(
            "NotifyPointerMotionAbsolute",
            # (sdd): the stream is named by STRING here, not object path
            GLib.Variant("(sdd)", (path, float(x), float(y))),
            Gio.DBusCallFlags.NONE, -1, None)
        return True

    def click(self, button=BTN_LEFT):
        """Press and release at the pointer's current position.

        Used to take keyboard focus: GNOME focuses on click, and a
        fullscreen window that never gets clicked never gets the keyboard,
        however visible it is.
        """
        if not self.rd_session:
            return False
        for pressed in (True, False):
            self.rd_session.call_sync(
                "NotifyPointerButton",
                GLib.Variant("(ib)", (int(button), pressed)),
                Gio.DBusCallFlags.NONE, -1, None)
        return True

    def frame_sizes(self):
        """Actual negotiated size per stream, once frames flow. Mirrors do
        not know their size until then."""
        out = []
        for i in range(len(self.specs)):
            got = self.latest(i)
            out.append((got[1], got[2]) if got else None)
        return out

    def stop(self):
        for p in self.pipelines:
            p.set_state(Gst.State.NULL)
        self.pipelines = []
        self.sinks = [None] * len(self.specs)
        if self.rd_session:
            try:
                self.rd_session.call_sync("Stop", None, Gio.DBusCallFlags.NONE,
                                          -1, None)
            except Exception:
                pass
            self.rd_session = None
        self.started = False


class VirtualDisplays(ScreenCapture):
    """N virtual monitors -- the original interface, unchanged.

    Kept as-is because xrdesk.py (the standalone proto-Desk) still uses it
    through the root shim until its port is signed off.
    """

    def __init__(self, sizes, capture=True, cursor=True):
        super().__init__([("virtual", tuple(s)) for s in sizes],
                         capture=capture, cursor=cursor)
