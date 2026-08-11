#!/usr/bin/env python3
"""Pure-math regressions -- no GL context, no glasses, no window.

    .venv/bin/python tests/selftest.py

Everything here is something that can be silently wrong while a screenshot
still looks plausible: axis conventions, handedness, and pointer aiming. The
rendering itself is verified separately by capture (see DEVELOPMENT_PLAN.md
"Verification discipline").
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.head import (Head, axis_of, quat_to_mat,      # noqa: E402
                               solve_basis)

PASS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("%s FAILED %s" % (name, detail))
    PASS.append(name)
    print("  ok   %s%s" % (name, ("  " + detail) if detail else ""))


def quat_about(axis, deg):
    ax = np.asarray(axis, dtype=float)
    ax = ax / np.linalg.norm(ax)
    h = math.radians(deg) / 2.0
    return (*(math.sin(h) * ax), math.cos(h))       # x, y, z, w


def test_imu_wire_format():
    """The byte layout of the IMU payload, per sdk/sample/src/main.c.

    This exists because reading the quaternion in the wrong component order
    is SILENT: it still normalises, still produces a valid rotation matrix,
    and merely describes the wrong rotation. It cost days of chasing head
    tracking that was inverted and cross-coupled, and it defeated three
    rounds of calibration, because no basis can undo a scrambled quaternion.
    """
    print("imu wire format")
    import struct

    from refract.core.viture_sdk import be_float, parse_imu

    # vendor layout: roll/pitch/yaw big-endian floats at 0/4/8, then
    # quaternion W,X,Y,Z at 20/24/28/32
    roll, pitch, yaw = 11.0, 126.0, -40.0
    qw, qx, qy, qz = 0.9063, 0.4226, 0.0, 0.0        # 50 deg about X
    buf = bytearray(36)
    for off, val in ((0, roll), (4, pitch), (8, yaw),
                     (20, qw), (24, qx), (28, qy), (32, qz)):
        buf[off:off + 4] = struct.pack(">f", val)
    buf = list(buf)

    check("floats are big-endian", abs(be_float(buf, 0) - roll) < 1e-4)
    euler, quat = parse_imu(buf)
    check("euler is (roll, pitch, yaw) at 0/4/8",
          all(abs(euler[i] - v) < 1e-4
              for i, v in enumerate((roll, pitch, yaw))),
          "%.1f %.1f %.1f" % euler)
    check("the wire SCALAR at offset 20 becomes w, not x",
          abs(quat[3] - qw) < 1e-4 and abs(quat[0] - qx) < 1e-4,
          "parsed (x,y,z,w) = %.3f %.3f %.3f %.3f" % quat)

    # and the decisive one: it must describe the rotation it encodes
    ax, ang = axis_of(quat_to_mat(quat))
    check("the parsed quaternion IS the rotation it encodes",
          abs(ang - 50.0) < 0.5 and abs(abs(ax[0]) - 1.0) < 1e-3,
          "%.1f deg about [%+.3f %+.3f %+.3f]" % (ang, *ax))

    # the old bug, stated as a test so it cannot come back quietly
    scrambled = (qw, qx, qy, qz)          # what we used to hand on
    ax2, ang2 = axis_of(quat_to_mat(scrambled))
    check("the old mis-order really did give a different rotation",
          abs(ang2 - 50.0) > 5.0 or abs(abs(ax2[0]) - 1.0) > 0.01,
          "would have been %.1f deg about [%+.3f %+.3f %+.3f]" % (ang2, *ax2))


def test_head_math():
    print("head math")
    # A calibration where the IMU frame is a pure permutation of the camera
    # frame: the solve must recover exactly that, and call it right-handed.
    perm = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], dtype=float)
    targets = {"up": (1.0, 0.0, 0.0), "right": (0.0, -1.0, 0.0),
               "tiltright": (0.0, 0.0, -1.0)}
    holds = {name: [quat_about(perm.T @ np.asarray(t), 30.0)]
             for name, t in targets.items()}
    basis, mirror, lines = solve_basis((0, 0, 0, 1), holds)
    check("solve_basis returns a basis", basis is not None, str(lines))
    for name, t in targets.items():
        got = basis @ (perm.T @ np.asarray(t))
        check("solve_basis maps %-9s" % name, np.allclose(got, t, atol=1e-5),
              "-> [%.2f %.2f %.2f]" % tuple(got))
    check("solve_basis handedness", mirror is False, "det > 0 -> not mirrored")

    # Too small a movement must be REJECTED, not silently fitted: a wearer who
    # barely moves would otherwise get a garbage basis that feels like drift.
    tiny = {name: [quat_about(perm.T @ np.asarray(t), 3.0)]
            for name, t in targets.items()}
    b2, _, _ = solve_basis((0, 0, 0, 1), tiny)
    check("solve_basis rejects tiny holds", b2 is None)

    h = Head()
    check("Head.matrix is identity before samples",
          np.allclose(h.matrix(), np.eye(3)))
    h2 = Head()
    check("Head settings round-trip", h2.load_settings(h.settings())
          and np.allclose(h2.basis, h.basis) and h2.flip == h.flip)

    ax, ang = axis_of(quat_to_mat(quat_about((0, 1, 0), 40.0)))
    check("axis_of recovers axis and angle",
          abs(ang - 40.0) < 1e-3 and abs(abs(ax[1]) - 1.0) < 1e-6,
          "%.1f deg about [%.2f %.2f %.2f]" % (ang, *ax))


def test_shell_pointer():
    print("shell pointer math")
    from refract.core.render import perspective
    from refract.shell.home import (pick_tile, project_ndc, tile_bounds_ndc,
                                    tile_center, tile_yaws)

    mvp = perspective(46.0, 16.0 / 9.0)          # eye at origin, looking -Z
    yaws = tile_yaws(4)
    bounds = [tile_bounds_ndc(mvp, y) for y in yaws]
    mid = [((b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5) for b in bounds]
    wide = [b[2] - b[0] for b in bounds]
    check("all four tiles project", all(b is not None for b in bounds))
    check("tiles are in view", all(abs(m[0]) < 1.0 for m in mid),
          "x = " + " ".join("%.2f" % m[0] for m in mid))
    check("tiles run left to right",
          all(mid[i][0] < mid[i + 1][0] for i in range(3)))
    check("tiles do not overlap",
          all(bounds[i][2] < bounds[i + 1][0] for i in range(3)))

    for i, m in enumerate(mid):
        check("pointing at tile %d picks it" % i, pick_tile(bounds, m) == i)
    check("pointing above the tiles picks none",
          pick_tile(bounds, (mid[0][0], 0.95)) is None)
    check("pointing far right picks none",
          pick_tile(bounds, (0.99, 0.0)) is None)
    check("pointing just inside an edge still picks",
          pick_tile(bounds, (bounds[0][0] + 1e-3, mid[0][1])) == 0)
    check("pointing just outside an edge picks none",
          pick_tile(bounds, (bounds[0][0] - 1e-3, mid[0][1])) is None)

    # A rectilinear projection STRETCHES off-axis: equal angular tiles get
    # wider in NDC toward the edges. (The first version measured a half-width
    # from one edge only and reported the outer tiles as both narrower and
    # asymmetric -- a biased hit box that drifts off the visible tile.)
    check("outer tiles project wider than inner",
          wide[0] > wide[1] and wide[3] > wide[2],
          "widths = " + " ".join("%.3f" % w for w in wide))
    check("hit boxes are left/right symmetric",
          abs(wide[0] - wide[3]) < 1e-6 and abs(wide[1] - wide[2]) < 1e-6)
    # The box midpoint is NOT the projected centre, and should not be: the
    # quad projects to a trapezoid whose outer edge is stretched further, so
    # its axis-aligned box sits slightly outward. It must still CONTAIN the
    # centre -- that is the property hit testing actually relies on.
    skew = [abs(mid[i][0] - project_ndc(mvp, tile_center(yaws[i]))[0])
            for i in range(4)]
    check("hit boxes contain their tile centre",
          all(pick_tile(bounds, project_ndc(mvp, tile_center(yaws[i]))) == i
              for i in range(4)),
          "outward skew = " + " ".join("%.4f" % s for s in skew))

    # A point behind the eye projects to a perfectly plausible NDC. If this
    # regresses, tiles behind your head become clickable.
    check("points behind the eye are rejected",
          project_ndc(mvp, tile_center(math.pi)) is None)
    check("bounds of a tile behind the eye are None",
          tile_bounds_ndc(mvp, math.pi) is None)


def test_head_conventions():
    """Pitch direction, and Desk's yaw-only steadying."""
    print("head conventions")
    from refract.core.head import (EULER_SIGNS, Head, euler_to_mat,
                                   rot_x, rot_y, rot_z)
    from refract.desk.scene import head_yaw_deg, yaw_only

    # Wearer-reported and fixed: the vendor's pitch is positive nose-DOWN,
    # so looking up must produce a POSITIVE rotation about camera +X.
    h = Head(mode="euler")
    h.euler = (0.0, 0.0, 0.0)
    h.recenter()
    h.euler = (0.0, 20.0, 0.0)          # vendor pitch +20
    ax, ang = axis_of(h.matrix())
    check("pitch sign is negated for the vendor convention",
          EULER_SIGNS[1] == -1.0)
    check("a vendor pitch increase turns about -X (i.e. looking up is +X)",
          abs(ang - 20.0) < 1e-3 and ax[0] < -0.99,
          "%.1f deg about [%+.3f %+.3f %+.3f]" % (ang, *ax))

    # yaw-only: heading survives, pitch and roll are discarded
    messy = np.asarray(rot_y(math.radians(35.0))) @ \
        np.asarray(rot_x(math.radians(18.0))) @ \
        np.asarray(rot_z(math.radians(-12.0)))
    steady = yaw_only(messy)
    check("yaw-only preserves the heading",
          abs(head_yaw_deg(steady) - head_yaw_deg(messy)) < 1e-3,
          "%.2f deg" % head_yaw_deg(steady))
    up = np.asarray(steady) @ np.array([0.0, 1.0, 0.0])
    check("yaw-only leaves up pointing up (no roll, no pitch)",
          abs(up[1] - 1.0) < 1e-6, "up = [%+.3f %+.3f %+.3f]" % tuple(up))
    ax2, ang2 = axis_of(steady)
    check("yaw-only rotates about Y alone",
          abs(abs(ax2[1]) - 1.0) < 1e-6,
          "axis [%+.3f %+.3f %+.3f]" % tuple(ax2))
    check("a pure-yaw input is left untouched",
          np.allclose(yaw_only(rot_y(math.radians(28.0))),
                      np.asarray(rot_y(math.radians(28.0))), atol=1e-5))
    # jitter rejection is the whole point: pitch/roll wobble must not move it
    jitter = np.asarray(rot_y(math.radians(35.0))) @ \
        np.asarray(rot_x(math.radians(3.0))) @ \
        np.asarray(rot_z(math.radians(2.0)))
    check("head wobble does not move a yaw-only view",
          np.allclose(yaw_only(jitter), steady, atol=1e-5))


