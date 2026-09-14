#!/usr/bin/env python3
"""vdisplay -- virtual monitors in the running Wayland session, with their
pixels.

Genuine compositor outputs, not a simulation. That distinction is the whole
design: the pointer crosses into them, windows maximise on them, and the
clipboard is just the session clipboard. None of that has to be implemented.

Two backends produce the same thing on two different Wayland desktops:

  MutterScreenCapture  GNOME. Mutter's private ScreenCast/RemoteDesktop
                       D-Bus API. One session carries every stream, and a
                       virtual stream's caps DEFINE the monitor's resolution.

  PortalScreenCapture  KDE and anything else with an xdg-desktop-portal that
                       offers the VIRTUAL source type. The portable path;
                       lives in refract.core.vdisplay_portal. It fans out to
                       one portal session per stream, because that is all the
                       KDE portal grants (see that module's header).

`ScreenCapture(...)` picks the backend for the running session, so callers
never name one. Both share the consumer half below -- the GStreamer graph
that turns a PipeWire node into readable frames -- and differ only in how the
session is created, how the pointer is injected, and how it is torn down.

Depends only on the compositor (or its portal), PipeWire and GStreamer. No
XR driver, no desktop extension.

ScreenCapture takes a mixed set of streams: `("virtual", (w, h))` creates a
new monitor, `("monitor", connector)` mirrors an existing output. Refract
Desk needs both -- side monitors that are real extended displays, and a
centre screen mirroring the laptop panel.

The two kinds differ in one important way: a virtual stream's caps DEFINE
the monitor's resolution on GNOME, so width/height are forced there. A
mirror's resolution comes from the output being mirrored, so forcing a size
there would silently rescale it -- let it negotiate and read the size back
off the caps instead. (On the KDE portal the virtual size is fixed by the
compositor and the forced caps are advisory; see vdisplay_portal.)
"""

import os

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


class _CaptureBackend:
    """The consumer half both backends share.

    A backend fills in `self.pipelines` and `self.sinks` (one per spec) from
    whatever session it knows how to build; everything here -- reading the
    newest frame, blitting it, reporting sizes, pumping the main loop -- then
    works the same regardless of which compositor produced the stream.

    specs: list of ("virtual", (w, h)) -- a NEW monitor
                   ("monitor", connector) -- a mirror of an existing output

    With capture=True the pixels are readable; capture=False just
    materialises the monitors (fakesink) -- cheaper when something else is
    doing the drawing.
    """

    def __init__(self, specs, capture=True, cursor=True):
        self.specs = [(kind, arg) for kind, arg in specs]
        self.capture = capture
        self.cursor = bool(cursor)
        self.pipelines = []
        self.sinks = [None] * len(self.specs)
        self.stream_paths = [None] * len(self.specs)
        self.started = False

    @property
    def sizes(self):
        """Declared sizes; None for mirrors, whose size the output decides."""
        return [arg if kind == "virtual" else None
                for kind, arg in self.specs]

    # -- the shared GStreamer graph ---------------------------------------

    @staticmethod
    def _pipeline_desc(node, caps, capture, fd=None):
        """A pipewiresrc -> (appsink | fakesink) description.

        `fd` names a private PipeWire remote (the portal hands one out);
        without it pipewiresrc uses the session bus's default connection,
        which is what Mutter's streams live on.
        """
        src = "pipewiresrc path=%d" % node
        if fd is not None:
            src = "pipewiresrc fd=%d path=%d" % (fd, node)
        if capture:
            return ("%s ! videoconvert ! %s ! appsink name=out "
                    "max-buffers=2 drop=true sync=false" % (src, caps))
        return "%s ! %s ! fakesink sync=false" % (src, caps)

    def _caps_for(self, idx, node):
        """RGBA caps for stream `idx`. A virtual stream's declared size is
        forced (it defines the monitor on GNOME); a mirror negotiates."""
        kind, arg = self.specs[idx]
        if kind == "virtual":
            return "video/x-raw,format=RGBA,width=%d,height=%d" % arg
        return "video/x-raw,format=RGBA"

    def pump(self, seconds=0.0):
        """Let D-Bus / portal signals land. Call until ready() or the
        monitors never appear -- stream creation is asynchronous."""
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
            # built in the stream-added handler), so reporting a failure here
            # would disable the fast path for the whole run every time the
            # bring-up order went the other way.
            return fastblit.NO_FRAME, 0, 0
        return fastblit.blit(sink, texture_glo, w, h)

    def frame_sizes(self):
        """Actual negotiated size per stream, once frames flow. Mirrors do
        not know their size until then."""
        out = []
        for i in range(len(self.specs)):
            got = self.latest(i)
            out.append((got[1], got[2]) if got else None)
        return out

    # -- backend-specific: each subclass implements these -----------------

    def start(self):
        raise NotImplementedError

    def move_pointer(self, index, x, y):
        raise NotImplementedError

    def click(self, button=BTN_LEFT):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class MutterScreenCapture(_CaptureBackend):
    """A mixed set of streams in ONE Mutter session (GNOME).

    Three conditions must ALL hold before Mutter produces a monitor, and
    every failure is silent -- the calls succeed and nothing appears:

      1. The ScreenCast session must be bound to a RemoteDesktop session
         through `remote-desktop-session-id`. A bare ScreenCast session hands
         back valid stream paths and creates no monitors. That binding is
         also what makes pointer input land on the monitor instead of passing
         through it.
      2. Something must CONSUME each PipeWire stream: the negotiated caps
         become the monitor's resolution, so no consumer means no monitor.
      3. Start() goes to the RemoteDesktop session, not the ScreenCast one.
    """

    def __init__(self, specs, capture=True, cursor=True):
        super().__init__(specs, capture=capture, cursor=cursor)
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
        self._pending = {}                      # stream path -> index

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
        node = params.unpack()[0]
        caps = self._caps_for(idx, node)
        desc = self._pipeline_desc(node, caps, self.capture)
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        self.pipelines.append(pipe)
        if self.capture:
            self.sinks[idx] = pipe.get_by_name("out")

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

    def stop(self):
        for p in self.pipelines:
            p.set_state(Gst.State.NULL)
        self.pipelines = []
        self.sinks = [None] * len(self.specs)
        # The GstAppSink* C objects behind those sinks are now freed;
        # fastblit's pointer-validity cache must not outlive them (see
        # fastblit.forget_sinks()).
        fastblit.forget_sinks()
        if self.rd_session:
            try:
                self.rd_session.call_sync("Stop", None, Gio.DBusCallFlags.NONE,
                                          -1, None)
            except Exception:                             # noqa: BLE001
                pass
            self.rd_session = None
        self.started = False


