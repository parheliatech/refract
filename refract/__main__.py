"""Entry point: python -m refract

Boot order matters and is inherited from xrdesk: IMU first, then the SBS
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
                    help="run the Phase 1 core-runtime test scene")
    ap.add_argument("--scene", metavar="NAME",
                    help="boot straight into a sub-experience (desk, "
                         "three60, ...) instead of the home screen")
    ap.add_argument("--windowed", action="store_true",
                    help="window on the laptop instead of fullscreen glasses")
    ap.add_argument("--size-win", default="1920x540")
    ap.add_argument("--monitor", default="DP-2", help="glasses output")
    ap.add_argument("--fov", type=float, default=46.0)
    ap.add_argument("--ipd", type=float, default=0.063)
    ap.add_argument("--no-imu", action="store_true")
    ap.add_argument("--allow-second", action="store_true",
                    help="start even if another Refract is running (they "
                         "will fight over the glasses and the layout)")
    ap.add_argument("--imu-mode", choices=["euler", "quat"], default="euler",
                    help="euler (default, no calibration -- vendor angles are "
                         "already in the glasses' frame) or quat (old basis)")
    ap.add_argument("--log-mcu", action="store_true",
                    help="print EVERY MCU event from the glasses, not just "
                         "the first of each id -- for finding wear events")
    ap.add_argument("--log-axis", action="store_true",
                    help="print the rotation axis each head movement actually "
                         "turns about: clean pitch is [1 0 0], and a Z "
                         "component means pitch is bleeding into roll")
    ap.add_argument("--no-sbs", action="store_true",
                    help="do not switch the glasses to side-by-side")
    ap.add_argument("--sim", default=None, metavar="YAW,PITCH,ROLL",
                    help="drive the view with a fixed rotation instead of the"
                         " IMU, degrees -- renderer testing without a head")
    ap.add_argument("--recenter-after", type=float, default=25.0)
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
              "  Quit it first, or:  echo quit > /tmp/refract.ctl"
              "   (--allow-second to override)" % other)
        return 1
    from refract.shell.home import HomeScene
    from refract.shell.testcard import TestCardScene

    sim_rot = parse_sim(a.sim) if a.sim else None
    if sim_rot is not None:
        a.no_imu = True

    cfg = config_mod.load()

    # IMU first: the SBS switch re-enumerates the display, so it has to
    # happen before the window is sized against it.
    head = None
    if not a.no_imu:
        head = Head(mode=a.imu_mode)
        head.log_all_mcu = a.log_mcu
        head.start()
        if head.error:
            print("  imu          : %s" % head.error)
            head = None
        else:
            print("  imu          : VITURE SDK, 120 Hz")
            # calibration is a property of the headset, not a session --
            # global section, falling back to where the migration put
            # xrdesk's copy
            imu_cfg = (cfg["global"].get("imu")
                       or cfg["desk"].get("imu") or {})
            if head.load_settings(imu_cfg):
                print("  imu config   : loaded")

    sbs_ours = False
    if not a.windowed and head and not a.no_sbs:
        from refract.core import displaymode
        if not displaymode.is_sbs(a.monitor):
            print("  %s is in 2D -- switching to side-by-side" % a.monitor)
            head.set_sbs(True)
            displaymode.wait_for_mode(a.monitor)
            print("  side-by-side : on")
            sbs_ours = True

    app = App(head=head, sim_rot=sim_rot, fov=a.fov, ipd=a.ipd,
              monitor=a.monitor, windowed=a.windowed,
              size_win=tuple(int(v) for v in a.size_win.lower().split("x")),
              platform=a.platform, recenter_after=a.recenter_after,
              config=cfg, log_axis=a.log_axis)
    app.sbs_ours = sbs_ours
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
            app.push(entry.make_scene())
    if a.hud:
        app.hud.show()
    app.run(capture=a.capture, capture_after=a.capture_after)
    App.hard_exit(0)      # the vendor SDK's deinit hangs


if __name__ == "__main__":
    sys.exit(main())