def test_head_bob():
    """Three nods open the HUD -- and nothing else does.

    A false trigger while someone is reading is worse than a missed
    gesture, so most of these check that it stays SHUT.
    """
    print("head-bob gesture")
    from refract.core.gesture import HeadBob

    def feed(samples, g=None):
        """samples: [(t, pitch)] -> number of times it fired."""
        g = g or HeadBob()
        return sum(1 for t, p in samples if g.update(t, p))

    def nods(n, depth=12.0, period=0.4, start=0.0, hz=60.0):
        """n nods: pitch dips by `depth` and returns, `period` apart."""
        out, t = [], start
        for _ in range(int(start * hz)):
            out.append((len(out) / hz, 0.0))
        for i in range(n):
            steps = int(period * hz)
            for s in range(steps):
                phase = s / steps
                # down and back up over one period
                out.append((t, -depth * math.sin(math.pi * phase)))
                t += 1.0 / hz
        for _ in range(30):
            out.append((t, 0.0))
            t += 1.0 / hz
        return out

    check("three nods fire once", feed(nods(3)) == 1)
    check("two nods do not fire", feed(nods(2)) == 0)
    check("one nod does not fire", feed(nods(1)) == 0)

    # a slow deliberate look down and back: the classic false positive
    slow = []
    t = 0.0
    for s in range(300):
        phase = s / 300.0
        slow.append((t, -35.0 * math.sin(math.pi * phase)))
        t += 1.0 / 60.0
    check("looking down slowly does not fire", feed(slow) == 0)

    # reading: small continuous drift plus jitter
    reading = []
    t = 0.0
    for s in range(600):
        reading.append((t, -12.0 * (s / 600.0)
                        + 1.2 * math.sin(s * 0.7)))
        t += 1.0 / 60.0
    check("reading down a page does not fire", feed(reading) == 0)

    # shallow head jitter, however busy, is not a nod
    jitter = [(s / 60.0, 3.0 * math.sin(s * 1.9)) for s in range(600)]
    check("head jitter does not fire", feed(jitter) == 0)

    # three nods spread too far apart are three separate nods
    spread = nods(1) + nods(1, start=3.0) + nods(1, start=6.0)
    spread = [(i / 60.0, p) for i, (_t, p) in enumerate(spread)]
    check("nods spread over seconds do not fire", feed(spread) == 0)

    # six nods should toggle twice (open then close), not six times
    g = HeadBob()
    fired = sum(1 for t, p in nods(3) if g.update(t, p))
    later = nods(3)
    fired += sum(1 for t, p in [(t + 4.0, p) for t, p in later]
                 if g.update(t, p))
    check("a second triple bob toggles again", fired == 2, "fired %d" % fired)

    # and it must not double-fire from the tail of one gesture
    g2 = HeadBob()
    check("one triple bob fires exactly once",
          sum(1 for t, p in nods(4) if g2.update(t, p)) == 1)


