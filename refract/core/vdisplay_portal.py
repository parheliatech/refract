"""vdisplay_portal -- the desktop-agnostic capture backend.

The portable counterpart to the Mutter backend in refract.core.vdisplay: it
drives the standard xdg-desktop-portal (org.freedesktop.portal.RemoteDesktop
+ ScreenCast) instead of GNOME's private Mutter API, so it works on KDE
Plasma and any other Wayland desktop whose portal offers the VIRTUAL source
type. It works on GNOME too -- GNOME's portal offers VIRTUAL -- but the
Mutter backend is preferred there because it is faster and can size a
virtual monitor freely.

Probed live against xdg-desktop-portal-kde (Plasma 6.6) -- see
tools/portal-probe.py, which is the tool these decisions came out of:

  * ONE source per session. KDE's portal grants a single stream per Start(),
    so a Desk topology of two virtual monitors plus a mirror cannot be one
    session as it is on Mutter. This backend therefore FANS OUT: one portal
    session per spec, held concurrently, presented through the same
    index-based API. Each session is RemoteDesktop-owned so the pointer can
    be injected onto any of them.

  * The virtual monitor's SIZE is the compositor's to choose (KWin makes a
    1920x1080 output); the caps we set are advisory. Refract Desk runs its
    virtual monitors at 1080p anyway, so this costs nothing in practice.

  * The MIRROR target is the portal's to choose, not ours: there is no
    RecordMonitor(connector) equivalent, the portal asks the user. First run
    shows a picker per session; a persisted restore token (persist_mode "until
    revoked") makes every run after that silent and pins the same choice.

  * Frames arrive over a private PipeWire remote opened with
    OpenPipeWireRemote; the GStreamer graph is otherwise identical to the
    Mutter path (shared in _CaptureBackend).

The consent dialogs are the one visible difference from GNOME on first run.
After tokens are minted the sessions come up without interaction.
"""

import os
import random
import string

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst        # noqa: E402

from . import config                            # noqa: E402
from . import fastblit                          # noqa: E402
from .vdisplay import BTN_LEFT, _CaptureBackend  # noqa: E402

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
REQ_IFACE = "org.freedesktop.portal.Request"
SESS_IFACE = "org.freedesktop.portal.Session"

# ScreenCast source types (bitmask, NOT an enum)
SRC_MONITOR, SRC_WINDOW, SRC_VIRTUAL = 1, 2, 4
# CursorMode is ALSO a bitmask here (1 HIDDEN, 2 EMBEDDED, 4 METADATA) --
# unlike Mutter's 0/1/2 enum. EMBEDDED for the same reason as on Mutter: the
# pointer has to be in the pixels to survive being re-rendered in 3D.
CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 1, 2, 4
# RemoteDesktop device types (bitmask)
DEV_POINTER = 2
# persist_mode: 0 none, 1 while the app runs, 2 until the user revokes it
PERSIST_UNTIL_REVOKED = 2


def source_available():
    """Does this session's portal offer the VIRTUAL source type at all?
    Without it there is nothing this backend can do."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sc = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, SC_IFACE, None)
        types = sc.get_cached_property("AvailableSourceTypes")
        return bool(types and types.unpack() & SRC_VIRTUAL)
    except Exception:                                     # noqa: BLE001
        return False


def _token():
    return "refract_" + "".join(random.choices(string.ascii_lowercase, k=10))


def _tokens_load():
    cfg = config.load()
    return cfg, cfg.setdefault("global", {}).setdefault("portal_tokens", {})


def _token_save(key, value):
    """Persist one restore token. Loads and rewrites the store each time --
    tokens are minted one per session at bring-up, rarely, so the cost of a
    read-modify-write per token is irrelevant and it keeps concurrent
    sessions from clobbering each other's freshly saved tokens."""
    cfg, store = _tokens_load()
    store[key] = value
    config.save(cfg)


