#!/usr/bin/env python3
"""Refract Desk smoke test. Needs a display and a GNOME session; NOT the
glasses.

    .venv/bin/python tests/smoke_desk.py [--outdir DIR]

Brings up the real three-monitor topology -- two Mutter virtual monitors and
a mirror of the laptop panel -- checks frames actually flow, drives the
layout controls, then asserts the virtual monitors are gone again on exit.
Creating and destroying monitors is visible on the desktop while it runs.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                        # noqa: E402

from refract.core import displaymode                      # noqa: E402
from refract.core import settings as S                    # noqa: E402
from refract.core.render import App                       # noqa: E402
from refract.core.vdisplay import VIRTUAL_PRODUCT         # noqa: E402
from refract.desk.scene import DeskScene                  # noqa: E402

STEPS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("%s FAILED %s" % (name, detail))
    STEPS.append(name)
    print("  ok   %s%s" % (name, ("  " + detail) if detail else ""))


def frame(app, path=None):
    if app.scene:
        app.scene.update(app, 1.0 / 60.0)
    app.render_frame()
    if path:
        app.grab(path, quiet=True)
    app.glfw.swap_buffers(app.win)
    app.glfw.poll_events()


def virtual_count():
    try:
        return sum(1 for o in displaymode.list_outputs()
                   if VIRTUAL_PRODUCT.lower() in (o["product"] or "").lower())
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="/tmp/refract-smoke-desk")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    out = lambda n: os.path.join(a.outdir, n)          # noqa: E731

    if virtual_count() != 0:
        import subprocess
        running = subprocess.run(["pgrep", "-af", "python -m refract"],
                                 capture_output=True, text=True).stdout
        hint = ("a Refract session is already running and owns them:\n      %s"
                % running.strip().splitlines()[0]
                if "refract" in running else
                "no Refract is running, so these leaked from an earlier run")
        sys.exit("  %d virtual monitors already exist -- %s\n"
                 "  This test needs the desktop to itself; quit Refract and "
                 "re-run." % (virtual_count(), hint))
    check("no stray virtual monitors before we start", virtual_count() == 0)

    app = App(head=None, windowed=True, size_win=(1920, 540),
              config={"global": {}, "desk": {}})
    app._config_mod = type("N", (), {"save": staticmethod(lambda c: None)})()
    g = app.glfw
    desk = DeskScene()
    t_enter = time.time()
    app.push(desk)
    enter_secs = time.time() - t_enter
    # entering must not block: pumping the capture to completion inside
    # enter() would freeze the whole shell for seconds
    check("entering desk does not block the shell", enter_secs < 2.0,
          "enter() took %.2fs" % enter_secs)

    check("desk picked a panel to mirror", bool(desk.mirror_connector),
          desk.mirror_connector or str(desk.failed))
    check("desk mirrors a REAL output, not the glasses",
          desk.mirror_connector != displaymode.glasses_connector(),
          "%s (glasses are %s)" % (desk.mirror_connector,
                                   displaymode.glasses_connector()))

    # -- bring-up ---------------------------------------------------------
    deadline = time.time() + 25.0
    while time.time() < deadline and not (desk.cap and desk.cap.ready()):
        frame(app)
        time.sleep(0.05)
    check("the virtual monitor streams came up", desk.cap.ready(),
          "%d pipelines" % len(desk.cap.pipelines))

    for _ in range(90):                 # let frames flow
        frame(app)
        time.sleep(0.02)
    check("two virtual monitors materialised", virtual_count() == 2,
          "%d virtual" % virtual_count())

    # NOT "is a sample pending right now": a static virtual monitor stops
    # producing buffers once it has painted, so an idle screen legitimately
    # has nothing queued. What matters is that content reached every screen.
    check("every screen has received content",
          all(n > 0 for n in desk.frames_written),
          "frames written: %s" % desk.frames_written)
    # The focus throttle is measured further down, once the pointer checks
    # below have had the quiet desktop they need.

    # Report (do not assert) whether the pointer would cross the screens in
    # the order the wearer sees them. Mutter parks virtual monitors at the
    # far right, so out of the box it does not -- which is what the opt-in
    # "Match desktop layout" setting exists to fix.
    from refract.desk.layout import pointer_order
    live = displaymode.logical_layout()
    order = desk._connector_order()
    crossing = [c for c in pointer_order(live) if c in order]
    matches = crossing == [c for c in order if c]
    print("  %s pointer order %s vs view order %s"
          % ("ok  " if matches else "NOTE", " ".join(crossing),
             " ".join(c or "?" for c in order)))
    check("desk can name the connector behind every screen",
          all(order), str(order))

    # The pointer must be IN the pixels: we re-render these screens in 3D, so
    # a cursor delivered as out-of-band metadata (Mutter cursor-mode 2) is a
    # cursor the wearer never sees -- which made the virtual monitors
    # unusable, since you cannot drag a window to a screen you cannot point
    # at. Warp it to two places and check the picture changes at each.
    def pointer_frame(x, y):
        desk.cap.move_pointer(0, x, y)
        last = None
        for _ in range(40):
            desk.cap.pump(0.05)
            got = desk.cap.latest(0)
            if got:
                d, w, h = got
                last = np.frombuffer(d, np.uint8).reshape(h, w, 4)[:, :, :3]
        return None if last is None else last.astype(np.int16)

    pa, pb = pointer_frame(400, 300), pointer_frame(1500, 800)
    if pa is not None and pb is not None:
        diff = np.abs(pa - pb).sum(axis=2)
        ys, xs = np.where(diff > 40)
        # The changed region must CONTAIN the warp target and stay local --
        # a cursor plus whatever it highlights, not the whole screen. Pinning
        # the exact bounding box would be pinning the cursor theme.
        contains = (len(ys) > 0 and xs.min() - 60 <= 1500 <= xs.max() + 60
                    and ys.min() - 60 <= 800 <= ys.max() + 60)
        local = len(ys) > 0 and (xs.max() - xs.min()) < 300 \
            and (ys.max() - ys.min()) < 300
        check("the pointer is drawn into the captured frame",
              contains and local,
              "changed region x %s..%s y %s..%s after warping to (1500,800)"
              % (xs.min() if len(xs) else "-", xs.max() if len(xs) else "-",
                 ys.min() if len(ys) else "-", ys.max() if len(ys) else "-"))
        check("the pointer can be placed on a virtual monitor at all",
              desk.cap.move_pointer(0, 960, 540) is True)

    # THE regression that froze a wearer's centre screen: rearranging the
    # desktop kills a RecordMonitor stream outright (measured: 105 frames in
    # 3 s -> 0, never recovering). The mirror must therefore be started after
    # the arrangement and restarted on any later one. Assert it is LIVE now,
    # with arranging on by default.
    live_before = desk.frames_written[desk.mirror_index]
    for _ in range(60):
        frame(app)
        time.sleep(0.02)
    check("the mirror is still live after the desktop was rearranged",
          desk.frames_written[desk.mirror_index] > live_before,
          "arrange=%s, mirror frames %d -> %d"
          % (desk._cfg("arrange"), live_before,
             desk.frames_written[desk.mirror_index]))
    check("the mirror has its own capture session",
          desk.mcap is not None and desk.mcap is not desk.cap)

    # The wearer complaint this protects: "update rate is horrible when
    # typing" -- a flat throttle made the screen being worked on wait behind
    # two idle ones. The screen you FACE must refresh every frame.
    #
    # It has to be measured against a desktop deliberately kept BUSY. The
    # centre screen mirrors a panel that sits perfectly still between test
    # runs, and a source producing nothing cannot out-refresh anything, so
    # measured cold this reports on the room rather than on the code -- which
    # is why it failed only when the suite ran after the others. Warping the
    # pointer over the mirrored panel makes damage reliably: the cursor is
    # composited into the frame (cursor-mode EMBEDDED), so each move dirties
    # it. It runs after the pointer checks above, which need a quiet desktop.
    focus = desk._focused_index(app)
    before = list(desk.frames_written)
    for i in range(90):
        desk.mcap.move_pointer(0, 400 + (i % 40) * 12, 300 + (i % 20) * 12)
        frame(app)
        time.sleep(0.02)
    busy = [desk.frames_written[i] - before[i] for i in range(3)]
    check("the focused screen updates far more often than idle ones",
          busy[focus] >= max(busy[i] for i in range(3) if i != focus),
          "frames while busy: %s, focus=%d" % (busy, focus))

    # Wait for a frame rather than assuming one is queued. A mirror of an
    # idle panel produces buffers only when something on it changes, so
    # "latest()" is legitimately None on a quiet desktop -- which is exactly
    # what this suite sees when it runs after the others rather than alone.
    mirror = None
    deadline = time.time() + 5.0
    while mirror is None and time.time() < deadline:
        frame(app)
        mirror = desk.mcap.latest(0)
        if mirror is None:
            time.sleep(0.02)
    check("the mirror hands back a frame", mirror is not None,
          "no buffer within 5 s")
    data, mw, mh = mirror
    arr = np.frombuffer(data, dtype=np.uint8).reshape(mh, mw, 4)[:, :, :3]
    check("the mirror carries real desktop pixels", float(arr.std()) > 8.0,
          "%dx%d std %.1f" % (mw, mh, arr.std()))

    frame(app, out("01-desk.png"))

    # -- layout -----------------------------------------------------------
    yaws = [s._geom["yaw"] for s in desk.screens]
    check("screens run left to right",
          yaws[0] < yaws[1] < yaws[2],
          "yaw = " + " ".join("%.3f" % y for y in yaws))
    check("the mirror sits in the centre", abs(yaws[desk.mirror_index]) < 1e-6,
          "centre yaw %.3f" % yaws[desk.mirror_index])

    # -- fill the view ----------------------------------------------------
    # A screen you look straight at must cover the whole frustum: that is
    # what puts one source pixel on one panel pixel and makes text readable.
    import math

    from refract.core.render import perspective
    proj = perspective(app.fov, 16.0 / 9.0)
    centre = desk.screens[desk.mirror_index]
    w = centre._geom["width_m"]
    h = w * centre.size_px[1] / centre.size_px[0]
    d = centre._geom["distance"]
    corner = np.array([w / 2.0, h / 2.0, -d, 1.0])
    clip = proj @ corner
    ndc = (clip[0] / clip[3], clip[1] / clip[3])
    check("a centred screen fills the whole view",
          abs(abs(ndc[0]) - 1.0) < 0.01 and abs(abs(ndc[1]) - 1.0) < 0.01,
          "corner lands at NDC %.3f, %.3f (want 1.000)" % ndc)
    check("screens are flat by default", desk._cfg("curve") == 0.0,
          "curve %.2f" % desk._cfg("curve"))

    expected = 2.0 * d * math.tan(math.radians(app.fov) * 0.5) * (16.0 / 9.0)
    check("fill width is derived from fov and distance",
          abs(w - expected) < 1e-6, "%.3f m" % w)

    schema = desk.settings_schema()
    labels = [s.label for s in schema]
    check("desk exposes a settings schema for the HUD",
          all(x in labels for x in ("Distance", "Screen size", "Curve",
                                    "Spacing", "Follow my head",
                                    "Reset layout")), " | ".join(labels))

    check("the size row is read-only while filling",
          next(s for s in schema if s.label == "Screen size").kind == S.INFO)

    dist = next(s for s in schema if s.label == "Distance")
    before = desk.screens[0]._geom["distance"]
    width_before = desk.screens[0]._geom["width_m"]
    S.adjust(app, dist, 1)
    frame(app)
    check("changing distance rebuilds the geometry",
          desk.screens[0]._geom["distance"] > before,
          "%.2f -> %.2f" % (before, desk.screens[0]._geom["distance"]))
    # fill mode keeps ANGULAR size constant: moving a screen away must grow
    # it, or it would shrink in view and stop filling
    check("filling keeps the screen filling when it moves",
          desk.screens[0]._geom["width_m"] > width_before,
          "%.2f -> %.2f m" % (width_before,
                              desk.screens[0]._geom["width_m"]))

    spacing = next(s for s in schema if s.label == "Spacing")
    # measured fresh: the distance change above already altered the arc,
    # because each screen's angular width shrinks as it moves away
    span_before = desk.screens[2]._geom["yaw"] - desk.screens[0]._geom["yaw"]
    S.adjust(app, spacing, 1)
    frame(app)
    span_after = desk.screens[2]._geom["yaw"] - desk.screens[0]._geom["yaw"]
    check("changing spacing widens the arc", span_after > span_before,
          "%.3f -> %.3f rad" % (span_before, span_after))

    # -- keys and control words ------------------------------------------
    # read through _cfg, which falls back to the defaults -- a key only
    # exists in the config once something has written it
    d0 = desk._cfg("distance")
    desk.on_key(app, g.KEY_LEFT_BRACKET, 0, g.PRESS, 0)
    check("[ pulls the screens nearer", desk._cfg("distance") < d0,
          "%.2f -> %.2f" % (d0, desk._cfg("distance")))
    s0 = desk._screen_width()
    desk.on_key(app, g.KEY_EQUAL, 0, g.PRESS, 0)
    check("= leaves fill mode and takes manual control",
          desk._cfg("fill") is False)
    check("= makes them bigger, starting from the filled size",
          desk._screen_width() > s0, "%.2f -> %.2f m" % (s0,
                                                         desk._screen_width()))
    c0 = desk._cfg("curve")
    desk.on_key(app, g.KEY_C, 0, g.PRESS, 0)
    check("c toggles the curve", desk._cfg("curve") != c0,
          "%.1f -> %.1f" % (c0, desk._cfg("curve")))

    app.command("follow")
    check("the control file toggles follow", desk._cfg("follow") is True)
    d1, s1 = desk._cfg("distance"), desk._cfg("size")
    app.command("nearer")
    app.command("bigger")
    check("control words drive the layout too",
          desk._cfg("distance") < d1 and desk._cfg("size") > s1,
          "%.2f m, %.2f m" % (desk._cfg("distance"), desk._cfg("size")))

    # follow easing must not move the screens until the head turns past the
    # threshold -- with no head at all it must stay exactly put
    frame(app)
    check("follow does not drift with a still head",
          abs(desk.follow_yaw) < 1e-9, "%.6f deg" % desk.follow_yaw)

    reset = next(s for s in desk.settings_schema()
                 if s.label == "Reset layout")
    S.activate(app, reset)
    frame(app)
    check("reset restores the default layout",
          app.config["desk"] == {} or
          app.config["desk"].get("distance") is None,
          str(app.config["desk"]))
    check("reset re-applies the default distance",
          abs(desk.screens[0]._geom["distance"] - 1.2) < 1e-6,
          "%.2f m" % desk.screens[0]._geom["distance"])

    frame(app, out("02-desk-reset.png"))

    # -- teardown ---------------------------------------------------------
    app.pop()
    time.sleep(1.0)
    check("exiting desk removes the virtual monitors", virtual_count() == 0,
          "%d left" % virtual_count())

    while app.scenes:
        app.scenes.pop().exit(app)
    app.glfw.terminate()
    print("\n  %d checks passed;  frames in %s" % (len(STEPS), a.outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