def test_backlight():
    """Blanking the laptop panel for privacy -- and always giving it back.

    The restore half is the one that matters: nobody should be left staring
    at a dark laptop because a scene exited badly.
    """
    print("laptop backlight")
    from refract.core import backlight

    state = {"level": 80, "sets": []}
    backlight.get = lambda: state["level"]

    def fake_set(v):
        state["level"] = int(v)
        state["sets"].append(int(v))
        return True
    backlight.set_level = fake_set

    b = backlight.Backlight()
    check("starts unblanked", b.blanked is False)
    check("blanking turns the panel off", b.blank() and state["level"] == 0)
    check("and remembers what it was", b.saved == 80)
    check("blanking twice is harmless", b.blank() and state["level"] == 0)
    check("restore puts back the ORIGINAL level",
          b.restore() and state["level"] == 80,
          "levels set: %s" % state["sets"])
    check("restore is safe when nothing was blanked", b.restore() is False)
    check("and does not touch the panel", state["level"] == 80)

    # a panel with no brightness control must fail gracefully, not crash
    backlight.get = lambda: None
    b2 = backlight.Backlight()
    check("a panel without brightness control just says no",
          b2.blank() is False and b2.blanked is False)


def test_unplug_handoff():
    """Unplugging is the only handoff we can detect automatically.

    Wear detection is impossible on this hardware: no `get_wear_status` in
    the Linux libglasses, and putting the glasses on and off produces no MCU
    events at all (verified with every event logged, three cycles). The
    cable is what is left.
    """
    print("unplug handoff")
    from refract.core import handoff

    class FakeApp:
        def __init__(self):
            self.parked = False
            self.sbs_ours = False
            self.head = None
            self.scene = None
            self.win = None
            self.monitor = "DP-2"
            self._device_t = 0.0
            self._device_present = True
            self._parked_by_unplug = False
            self.glfw = type("G", (), {
                "iconify_window": staticmethod(lambda w: None),
                "restore_window": staticmethod(lambda w: None),
                "focus_window": staticmethod(lambda w: None)})()

        def recenter(self):
            pass

    app = FakeApp()
    t = 100.0
    check("no event while it stays plugged in",
          handoff.poll_device(app, t, lambda: True) is None)
    t += handoff.DEVICE_POLL
    check("unplugging parks", handoff.poll_device(app, t, lambda: False)
          == "unplugged")
    check("and it is parked", app.parked is True)
    t += handoff.DEVICE_POLL
    check("staying unplugged does not park again",
          handoff.poll_device(app, t, lambda: False) is None)
    t += handoff.DEVICE_POLL
    check("replugging resumes", handoff.poll_device(app, t, lambda: True)
          == "replugged")
    check("and it is running again", app.parked is False)

    # a DELIBERATE park must survive a replug -- it was not the cable's doing
    handoff.park(app)
    t += handoff.DEVICE_POLL
    handoff.poll_device(app, t, lambda: False)
    t += handoff.DEVICE_POLL
    check("a deliberate park is not undone by a replug",
          handoff.poll_device(app, t, lambda: True) != "replugged"
          and app.parked is True)

    # polling must be cheap: no device query between ticks
    calls = []
    t += 0.1
    handoff.poll_device(app, t, lambda: calls.append(1) or True)
    check("device is not queried faster than the poll interval",
          len(calls) == 0)


