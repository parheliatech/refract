"""Entry point: python -m refract

Boot order matters: IMU first, then the SBS
switch, THEN the window -- the SBS switch re-enumerates the display, so it
has to happen before the window is sized against it.
"""

import argparse
import math
import sys

import numpy as np

from refract import __version__


def parse_sim(spec):
    y, p, r = (math.radians(float(v)) for v in spec.split(","))
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0],
                   [-math.sin(y), 0, math.cos(y)]], dtype="f4")
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)],
                   [0, math.sin(p), math.cos(p)]], dtype="f4")
    rz = np.array([[math.cos(r), -math.sin(r), 0],
                   [math.sin(r), math.cos(r), 0], [0, 0, 1]], dtype="f4")
    return (ry @ rx @ rz).astype("f4")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="refract",
        description="Refract -- XR shell for the VITURE Pro XR glasses.")
    ap.add_argument("--version", action="version",
                    version=f"Refract {__version__}")
    ap.add_argument("--test-card", action="store_true",
                    help="run the core-runtime test card scene")
    ap.add_argument("--scene", metavar="NAME",
                    help="boot straight into a sub-experience (desk, "
                         "three60, ...) instead of the home screen")
    ap.add_argument("--windowed", action="store_true",
                    help="window on the laptop instead of fullscreen glasses")
    ap.add_argument("--size-win", default="1920x540")
    ap.add_argument("--monitor", default=None,
                    help="glasses output connector, e.g. DP-2 -- "
                         "auto-detected by EDID if omitted")
    ap.add_argument("--fov", type=float, default=46.0)
    ap.add_argument("--ipd", type=float, default=0.063)
    ap.add_argument("--no-imu", action="store_true")
    ap.add_argument("--allow-second", action="store_true",
                    help="start even if another Refract is running (they "
                         "will fight over the glasses and the layout)")
    ap.add_argument("--conflicts", choices=["ask", "stop", "uninstall",
                                            "ignore"], default="ask",
                    help="what to do if another XR driver (Breezy Desktop, "
                         "XRLinuxDriver) is holding the glasses: ask "
                         "(default), stop it, stop and uninstall it, or "
                         "skip the check")
    ap.add_argument("--log-mcu", action="store_true",
                    help="print EVERY MCU event from the glasses, not just "
                         "the first of each id -- for finding wear events")
    ap.add_argument("--log-axis", action="store_true",
                    help="print the rotation axis each head movement actually "
                         "turns about: clean pitch is [1 0 0], and a Z "
                         "component means pitch is bleeding into roll")
    ap.add_argument("--log-tap", action="store_true",
                    help="temple-tap diagnostics: a 2s heartbeat (IMU sample "
                         "rate + peak yaw/roll/pitch deviation) and every "
                         "detector decision (enter/tap/rejected/FIRE)")
    ap.add_argument("--no-sbs", action="store_true",
                    help="do not switch the glasses to side-by-side")
    ap.add_argument("--sim", default=None, metavar="YAW,PITCH,ROLL",
                    help="drive the view with a fixed rotation instead of the"
                         " IMU, degrees -- renderer testing without a head")
    ap.add_argument("--recenter-after", type=float, default=4.0)
    ap.add_argument("--platform", choices=["x11", "wayland", "any"],
                    default="any")
    ap.add_argument("--hud", action="store_true",
                    help="open the HUD at boot (for capture verification)")
    ap.add_argument("--capture", metavar="FILE",
                    help="render a few frames, save the framebuffer, exit")
    ap.add_argument("--capture-after", type=float, default=3.0)
    a = ap.parse_args(argv)

    from refract.core import config as config_mod
    from refract.core.head import Head
    from refract.core.render import App, already_running

    other = already_running()
    if other and not a.allow_second:
        print("  Refract is already running (pid %d).\n"
              "  Two instances fight over the glasses, the desktop layout "
              "and the control file.\n"
              "  Quit it first, or:  python -m refract.ctl quit"
              "   (--allow-second to override)" % other)
        return 1
    from refract.shell.home import HomeScene
    from refract.shell.testcard import TestCardScene

    sim_rot = parse_sim(a.sim) if a.sim else None
    if sim_rot is not None:
        a.no_imu = True

    # Before anything touches the hardware: USB access to the glasses is
    # exclusive, and another driver holding them makes the SDK's init() fail
    # without saying why.
    if not a.no_imu:
        from refract.core import conflicts
        if not conflicts.check(a.conflicts):
            return 1

    # If a previous run was killed with the panel blanked, put it back
    # before doing anything else.
    from refract.core import backlight
    backlight.recover_stale()

    cfg = config_mod.load()

    # IMU first: the SBS switch re-enumerates the display, so it has to
    # happen before the window is sized against it.
    head = None
    if not a.no_imu:
        head = Head()
        head.log_all_mcu = a.log_mcu
        try:
            imu_hz = int(cfg["global"].get("imu_rate", 240))
        except (TypeError, ValueError):
            imu_hz = 240
        imu_aux = bool(cfg["global"].get("imu_aux", False))
        head.start(rate_hz=imu_hz, aux=imu_aux)
        if head.error:
            print("  imu          : %s" % head.error)
            if not a.windowed and not a.no_sbs:
                # Without the SDK there is no side-by-side switch, and a
                # fullscreen window on a 2D panel shows both eyes the same
                # squeezed image: a clean-looking start that is useless.
                print("  imu          : cannot switch the glasses to "
                      "side-by-side, so not starting (--windowed, --no-sbs "
                      "or --no-imu to run anyway)")
                return 1
            print("  imu          : no head tracking, no side-by-side "
                  "switch, and no head-bob HUD gesture this session")
            head = None
        else:
            print("  imu          : VITURE SDK, %d Hz requested%s"
                  % (imu_hz, ", extended report (accel + gyro)"
                     if head.aux_mode else ""))
            try:
                head.predict_s = max(0.0, float(
                    cfg["global"].get("predict_ms", 0) or 0)) / 1000.0
            except (TypeError, ValueError):
                head.predict_s = 0.0
            if head.predict_s:
                print("  prediction   : %.0f ms" % (head.predict_s * 1000))

    # No --monitor given: find the glasses by EDID. The connector name varies
    # by machine, and a wrong guess does not fail loudly -- the window falls
    # back to whatever monitor GNOME picks, usually the laptop panel.
    if a.monitor is None:
        from refract.core import displaymode
        try:
            a.monitor = displaymode.glasses_connector()
        except Exception as e:                            # noqa: BLE001
            print("  monitor      : could not query outputs: %s" % e)
        if a.monitor:
            print("  monitor      : %s (auto-detected)" % a.monitor)
        elif a.windowed:
            a.monitor = "DP-2"      # never used for placement when windowed
        else:
            # no guessing: a wrong connector lands us on the laptop panel
            print("  monitor      : no VITURE display found. Are the glasses "
                  "plugged in, in DisplayPort mode?\n"
                  "                 (pass --monitor NAME to force a "
                  "connector, or --windowed to run on the laptop)")
            if head:
                head.stop()
            App.hard_exit(1)    # the IMU is up; its SDK threads never join

    sbs_ours = False
    sbs_ok = False
    if not a.windowed and head and not a.no_sbs:
        from refract.core import displaymode
        already = displaymode.is_sbs(a.monitor)
        if not already:
            print("  %s is in 2D -- switching to side-by-side" % a.monitor)
            head.set_sbs(True)
            sbs_ours = True
        # Confirm the mode is really there before the renderer builds an
        # eye-split framebuffer around it -- otherwise it looks like a clean
        # start, but both eyes see the same squeezed half-image.
        sbs_ok = displaymode.wait_for_mode(a.monitor)
        if sbs_ok:
            print("  side-by-side : already on" if already
                  else "  side-by-side : on")
        else:
            msg = ("The glasses would not switch to side-by-side "
                   "(%s is not reporting 3840x1080) -- unplug and replug "
                   "them, then start Refract again." % a.monitor)
            print("  side-by-side : FAILED -- " + msg, flush=True)
            # A fullscreen window on a 2D panel shows both eyes the same
            # squeezed half-image; do not start into that. The firmware
            # refuses set_3d now and then until the cable is reseated.
            import shutil
            import subprocess
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", "-i", "refract", "Refract",
                                msg], check=False)
            head.stop()
            App.hard_exit(1)    # the IMU is up; its SDK threads never join

    app = App(head=head, sim_rot=sim_rot, fov=a.fov, ipd=a.ipd,
              monitor=a.monitor, windowed=a.windowed,
              size_win=tuple(int(v) for v in a.size_win.lower().split("x")),
              platform=a.platform, recenter_after=a.recenter_after,
              config=cfg, log_axis=a.log_axis, log_tap=a.log_tap)
    app.sbs_ours = sbs_ours
    app.sbs_ok = sbs_ok
    print("  hud key      : %s" % " or ".join(app.hud.combo_names))
    print("  output       : %dx%d  eye %dx%d"
          % (app.fb_w, app.fb_h, app.eye_w, app.eye_h))
    app.push(TestCardScene() if a.test_card else HomeScene())
    if a.scene:
        from refract.shell.registry import by_name
        entry = by_name(a.scene)
        if entry is None:
            print("  no such sub-experience: %s" % a.scene)
        else:
            app.launch(entry)
    if a.hud:
        app.hud.show()
    app.run(capture=a.capture, capture_after=a.capture_after)
    App.hard_exit(0)      # the vendor SDK's deinit hangs


if __name__ == "__main__":
    sys.exit(main())
