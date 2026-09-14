#!/usr/bin/env python3
"""Probe the XDG desktop portal for virtual-monitor support -- DATA ONLY.

    python3 tools/portal-probe.py [--spec virtual:1280x720,monitor]
                                  [--token-file PATH] [--wait 300]

refract.core.vdisplay drives Mutter's PRIVATE ScreenCast/RemoteDesktop D-Bus
API, which only exists on GNOME. The portable equivalent is the standard
portal pair (org.freedesktop.portal.RemoteDesktop + ScreenCast): since
ScreenCast v4 the portal defines a VIRTUAL source type, and KDE's portal
advertises it (AvailableSourceTypes bit 4). Whether the portal path can
actually replace the Mutter one hinges on questions the spec does not
answer, so this tool asks the running compositor instead:

  1. Can ONE session carry several virtual streams plus a mirror of a real
     output? (Desk needs exactly that: two side monitors and a centre
     mirror, with one lifetime and one teardown.)
  2. Do the negotiated PipeWire caps DEFINE a virtual monitor's resolution,
     as they do against Mutter directly -- or does the compositor pick a
     size of its own?
  3. Does a restore token make the second run silent? The portal fronts
     everything with a consent dialog; Desk cannot be popping one on every
     start. (persist_mode=2 asks for "until revoked".)
  4. Does NotifyPointerMotionAbsolute accept our stream and land without an
     error? (Full verification needs eyes on a screen; no-error is the
     most a probe can check.)

The first run BLOCKS on the consent dialog -- that is expected, not a hang.
The dialog appears on the machine's physical screen and must be accepted
there once; the restore token saved via --token-file covers every run after
that. What the dialog offers is itself a finding: whether a "virtual
monitor" entry exists at all, and whether it can be added more than once,
differs per portal backend and version.

This tool only OBSERVES. It creates nothing that outlives it: the portal
session is closed on exit (also on Ctrl-C), which is defined to destroy
whatever monitors it materialised. Run it from any terminal in (or pointed
at) the target session -- the compositor answers over D-Bus either way.

Output: findings printed as they land, plus a JSON summary on stdout at the
end for the record.
"""

import argparse
import json
import os
import random
import string
import subprocess
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gio, GLib, Gst, GstApp    # noqa: E402,F401

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
REQ_IFACE = "org.freedesktop.portal.Request"
SESS_IFACE = "org.freedesktop.portal.Session"

# ScreenCast source types (bitmask)
SRC_MONITOR, SRC_WINDOW, SRC_VIRTUAL = 1, 2, 4
# Cursor modes (bitmask -- NOT Mutter's 0/1/2 enum)
CURSOR_HIDDEN, CURSOR_EMBEDDED, CURSOR_METADATA = 1, 2, 4
# RemoteDesktop device types (bitmask)
DEV_KEYBOARD, DEV_POINTER = 1, 2
# persist_mode: 0 none, 1 while the app runs, 2 until revoked
PERSIST_UNTIL_REVOKED = 2


def say(msg):
    print("  %s" % msg, flush=True)


def _token():
    return "refract_" + "".join(random.choices(string.ascii_lowercase, k=8))


class Portal:
    """The portal's request/response dance, synchronously.

    Every portal method returns immediately with a Request object path and
    delivers its real result later as a Response signal on that path. The
    path is predictable from our unique bus name and the handle_token we
    pass, so the subscription can be set up BEFORE the call -- the response
    can otherwise land in the gap and be lost.
    """

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")

    def _proxy(self, iface):
        return Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, iface, None)

    def request(self, iface, method, args, options, wait=30.0,
                fd_list=None):
        """Call a portal method; block for its Response.

        Returns (response_code, results_dict): 0 success, 1 the user
        cancelled, 2 something else went wrong.
        """
        token = _token()
        options = dict(options, handle_token=GLib.Variant("s", token))
        req_path = "/org/freedesktop/portal/desktop/request/%s/%s" % (
            self.sender, token)

        got = {}
        loop = GLib.MainLoop()

        def on_response(conn, sender, path, i, signal, params):
            got["code"], got["results"] = params.unpack()
            loop.quit()

        sub = self.bus.signal_subscribe(
            PORTAL_NAME, REQ_IFACE, "Response", req_path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        try:
            proxy = self._proxy(iface)
            proxy.call_sync(method,
                            GLib.Variant(*args(options)),
                            Gio.DBusCallFlags.NONE, -1, None)

            def on_timeout():
                loop.quit()
                return False
            timeout = GLib.timeout_add(int(wait * 1000), on_timeout)
            loop.run()
            if "code" in got:
                GLib.source_remove(timeout)
        finally:
            self.bus.signal_unsubscribe(sub)
        if "code" not in got:
            return None, {}                     # timed out
        return got["code"], got["results"]

    def open_pipewire_fd(self, session):
        """A private PipeWire connection carrying this session's streams."""
        proxy = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, SC_IFACE, None)
        res, fds = proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})),
            Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(res.unpack()[0])

    def close_session(self, session):
        Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, session, SESS_IFACE, None).call_sync(
            "Close", None, Gio.DBusCallFlags.NONE, -1, None)