def test_desk_carousel():
    """Bringing a monitor to you, instead of turning 76 degrees to it."""
    print("desk carousel")
    from refract.core.head import rot_y
    from refract.desk.scene import DeskScene

    class FakeScreen:
        def __init__(self, yaw):
            self._geom = {"yaw": yaw}

    class FakeApp:
        def __init__(self, yaw_deg=0.0):
            self.yaw = yaw_deg

        def head_rot(self):
            # head_yaw_deg reports the opposite sense to rot_y
            return rot_y(-math.radians(self.yaw))

    desk = DeskScene()
    step = math.radians(76.0)
    desk.screens = [FakeScreen(-step), FakeScreen(0.0), FakeScreen(step)]
    desk.labels = ["left", "centre", "right"]

    check("facing forward focuses the centre screen",
          desk._focused_index(FakeApp(0.0)) == 1)
    check("turning left focuses the left screen",
          desk._focused_index(FakeApp(-76.0)) == 0)
    check("turning right focuses the right screen",
          desk._focused_index(FakeApp(76.0)) == 2)

    desk.centre_on(0)
    check("centring the left screen swings the arc to it",
          abs(desk.carousel_target - math.degrees(-step)) < 1e-6,
          "target %.1f deg" % desk.carousel_target)
    desk.carousel = desk.carousel_target
    check("after swinging, the left screen IS what you face",
          desk._focused_index(FakeApp(0.0)) == 0)
    check("and the others moved with it, not away",
          desk._focused_index(FakeApp(76.0)) == 1)

    desk.centre_on(99)
    check("centring clamps to a real screen",
          abs(desk.carousel_target - math.degrees(step)) < 1e-6)