def mutter_available(bus=None):
    """Is Mutter's private ScreenCast API on the session bus?

    This -- not $XDG_CURRENT_DESKTOP -- is the question that actually
    decides the backend: the name is present on GNOME and absent everywhere
    else, and it is exactly the capability the Mutter backend needs. Checks
    both active and activatable names, since Mutter's services are
    D-Bus-activated and may not be running until first touched.
    """
    try:
        bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        dbus = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", None)
        active = dbus.call_sync("ListNames", None, Gio.DBusCallFlags.NONE,
                                -1, None).unpack()[0]
        activatable = dbus.call_sync(
            "ListActivatableNames", None, Gio.DBusCallFlags.NONE,
            -1, None).unpack()[0]
        return SC_NAME in active or SC_NAME in activatable
    except Exception:                                     # noqa: BLE001
        return False


def _backend():
    """The capture backend class for this session.

    Overridable with REFRACT_DISPLAY_BACKEND=mutter|portal, which is how the
    portal path can be exercised on GNOME (it works there too -- GNOME's
    portal offers the VIRTUAL source type) and how a headless test forces a
    known backend.
    """
    override = os.environ.get("REFRACT_DISPLAY_BACKEND", "").strip().lower()
    if override == "mutter":
        return MutterScreenCapture
    if override == "portal":
        from .vdisplay_portal import PortalScreenCapture
        return PortalScreenCapture
    if mutter_available():
        return MutterScreenCapture
    from .vdisplay_portal import PortalScreenCapture
    return PortalScreenCapture


def ScreenCapture(specs, capture=True, cursor=True):
    """Open a capture backed by whichever session is running.

    A factory rather than a class so the one call every caller already makes
    -- `ScreenCapture([...], capture=True)` -- transparently gets the Mutter
    backend on GNOME and the portal backend on KDE, with no call site
    knowing which. Both returned objects share the same public surface
    (start / pump / ready / latest / blit_into / move_pointer / click /
    frame_sizes / stop, plus the `sizes` property).
    """
    return _backend()(specs, capture=capture, cursor=cursor)


class VirtualDisplays(MutterScreenCapture):
    """N virtual monitors -- the original interface, unchanged.

    Kept as-is because xrdesk.py (the standalone proto-Desk) still uses it
    through the root shim until its port is signed off. GNOME-only, like the
    proto it serves; new code goes through ScreenCapture() instead.
    """

    def __init__(self, sizes, capture=True, cursor=True):
        super().__init__([("virtual", tuple(s)) for s in sizes],
                         capture=capture, cursor=cursor)