def outputs_snapshot():
    """{name: (w, h)} of enabled outputs, per kscreen-doctor; None where the
    tool is missing (non-KDE), in which case the diff findings are skipped."""
    try:
        raw = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                             text=True, timeout=10).stdout
        doc = json.loads(raw)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    out = {}
    for o in doc.get("outputs", []):
        if not o.get("enabled"):
            continue
        mode = next((m for m in o.get("modes", [])
                     if m["id"] == o.get("currentModeId")), None)
        size = ((mode["size"]["width"], mode["size"]["height"])
                if mode else (0, 0))
        out[o["name"]] = size
    return out


def main():
    ap = argparse.ArgumentParser(
        description="probe the portal for virtual-monitor support")
    ap.add_argument("--spec", default="virtual:1280x720,virtual:1600x900,"
                    "monitor",
                    help="comma list of virtual:WxH and monitor entries "
                    "(default asks for two virtuals of DIFFERENT sizes -- "
                    "that difference is how caps-defined sizing is proven "
                    "-- plus one mirror)")
    ap.add_argument("--token-file",
                    default=os.path.expanduser(
                        "~/.cache/refract-portal-probe.token"),
                    help="where the restore token is kept between runs")
    ap.add_argument("--wait", type=float, default=300.0,
                    help="seconds to wait on the consent dialog")
    ap.add_argument("--hold", type=float, default=8.0,
                    help="seconds to hold the session open once running")
    ap.add_argument("--no-input", action="store_true",
                    help="pure ScreenCast session, no RemoteDesktop: some "
                    "portals treat virtual sources differently with and "
                    "without input attached, so both shapes need probing")
    args = ap.parse_args()

    specs = []
    for item in args.spec.split(","):
        if item == "monitor":
            specs.append(("monitor", None))
        elif item == "virtual":
            specs.append(("virtual", None))     # let the compositor choose
        elif item.startswith("virtual:"):
            w, h = item.split(":", 1)[1].split("x")
            specs.append(("virtual", (int(w), int(h))))
        else:
            ap.error("bad spec entry: %r" % item)
    n_virtual = sum(1 for kind, _ in specs if kind == "virtual")
    want_monitor = any(kind == "monitor" for kind, _ in specs)

    findings = {"spec": args.spec}
    Gst.init(None)
    portal = Portal()

    sc_ver = portal._proxy(SC_IFACE).get_cached_property("version")
    rd_ver = portal._proxy(RD_IFACE).get_cached_property("version")
    types = portal._proxy(SC_IFACE).get_cached_property(
        "AvailableSourceTypes")
    findings["versions"] = {"screencast": sc_ver.unpack() if sc_ver else None,
                            "remotedesktop": rd_ver.unpack() if rd_ver
                            else None}
    findings["source_types"] = types.unpack() if types else 0
    say("portal: ScreenCast v%s, RemoteDesktop v%s, source types 0x%x"
        % (findings["versions"]["screencast"],
           findings["versions"]["remotedesktop"], findings["source_types"]))
    if not findings["source_types"] & SRC_VIRTUAL:
        say("VIRTUAL source type not offered -- the portal path is out")
        print(json.dumps(findings, indent=2))
        return 1

    restore_token = None
    try:
        with open(args.token_file) as f:
            restore_token = f.read().strip() or None
    except OSError:
        pass
    findings["had_restore_token"] = bool(restore_token)

    before = outputs_snapshot()

    # ---- session setup: RemoteDesktop owns it, ScreenCast rides along
    # (or, with --no-input, a bare ScreenCast session) ----
    owner = SC_IFACE if args.no_input else RD_IFACE
    findings["session_owner"] = owner
    code, res = portal.request(
        owner, "CreateSession", lambda o: (
            "(a{sv})", (dict(o, session_handle_token=GLib.Variant(
                "s", _token())),)), {})
    if code != 0:
        say("CreateSession failed (code %s)" % code)
        return 1
    session = res["session_handle"]

    # persist/restore ride on SelectDevices when RemoteDesktop owns the
    # session, on SelectSources when ScreenCast does
    persist_opts = {"persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED)}
    if restore_token:
        persist_opts["restore_token"] = GLib.Variant("s", restore_token)

    if not args.no_input:
        dev_opts = dict(persist_opts, types=GLib.Variant("u", DEV_POINTER))
        code, _ = portal.request(
            RD_IFACE, "SelectDevices",
            lambda o: ("(oa{sv})", (session, dict(o, **dev_opts))), {})
        say("SelectDevices: code %s" % code)
        findings["select_devices"] = code

    src_types = SRC_VIRTUAL | (SRC_MONITOR if want_monitor else 0)
    src_opts = {"types": GLib.Variant("u", src_types),
                "multiple": GLib.Variant("b", True),
                "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED)}
    if args.no_input:
        src_opts.update(persist_opts)
    code, _ = portal.request(
        SC_IFACE, "SelectSources",
        lambda o: ("(oa{sv})", (session, dict(o, **src_opts))), {})
    say("SelectSources (types 0x%x, multiple): code %s" % (src_types, code))
    findings["select_sources"] = code
    if code != 0:
        portal.close_session(session)
        return 1

    if not restore_token:
        say("no restore token yet -- the consent dialog is now up on the")
        say("MACHINE'S OWN SCREEN and this will wait %ds for it" % args.wait)
    code, res = portal.request(
        owner, "Start",
        lambda o: ("(osa{sv})", (session, "", o)), {}, wait=args.wait)
    if code is None:
        say("Start: no response in %ds -- dialog not answered" % args.wait)
        findings["start"] = "timeout"
        print(json.dumps(findings, indent=2))
        portal.close_session(session)
        return 2
    say("Start: code %s" % code)
    findings["start"] = code
    if code != 0:
        portal.close_session(session)
        print(json.dumps(findings, indent=2))
        return 1

    token = res.get("restore_token")
    findings["got_restore_token"] = bool(token)
    if token:
        with open(args.token_file, "w") as f:
            f.write(token)
        say("restore token saved -> %s" % args.token_file)

    streams = res.get("streams", [])
    findings["streams"] = [
        {"node": node, **{k: v for k, v in props.items()}}
        for node, props in streams]
    say("streams granted: %d (wanted %d virtual + %s mirror)"
        % (len(streams), n_virtual, "1" if want_monitor else "no"))
    for node, props in streams:
        say("  node %d: %s" % (node, props))

    # ---- consume: virtual caps forced (that IS the probe), mirror free ----
    fd = portal.open_pipewire_fd(session)
    virt_sizes = [s for kind, s in specs if kind == "virtual"]
    pipes, sinks = [], []
    for i, (node, props) in enumerate(streams):
        is_virtual = props.get("source_type", 0) == SRC_VIRTUAL
        size = virt_sizes.pop(0) if is_virtual and virt_sizes else None
        if size:
            caps = "video/x-raw,format=RGBA,width=%d,height=%d" % size
        else:
            caps = "video/x-raw,format=RGBA"
        desc = ("pipewiresrc fd=%d path=%d ! videoconvert ! %s ! "
                "appsink name=out max-buffers=2 drop=true sync=false"
                % (os.dup(fd), node, caps))
        pipe = Gst.parse_launch(desc)
        pipe.set_state(Gst.State.PLAYING)
        pipes.append(pipe)
        sinks.append((node, props, pipe.get_by_name("out")))

    say("pipelines playing; holding %.0fs for negotiation and the "
        "compositor" % args.hold)
    end = time.time() + args.hold
    ctx = GLib.MainContext.default()
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.05)

    findings["negotiated"] = []
    for node, props, sink in sinks:
        sample = sink.try_pull_sample(Gst.SECOND)
        if sample is None:
            say("node %d: NO frames" % node)
            findings["negotiated"].append({"node": node, "frames": False})
            continue
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        say("node %d: frames at %dx%d" % (node, w, h))
        findings["negotiated"].append(
            {"node": node, "frames": True, "size": [w, h]})

    after = outputs_snapshot()
    if before is not None and after is not None:
        new = {k: v for k, v in after.items() if k not in before}
        say("new compositor outputs: %s" % (new or "none"))
        findings["new_outputs"] = {k: list(v) for k, v in new.items()}
        if new:
            # while it exists, record what the compositor would let display
            # config do to it -- if it has more modes (or takes custom
            # ones), sizes the portal cannot ask for can still be SET
            try:
                doc = json.loads(subprocess.run(
                    ["kscreen-doctor", "-j"], capture_output=True,
                    text=True, timeout=10).stdout)
                findings["new_output_details"] = [
                    o for o in doc.get("outputs", []) if o["name"] in new]
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass

    # ---- pointer: does the injection path accept our stream at all? ----
    if not args.no_input and streams:
        rd = Gio.DBusProxy.new_sync(
            portal.bus, Gio.DBusProxyFlags.NONE, None,
            PORTAL_NAME, PORTAL_PATH, RD_IFACE, None)
        try:
            node = streams[0][0]
            rd.call_sync(
                "NotifyPointerMotionAbsolute",
                GLib.Variant("(oa{sv}udd)", (session, {}, node, 10.0, 10.0)),
                Gio.DBusCallFlags.NONE, -1, None)
            say("NotifyPointerMotionAbsolute: accepted")
            findings["pointer_motion"] = "accepted"
        except GLib.Error as e:
            say("NotifyPointerMotionAbsolute: REJECTED (%s)" % e.message)
            findings["pointer_motion"] = "rejected: %s" % e.message

    # ---- teardown must take the monitors with it ----
    for pipe in pipes:
        pipe.set_state(Gst.State.NULL)
    portal.close_session(session)
    time.sleep(2.0)
    final = outputs_snapshot()
    if after is not None and final is not None:
        leftover = {k: v for k, v in final.items()
                    if k not in (before or {})}
        say("outputs surviving session close: %s" % (leftover or "none"))
        findings["leftover_outputs"] = sorted(leftover)

    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