class _Session:
    """One portal session carrying exactly one stream.

    KDE grants a single source per Start(), so each Refract stream gets its
    own session here. A session is RemoteDesktop-owned (so the pointer can be
    injected onto its stream) and persists its consent behind a restore token
    keyed by `role` -- a stable string, so the same monitor/virtual is
    re-granted silently on later runs.
    """

    def __init__(self, bus, spec, role, capture, cursor):
        self.bus = bus
        self.kind, self.arg = spec
        self.role = role
        self.capture = capture
        self.cursor = cursor
        self.session = None         # portal session object path
        self.node = None            # PipeWire node id of the granted stream
        self.size = None            # (w, h) the portal reports for the stream
        self.pipeline = None
        self.sink = None
        self.sender = bus.get_unique_name()[1:].replace(".", "_")

    # -- the request/response dance ---------------------------------------

    def _request(self, iface, method, body, options, wait):
        """Call a portal method and block for its Response signal.

        The Request object path is predictable from our unique name and the
        handle_token, so the subscription is in place before the call --
        otherwise the response can land in the gap and be lost. Returns
        (code, results): 0 success, 1 the user cancelled, 2 otherwise.
        """
        tok = _token()
        options = dict(options, handle_token=GLib.Variant("s", tok))
        req_path = "/org/freedesktop/portal/desktop/request/%s/%s" % (
            self.sender, tok)
        got = {}
        loop = GLib.MainLoop()

        def on_response(conn, sender, path, i, sig, params):
            got["code"], got["results"] = params.unpack()
            loop.quit()

        sub = self.bus.signal_subscribe(
            PORTAL_NAME, REQ_IFACE, "Response", req_path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        try:
            proxy = Gio.DBusProxy.new_sync(
                self.bus, Gio.DBusProxyFlags.NONE, None,
                PORTAL_NAME, PORTAL_PATH, iface, None)
            proxy.call_sync(method, GLib.Variant(*body(options)),
                            Gio.DBusCallFlags.NONE, -1, None)

            def on_timeout():
                loop.quit()
                return False
            timer = GLib.timeout_add(int(wait * 1000), on_timeout)
            loop.run()
            if "code" in got:
                GLib.source_remove(timer)
        finally:
            self.bus.signal_unsubscribe(sub)
        if "code" not in got:
            return None, {}                     # timed out
        return got["code"], got["results"]

    def open(self, wait):
        """Create the session, select its source, and Start() it. On success
        the stream's node id and size are on self; raises on failure.

        `wait` bounds only the first, interactive Start() -- the consent
        dialog. Once a restore token exists the portal answers immediately.
        """
        cfg, store = _tokens_load()
        restore = store.get(self.role)

        # A virtual monitor needs pointer injection, so its session is
        # RemoteDesktop-owned; a mirror is display-only. That distinction is
        # not just tidiness on KWin: a RemoteDesktop-bound capture is scoped
        # to the whole logical desktop, so asking it to mirror one monitor
        # hands back the entire workspace bounding box instead (measured:
        # a 3440x1440 output came through as the full multi-monitor span).
        # A pure ScreenCast session mirrors exactly the one output picked.
        wants_input = self.kind == "virtual"
        owner = RD_IFACE if wants_input else SC_IFACE

        code, res = self._request(
            owner, "CreateSession",
            lambda o: ("(a{sv})", (dict(
                o, session_handle_token=GLib.Variant("s", _token())),)),
            {}, wait=30)
        if code != 0:
            raise RuntimeError("portal CreateSession failed (code %s)" % code)
        self.session = res["session_handle"]

        # persist/restore rides on SelectDevices when RemoteDesktop owns the
        # session, on SelectSources when ScreenCast does.
        persist = {"persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED)}
        if restore:
            persist["restore_token"] = GLib.Variant("s", restore)

        if wants_input:
            dev_opts = dict(persist, types=GLib.Variant("u", DEV_POINTER))
            code, _ = self._request(
                RD_IFACE, "SelectDevices",
                lambda o: ("(oa{sv})", (self.session, dict(o, **dev_opts))),
                {}, wait=30)
            if code != 0:
                raise RuntimeError(
                    "portal SelectDevices failed (code %s)" % code)

        src_type = SRC_VIRTUAL if self.kind == "virtual" else SRC_MONITOR
        cursor_mode = CURSOR_EMBEDDED if self.cursor else CURSOR_HIDDEN
        src_opts = {"types": GLib.Variant("u", src_type),
                    "cursor_mode": GLib.Variant("u", cursor_mode)}
        if not wants_input:
            src_opts.update(persist)
        code, _ = self._request(
            SC_IFACE, "SelectSources",
            lambda o: ("(oa{sv})", (self.session, dict(o, **src_opts))),
            {}, wait=30)
        if code != 0:
            raise RuntimeError("portal SelectSources failed (code %s)" % code)

        code, res = self._request(
            owner, "Start",
            lambda o: ("(osa{sv})", (self.session, "", o)), {}, wait=wait)
        if code is None:
            raise RuntimeError("portal Start timed out -- the consent dialog "
                               "for '%s' went unanswered" % self.role)
        if code != 0:
            raise RuntimeError("portal Start failed (code %s) for '%s'"
                               % (code, self.role))

        token = res.get("restore_token")
        if token and token != restore:
            _token_save(self.role, token)

        streams = res.get("streams", [])
        if not streams:
            raise RuntimeError("portal granted no stream for '%s'" % self.role)
        self.node, props = streams[0]
        self.size = tuple(props["size"]) if "size" in props else None

    def build_pipeline(self, fd, caps_for):
        """Consume the stream over the portal's PipeWire remote `fd`."""
        caps = caps_for(self.size)
        # each pipewiresrc needs its own dup of the fd -- GStreamer closes it
        desc = _CaptureBackend._pipeline_desc(self.node, caps, self.capture,
                                              fd=os.dup(fd))
        self.pipeline = Gst.parse_launch(desc)
        self.pipeline.set_state(Gst.State.PLAYING)
        if self.capture:
            self.sink = self.pipeline.get_by_name("out")

    def open_pipewire_fd(self):
        sc = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, SC_IFACE, None)
        res, fds = sc.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})),
            Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(res.unpack()[0])

    def move_pointer(self, x, y):
        rd = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, RD_IFACE, None)
        rd.call_sync(
            "NotifyPointerMotionAbsolute",
            GLib.Variant("(oa{sv}udd)", (self.session, {}, self.node,
                                         float(x), float(y))),
            Gio.DBusCallFlags.NONE, -1, None)

    def click(self, button):
        rd = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, RD_IFACE, None)
        for pressed in (True, False):
            rd.call_sync(
                "NotifyPointerButton",
                GLib.Variant("(oa{sv}ib)", (self.session, {}, int(button),
                                            pressed)),
                Gio.DBusCallFlags.NONE, -1, None)

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.sink = None
        if self.session:
            try:
                Gio.DBusProxy.new_sync(
                    self.bus, Gio.DBusProxyFlags.NONE, None,
                    PORTAL_NAME, self.session, SESS_IFACE, None).call_sync(
                    "Close", None, Gio.DBusCallFlags.NONE, -1, None)
            except Exception:                             # noqa: BLE001
                pass
            self.session = None