def test_desk_layout():
    print("desk monitor arrangement")
    from refract.desk.layout import (plan_positions, pointer_order,
                                     positions_of)

    # exactly what Mutter produced with Desk running (measured 2026-08-11):
    # the virtual monitors are parked to the right of everything
    measured = [("eDP-1", 0, 0, 1920, 1080),
                ("DP-2", 1920, 0, 3840, 1080),
                ("Meta-0", 5760, 0, 1920, 1080),
                ("Meta-1", 7680, 0, 1920, 1080)]
    check("measured layout disagrees with the 3D order",
          pointer_order(measured) != ["Meta-0", "eDP-1", "Meta-1", "DP-2"],
          " ".join(pointer_order(measured)))

    order = ["Meta-0", "eDP-1", "Meta-1"]          # left, centre, right
    pos = plan_positions(measured, order, park=["DP-2"])
    check("desk screens are laid out in view order",
          [c for c, _ in sorted(pos.items(), key=lambda kv: kv[1][0])][:3]
          == order, str(sorted(pos.items(), key=lambda kv: kv[1][0])))
    check("the arrangement leaves no gaps",
          pos["Meta-0"] == (0, 0) and pos["eDP-1"] == (1920, 0)
          and pos["Meta-1"] == (3840, 0))
    # The glasses output must leave the horizontal path entirely: a window
    # dragged sideways onto it lands in front of your eyes and hides the
    # monitors you were aiming for. Mutter forbids gaps, so it goes on a
    # second ROW instead of a distant column.
    check("the glasses output is off the desk row", pos["DP-2"][1] > 0,
          "DP-2@(%d,%d)" % pos["DP-2"])
    desk_row = [pos[c] for c in order]
    check("the desk monitors share one row",
          all(p[1] == 0 for p in desk_row))
    check("dragging sideways never crosses the glasses output",
          all(p[1] == 0 for p in desk_row) and pos["DP-2"][1] >= 1080,
          "desk row y=0, glasses y=%d" % pos["DP-2"][1])
    check("the parked monitor still touches the row (Mutter needs adjacency)",
          pos["DP-2"][0] < 3840 + 1920 and pos["DP-2"][0] >= 3840,
          "DP-2 x=%d under the right monitor at x=3840" % pos["DP-2"][0])
    check("every monitor keeps a position", len(pos) == len(measured))

    # widths differ per monitor: positions must follow the actual widths, not
    # a fixed stride, or screens overlap and Mutter rejects the config
    mixed = [("A", 0, 0, 1280, 720), ("B", 1280, 0, 3840, 1080),
             ("C", 5120, 0, 1920, 1080)]
    p2 = plan_positions(mixed, ["B", "A", "C"])
    check("positions follow each monitor's own width",
          p2["B"] == (0, 0) and p2["A"] == (3840, 0)
          and p2["C"] == (5120, 0), str(p2))

    snap = positions_of(measured)
    check("a snapshot round-trips for restore",
          snap["Meta-0"] == (5760, 0) and snap["eDP-1"] == (0, 0))
    check("planning does not mutate the input",
          measured[0] == ("eDP-1", 0, 0, 1920, 1080))


def main():
    test_imu_wire_format()
    test_head_math()
    test_head_conventions()
    test_shell_pointer()
    test_head_bob()
    test_backlight()
    test_unplug_handoff()
    test_desk_carousel()
    test_desk_layout()
    print("\n  %d checks passed" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