def _role_for(kind, arg, ordinal):
    """A stable restore-token key for one spec.

    Stable across runs so the same consent is reused: a mirror keys off the
    connector it stands for (two capture instances asking to mirror the same
    output legitimately share the user's one choice), a virtual off its
    declared size plus its position among the virtuals in its instance (they
    are otherwise identical). The key is per physical target, never per
    session object, so it survives teardown and restart.
    """
    if kind == "virtual":
        w, h = arg if arg else (0, 0)
        return "virtual:%dx%d#%d" % (w, h, ordinal)
    return "monitor:%s" % (arg,)


class PortalScreenCapture(_CaptureBackend):
    """The Desk topology as a fan-out of one-stream portal sessions.

    Public surface identical to MutterScreenCapture. `start()` runs the
    portal negotiation synchronously (it is inherently request/response);
    once tokens exist this is a handful of fast round-trips, and on the very
    first run it blocks on the consent dialog -- the one interactive moment.
    After start() the pipelines and sinks are already in place, so pump() and
    ready() behave exactly as the Mutter backend's callers expect.
    """

    # how long start() waits on a first-run consent dialog before giving up
    CONSENT_WAIT = 120.0

    def __init__(self, specs, capture=True, cursor=True):
        super().__init__(specs, capture=capture, cursor=cursor)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sessions = []
        self._last_pointer = 0

    def start(self):
        if not Gst.is_initialized():
            Gst.init(None)
        ordinals = {}
        for idx, (kind, arg) in enumerate(self.specs):
            ordinal = ordinals.get(kind, 0)
            ordinals[kind] = ordinal + 1
            role = _role_for(kind, arg, ordinal)
            sess = _Session(self.bus, (kind, arg), role, self.capture,
                            self.cursor)
            sess.open(wait=self.CONSENT_WAIT)
            fd = sess.open_pipewire_fd()
            sess.build_pipeline(
                fd, lambda size, i=idx: self._caps_for_portal(i, size))
            os.close(fd)
            self._sessions.append(sess)
            self.pipelines.append(sess.pipeline)
            self.stream_paths[idx] = sess.session
            if self.capture:
                self.sinks[idx] = sess.sink
        self.started = True

    def _caps_for_portal(self, idx, size):
        """RGBA, but never a forced width/height.

        The Mutter path forces a virtual stream's size because there the caps
        DEFINE the monitor. Through the portal the compositor has already
        fixed the size (KWin makes a 1080p output; our request was advisory),
        so pinning width/height here only gives videoconvert -- which does not
        rescale -- a constraint it cannot meet whenever the negotiated size
        differs by a pixel, and the pipeline fails to link. Let it negotiate;
        latest()/frame_sizes() read the real size back off the caps."""
        return "video/x-raw,format=RGBA"

    def move_pointer(self, index, x, y):
        if index >= len(self._sessions):
            return False
        try:
            self._sessions[index].move_pointer(x, y)
            self._last_pointer = index
            return True
        except Exception:                                 # noqa: BLE001
            return False

    def click(self, button=BTN_LEFT):
        # click where the pointer was last placed -- grab_focus() warps then
        # clicks, and the button event has to reach the same session's stream
        if not self._sessions:
            return False
        idx = self._last_pointer if self._last_pointer < len(self._sessions) \
            else 0
        try:
            self._sessions[idx].click(button)
            return True
        except Exception:                                 # noqa: BLE001
            return False

    def stop(self):
        for sess in self._sessions:
            sess.stop()
        self._sessions = []
        self.pipelines = []
        self.sinks = [None] * len(self.specs)
        # the GstAppSink* C objects are freed with the pipelines above;
        # fastblit's pointer cache must not outlive them
        fastblit.forget_sinks()
        self.started = False
