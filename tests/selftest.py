#!/usr/bin/env python3
"""Pure-math regressions -- no GL context, no glasses, no window.

    .venv/bin/python tests/selftest.py

Everything here is something that can be silently wrong while a screenshot
still looks plausible: axis conventions, handedness, and pointer aiming. The
rendering itself is verified separately by capture (see DEVELOPMENT_PLAN.md
"Verification discipline").
"""

import ctypes
import math
import os
import sys
import time

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

    # offset_deg: how vehicle-motion compensation composes with yaw-only.
    # It has to be an ADDITION to the extracted yaw before rebuilding the
    # rotation, not some separate correction, or the two would fight.
    base_yaw = head_yaw_deg(rot_y(math.radians(28.0)))
    check("offset_deg=0 changes nothing",
          np.allclose(yaw_only(rot_y(math.radians(28.0)), offset_deg=0.0),
                      yaw_only(rot_y(math.radians(28.0))), atol=1e-6))
    shifted = yaw_only(rot_y(math.radians(28.0)), offset_deg=10.0)
    check("a positive offset adds to the reported yaw",
          abs(head_yaw_deg(shifted) - (base_yaw + 10.0)) < 1e-3,
          "%.2f deg" % head_yaw_deg(shifted))
    cancelled = yaw_only(rot_y(math.radians(28.0)), offset_deg=-base_yaw)
    check("the offset a vehicle-compensation caller would pass "
          "(-vehicle_yaw, when vehicle_yaw == head yaw) cancels it out",
          abs(head_yaw_deg(cancelled)) < 1e-3,
          "%.2f deg" % head_yaw_deg(cancelled))


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


def test_temple_tap():
    """Three quick temple taps -- right opens the HUD, left recenters, and
    ordinary head motion does neither.

    Built against tools/temple-tap-probe.py's worn capture: a tap is a
    ~0.5 deg yaw pulse ~40 ms wide with roll/pitch near still, and "three
    quick" landed ~360 ms apart.
    """
    print("temple-tap gesture")
    from refract.core.gesture import TempleTap

    HZ = 112.0                         # what the glasses actually deliver
    PITCH0 = 30.0                      # the wearer's neutral head pitch

    def still(seq, secs, jitter=0.0):
        t = (seq[-1][0] + 1.0 / HZ) if seq else 0.0
        for i in range(int(secs * HZ)):
            wob = jitter * math.sin(i * 0.7) if jitter else 0.0
            seq.append((t + i / HZ, 0.0, PITCH0, wob))
        return seq

    def pulse(seq, yaw=0.0, pitch=0.0, roll=0.0, width=0.05):
        """One impulse: half-sine bumps on the given axes over `width` s,
        then a small opposite rebound -- the shape a finger tap leaves.
        Measured worn taps are ~0.5 deg of yaw peaking in 20-40 ms."""
        t = seq[-1][0] + 1.0 / HZ
        n = max(3, int(width * HZ))
        for i in range(n):
            k = math.sin(math.pi * i / n)
            seq.append((t + i / HZ, roll * k, PITCH0 + pitch * k, yaw * k))
        t = seq[-1][0] + 1.0 / HZ
        m = max(2, int(0.03 * HZ))
        for i in range(m):
            k = -0.3 * math.sin(math.pi * i / m)
            seq.append((t + i / HZ, roll * k, PITCH0 + pitch * k, yaw * k))
        return seq

    def triple(sign, inner=0.36, tail=1.1, **kw):
        s = still([], 1.0)
        kw.setdefault("yaw", sign * 0.55)
        for k in range(3):
            pulse(s, **kw)
            still(s, inner if k < 2 else tail)
        return s

    def feed(seq, g=None):
        g = g or TempleTap()
        return [v for (t, r, p, y) in seq if (v := g.update(t, r, p, y))]

    check("three right taps open the HUD (+1)", feed(triple(+1)) == [1])
    check("three left taps recenter (-1)", feed(triple(-1)) == [-1])
    # a real worn tap brings some roll with it -- that must still pass
    check("taps carrying ~0.5 deg of coincident roll still fire",
          feed(triple(+1, roll=0.5)) == [1])

    # a single tap, and a double, do nothing
    one = still([], 1.0)
    pulse(one, yaw=0.55)
    still(one, 1.0)
    check("one tap does nothing", feed(one) == [])
    two = still([], 1.0)
    pulse(two, yaw=0.55)
    still(two, 0.36)
    pulse(two, yaw=0.55)
    still(two, 1.0)
    check("two taps do nothing", feed(two) == [])

    # three "taps" that also swing pitch hard -- i.e. nods
    check("yaw pulses riding a big pitch swing (a nod) do not fire",
          feed(triple(+1, pitch=8.0)) == [])
    # a gentler nod: pitch only 2 deg, but still dwarfs the yaw
    check("yaw pulses riding even a 2 deg pitch swing do not fire",
          feed(triple(+1, pitch=2.0)) == [])
    # ...or roll (a head tilt / jostle)
    check("yaw pulses riding a roll swing do not fire",
          feed(triple(+1, roll=4.0)) == [])

    # a big slow head turn: rises over hundreds of ms, past the amp cap
    turn = still([], 1.0)
    t0 = turn[-1][0]
    for i in range(int(1.2 * HZ)):
        turn.append((t0 + i / HZ, 0.0, PITCH0,
                     18.0 * math.sin(math.pi * i / (1.2 * HZ))))
    still(turn, 1.0)
    check("a slow head turn does not fire", feed(turn) == [])

    # sub-threshold yaw wobble, however busy
    check("shallow yaw jitter does not fire",
          feed(still([], 4.0, jitter=0.15)) == [])

    # three taps but spaced out over seconds -- three separate taps
    spread = still([], 1.0)
    for _ in range(3):
        pulse(spread, yaw=0.55)
        still(spread, 1.2)
    check("taps spread over seconds do not fire", feed(spread) == [])

    # alternating temples cancel rather than fire either gesture
    alt = still([], 1.0)
    for sg in (+1, -1, +1):
        pulse(alt, yaw=sg * 0.55)
        still(alt, 0.36)
    still(alt, 1.1)
    check("alternating-sign taps do not fire", feed(alt) == [])

    # six right taps -> the HUD toggles twice, not six times, not once
    six = still([], 1.0)
    for k in range(6):
        pulse(six, yaw=0.55)
        still(six, 0.36 if k % 3 != 2 else 1.4)
    check("six right taps fire exactly twice", feed(six) == [1, 1])

    # it must not double-fire off the tail of one clean gesture
    check("a clean triple fires exactly once", feed(triple(+1)) == [1])


def test_vehicle_yaw():
    """Vehicle-motion compensation (prototype) -- the pure integration math,
    without touching the laptop's actual sensor.

    A gyroscope reports a RATE, not an angle, so recovering an angle means
    summing (rate x time) -- and any small zero-rate error in the sensor
    accumulates into a false rotation over time if it isn't removed first.
    These checks are about exactly that: does a constant rate integrate to
    the angle it should, and does calibrate() actually cancel a rest-state
    bias rather than just recording it.
    """
    print("vehicle yaw integration")
    import math

    from refract.core.vehicle import YawIntegrator

    # 90 deg/s for exactly 1 second should read back as 90 degrees -- basic
    # sanity on the units (the sensor reports radians/second; this is where
    # a stray forgotten math.degrees() would show up).
    yi = YawIntegrator()
    rate = math.radians(90.0)
    yi.update(0.0, rate)          # establishes the baseline; contributes 0
    t = 0.0
    for _ in range(100):
        t += 0.01
        yi.update(t, rate)
    check("constant rate integrates to the right angle",
          abs(yi.yaw_deg - 90.0) < 0.5, "got %.2f" % yi.yaw_deg)

    # A sensor that is NOT moving still reports some small nonzero rate --
    # that is the bias calibrate() exists to remove. Uncorrected, holding
    # the "still" reading and integrating it for a while must NOT settle at
    # zero; corrected, it must.
    still_rate = math.radians(3.0)          # a plausible MEMS gyro bias
    uncorrected = YawIntegrator()
    t = 0.0
    for _ in range(500):
        t += 0.01
        uncorrected.update(t, still_rate)
    check("an unremoved bias drifts away from zero",
          abs(uncorrected.yaw_deg) > 5.0, "got %.2f" % uncorrected.yaw_deg)

    corrected = YawIntegrator()
    check("calibrate() learns the bias from still samples",
          corrected.calibrate([still_rate] * 100))
    t = 0.0
    for _ in range(500):
        t += 0.01
        corrected.update(t, still_rate)
    check("and a calibrated sensor holding still stays near zero",
          abs(corrected.yaw_deg) < 0.5, "got %.2f" % corrected.yaw_deg)

    # calibrate() must not crash or silently misbehave on no data -- it is
    # called from a live sensor, and an empty read is a real possibility.
    empty = YawIntegrator()
    check("calibrating on no samples is refused, not crashed",
          empty.calibrate([]) is False)
    check("and leaves the integrator untouched", empty.bias == 0.0)

    # reset() (used for recenter()) must zero the accumulated angle and let
    # a fresh update() start clean rather than computing a huge dt against
    # a stale timestamp.
    yi.reset()
    check("reset() zeroes the accumulated yaw", yi.yaw_deg == 0.0)
    yi.update(1000.0, rate)
    check("and the first update() after reset contributes nothing (no "
          "baseline to diff against yet)", yi.yaw_deg == 0.0)

    # LaptopIMU on a machine with no sensor (or a test double standing in
    # for one): every method must be a harmless no-op, since Desk calls
    # these unconditionally whenever "Cancel vehicle motion" is on, and a
    # crash here would take Desk down with it.
    from refract.core.vehicle import LaptopIMU
    imu = LaptopIMU.__new__(LaptopIMU)          # skip __init__'s sysfs probe
    imu.gyro_dir = imu.accel_dir = None
    imu.available = False
    imu.axis, imu.sign = 2, 1.0
    imu.integrator = YawIntegrator()
    imu.calibrating = imu.calibrated = False
    imu._cal_samples, imu._cal_until = [], 0.0
    check("begin_calibrate() refuses when unavailable, does not crash",
          imu.begin_calibrate() is False)
    check("update() is a harmless no-op when unavailable",
          imu.update() == 0.0)


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


def test_conflicts():
    """Another XR driver holding the glasses, and what we offer to do.

    Every branch here is one nobody wants to discover on a head: matching the
    WRONG device would kill an unrelated process, and matching nothing would
    let Refract start into a guaranteed SDK failure. The vendor uninstallers
    are never actually run -- only the decision that leads to them.
    """
    print("driver conflicts")
    from refract.core import conflicts

    # hidraw identification. HID_ID is bus:vendor:product, zero padded and
    # upper case; a substring match on "35CA" would also claim this Wacom's
    # neighbours, and the 0x35CA in a PRODUCT id is not our device.
    viture = "DRIVER=hid-generic\nHID_ID=0003:000035CA:0000101D\n"
    wacom = "DRIVER=wacom\nHID_ID=0018:0000056A:00004846\n"
    decoy = "DRIVER=hid-generic\nHID_ID=0003:00001234:000035CA\n"
    check("hidraw: the glasses are recognised",
          conflicts.hid_is_viture(viture))
    check("hidraw: another device is not",
          conflicts.hid_is_viture(wacom) is False)
    check("hidraw: 35CA in the PRODUCT id is not a match",
          conflicts.hid_is_viture(decoy) is False)
    check("hidraw: no HID_ID at all is not a match",
          conflicts.hid_is_viture("DRIVER=x\n") is False)

    # `gnome-extensions info`. Installed-but-disabled is NOT a conflict --
    # treating it as one would interrupt every launch on a machine that
    # merely tried Breezy Desktop once.
    on = "breezydesktop@xronlinux.com\n  Name: Breezy\n  Enabled: Yes\n"
    off = "breezydesktop@xronlinux.com\n  Name: Breezy\n  Enabled: No\n"
    check("extension: enabled is read as enabled",
          conflicts.extension_enabled(on))
    check("extension: disabled is not",
          conflicts.extension_enabled(off) is False)
    check("extension: absent is not", conflicts.extension_enabled("") is False)

    class Fake(conflicts.Conflict):
        name = "FakeDriver"
        why = "stands in for a real one"

        def __init__(self, running=True):
            self.running = running
            self.acted = []
            super().__init__()

        def detect(self):
            if self.running:
                self.active.append("fakeDriver is running (pid 4242)")
            self.dormant.append("installed: ~/.local/bin/fakeDriver")

        def refresh(self):
            return self

        def removable(self):
            return True

        def stop(self, log=lambda *a: None):
            self.acted.append("stop")
            self.running = False
            return True

        def uninstall(self, log=lambda *a: None):
            self.acted.append("uninstall")
            return True

    def run(mode, running=True):
        f = Fake(running)
        conflicts.scan = lambda exclude_pids=(): [f]
        conflicts.still_held = lambda exclude=(): []
        carried_on = conflicts.check(mode, log=lambda *a: None)
        return f, carried_on

    f, go = run("stop")
    check("stop: stops it and carries on", go and f.acted == ["stop"])
    f, go = run("uninstall")
    check("uninstall: stops it FIRST, then uninstalls",
          go and f.acted == ["stop", "uninstall"], "%s" % f.acted)
    f, go = run("ignore")
    check("ignore: touches nothing", go and f.acted == [])
    f, go = run("stop", running=False)
    check("installed but not running is not worth stopping",
          go and f.acted == [], "a dormant install must never interrupt a "
                               "launch")

    check("present: something on disk still counts as found",
          Fake(running=False).present)
    check("active: but only a running one is active",
          conflicts.active([Fake(running=False)]) == [])

    # An unknown holder can be stopped but never uninstalled -- we have no
    # idea what it is, so offering to remove it would be a lie.
    holder = conflicts.ForeignHolder(4242, "python3", "/dev/hidraw9")
    check("unknown holder: reported as active", bool(holder.active))
    check("unknown holder: has no uninstaller", holder.removable() is False)
    check("unknown holder: names the node it holds",
          any("/dev/hidraw9" in ln for ln in holder.active))

    # Never kill ourselves to free the glasses.
    check("kill_pids refuses our own pid",
          conflicts.kill_pids([os.getpid()], log=lambda *a: None) == []
          and os.path.exists("/proc/%d" % os.getpid()))
    check("node_holders with no device finds nobody",
          conflicts.node_holders([]) == [])


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
            self._parked_iconified = False
            self.sbs_ours = False
            self.windowed = False
            self.head = None
            self.scene = None
            self.win = None
            self.monitor = "DP-2"
            self._device_t = 0.0
            self._device_present = True
            self._device_pending_absent_since = None
            self._output_bad_since = None
            self._parked_by_unplug = False
            self._unplug_dialog = None
            self.quit = False
            self.reasserted = 0
            self.glfw_calls = []
            self.glfw = type("G", (), {
                "iconify_window": lambda s, w: self.glfw_calls.append("iconify"),
                "restore_window": lambda s, w: self.glfw_calls.append("restore"),
                "focus_window": lambda s, w: self.glfw_calls.append("focus")})()

        def recenter(self):
            pass

        def reassert_output(self):
            self.reasserted += 1

    app = FakeApp()
    t = 100.0
    check("no event while it stays plugged in",
          handoff.poll_device(app, t, lambda: True) is None)

    # Unplugging must be DEBOUNCED -- a reseat blips absent/present for a
    # few seconds before it settles, and reacting to the first absent
    # reading tore Desk down over a single manual replug in practice.
    t += handoff.DEVICE_POLL
    check("first absent reading only starts the debounce",
          handoff.poll_device(app, t, lambda: False) is None)
    check("nothing parked yet", app.parked is False)
    t += handoff.DEVICE_POLL
    check("still inside the confirm window",
          handoff.poll_device(app, t, lambda: False) is None)
    check("still nothing parked", app.parked is False)
    t += handoff.DEVICE_POLL              # now >= CONFIRM_UNPLUG since the
                                           # first absent reading
    check("sustained absence finally commits as an unplug",
          handoff.poll_device(app, t, lambda: False) == "unplugged")
    check("and it is parked", app.parked is True)
    # GLFW's focus_window() is a documented no-op under Wayland -- an
    # unplug-triggered park must not iconify, or resume() has no reliable
    # way back (confirmed live: the window stayed hidden behind the
    # desktop until manually clicked).
    check("an unplug-triggered park does NOT iconify the window",
          "iconify" not in app.glfw_calls, str(app.glfw_calls))

    t += handoff.DEVICE_POLL
    check("staying unplugged does not park again",
          handoff.poll_device(app, t, lambda: False) is None)
    t += handoff.DEVICE_POLL
    check("replugging resumes immediately -- no debounce that direction",
          handoff.poll_device(app, t, lambda: True) == "replugged")
    check("and it is running again", app.parked is False)
    check("and resume never tried to restore/focus a never-iconified window",
          "restore" not in app.glfw_calls and "focus" not in app.glfw_calls,
          str(app.glfw_calls))

    # A DELIBERATE park (e.g. the Display Handoff hotkey) is different --
    # the wearer is choosing to switch away, so iconifying is correct and
    # safe (they will focus whatever they click next themselves).
    app5 = FakeApp()
    handoff.park(app5)
    check("a deliberate park DOES iconify",
          "iconify" in app5.glfw_calls, str(app5.glfw_calls))
    handoff.resume(app5)
    check("and resume tries to restore/focus it",
          "restore" in app5.glfw_calls and "focus" in app5.glfw_calls,
          str(app5.glfw_calls))

    # A blip shorter than the confirm window must have NO effect at all.
    t += handoff.DEVICE_POLL
    check("a blip starts debouncing",
          handoff.poll_device(app, t, lambda: False) is None)
    t += handoff.DEVICE_POLL
    check("present again before the confirm window elapses",
          handoff.poll_device(app, t, lambda: True) is None)
    check("nothing was ever parked for the blip", app.parked is False)

    # a DELIBERATE park must survive a replug -- it was not the cable's doing
    handoff.park(app)
    t += handoff.DEVICE_POLL
    handoff.poll_device(app, t, lambda: False)
    t += handoff.DEVICE_POLL
    handoff.poll_device(app, t, lambda: False)
    t += handoff.DEVICE_POLL
    handoff.poll_device(app, t, lambda: False)      # confirms the unplug
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

    # the "quit?" dialog after an accidental unplug -- exercised without a
    # real zenity process by planting a fake Popen-shaped handle directly.
    class FakeDialog:
        def __init__(self, rc=None):
            self.rc = rc
            self.terminated = False

        def poll(self):
            return self.rc

        def terminate(self):
            self.terminated = True

    app2 = FakeApp()
    app2._unplug_dialog = FakeDialog(rc=0)      # "Quit" pressed (rc=0)
    handoff.poll_device(app2, 0.0, lambda: True)
    check("choosing Quit in the dialog quits", app2.quit is True)
    check("the answered dialog handle is cleared", app2._unplug_dialog is None)

    app3 = FakeApp()
    app3._unplug_dialog = FakeDialog(rc=1)      # "Keep it running" (rc!=0)
    handoff.poll_device(app3, 0.0, lambda: True)
    check("choosing 'keep running' does not quit", app3.quit is False)
    check("that dialog handle is cleared too", app3._unplug_dialog is None)

    app4 = FakeApp()
    t2 = 100.0
    handoff.poll_device(app4, t2, lambda: True)          # baseline: present
    for _ in range(3):                                   # confirm the unplug
        t2 += handoff.DEVICE_POLL
        handoff.poll_device(app4, t2, lambda: False)
    check("the unplug committed", app4.parked is True)
    dlg = FakeDialog(rc=None)                             # still awaiting
    app4._unplug_dialog = dlg
    t2 += handoff.DEVICE_POLL
    handoff.poll_device(app4, t2, lambda: True)           # replug
    check("a replug dismisses an unanswered dialog",
          dlg.terminated is True and app4._unplug_dialog is None)

    # LOST OUTPUT while the USB device stays put -- a connector flex makes
    # the compositor migrate our fullscreen window onto the laptop panel.
    # The USB present_fn keeps saying True, so this is only caught by the
    # separate output-health check; drive that check directly.
    healthy = [True]
    asked = []
    orig_health = handoff._output_healthy
    orig_ask = handoff._ask_quit_on_unplug
    handoff._output_healthy = lambda app: healthy[0]
    handoff._ask_quit_on_unplug = lambda app, reason="unplugged": \
        asked.append(reason)
    try:
        up = lambda: True                    # USB stays present throughout
        app6 = FakeApp()
        app6.head = object()                  # a real session owns an output
        t3 = 100.0
        check("healthy output, USB present -> nothing",
              handoff.poll_device(app6, t3, up) is None)
        healthy[0] = False
        t3 += handoff.DEVICE_POLL
        check("first bad-output reading only starts the debounce",
              handoff.poll_device(app6, t3, up) is None
              and app6.parked is False)
        t3 += handoff.DEVICE_POLL
        handoff.poll_device(app6, t3, up)
        t3 += handoff.DEVICE_POLL              # now past CONFIRM_UNPLUG
        check("a sustained output loss parks as 'display-lost'",
              handoff.poll_device(app6, t3, up) == "display-lost"
              and app6.parked is True)
        check("a lost-output park does NOT iconify (no way back if it did)",
              "iconify" not in app6.glfw_calls, str(app6.glfw_calls))
        check("and it asked on the laptop, naming the right cause",
              asked == ["display-lost"], str(asked))
        healthy[0] = True
        t3 += handoff.DEVICE_POLL
        check("the output coming back resumes and re-places the window",
              handoff.poll_device(app6, t3, up) == "replugged"
              and app6.parked is False and app6.reasserted >= 1)

        # a shorter-than-confirm output blip must do nothing
        app7 = FakeApp()
        app7.head = object()
        t4 = 100.0
        handoff.poll_device(app7, t4, up)
        healthy[0] = False
        t4 += handoff.DEVICE_POLL
        handoff.poll_device(app7, t4, up)
        healthy[0] = True
        t4 += handoff.DEVICE_POLL
        check("an output blip shorter than the confirm window is ignored",
              handoff.poll_device(app7, t4, up) is None
              and app7.parked is False)
    finally:
        handoff._output_healthy = orig_health
        handoff._ask_quit_on_unplug = orig_ask


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

    # Restoring on park/exit used to reapply ONLY the pre-Desk snapshot
    # (real monitors) while the virtual monitors were still present,
    # untouched, at Desk's arrange positions -- a layout with SOME monitors
    # restored and others left wherever is not guaranteed adjacent, and
    # Mutter rejected it ("Logical monitors not adjacent"), which also meant
    # the desktop was never actually put back. The fix (DeskScene.
    # _restore_positions) is exactly this: plan_positions with the ORIGINAL
    # connectors in `order` and everything else parked.
    saved = {"eDP-1": (0, 0), "DP-2": (1920, 0)}    # pre-Desk snapshot
    order = sorted(saved, key=lambda c: saved[c][0])
    virtuals = [c for c, *_ in measured if c not in saved]
    restore = plan_positions(measured, order, park=virtuals)
    check("restore covers every currently-present monitor (Mutter needs "
          "the whole set, not just the restored ones)",
          set(restore) == {c for c, *_ in measured}, str(restore))
    check("the real monitors land back where they started",
          restore["eDP-1"] == saved["eDP-1"]
          and restore["DP-2"] == saved["DP-2"], str(restore))
    check("the still-present virtuals are parked off the restored row",
          all(restore[c][1] > 0 for c in virtuals), str(restore))


def test_fastblit():
    """The C capture fast path, as far as it can be driven without GL.

    Everything up to the upload is testable in a plain process: resolving
    the GstAppSink pointer out of PyGObject, proving it really is an
    AppSink, and reading the frame's size off its caps. The upload itself
    needs a current GL context and is covered by the desk smoke test and by
    on-glasses capture.
    """
    print("fastblit (C capture fast path)")
    from refract.core import fastblit
    from refract.core.vdisplay import ScreenCapture

    # A stream whose sink has not appeared yet must read as "no frame", not
    # as a failure. Desk retires the fast path for the whole run on an error,
    # and the mirror's sink is created asynchronously a moment after its
    # session -- so getting this wrong silently drops back to the slow path
    # depending on which order bring-up happened to complete in.
    cap = ScreenCapture([("virtual", (640, 480))], capture=True)
    rc, _, _ = cap.blit_into(0, 1, 640, 480)
    check("a stream with no sink yet reports NO_FRAME, not an error",
          rc == fastblit.NO_FRAME, "rc=%d" % rc)

    if not fastblit.available():
        # Not a failure: the whole point is that Desk runs without it.
        check("absence is reported, not raised",
              isinstance(fastblit.why_unavailable(), str),
              fastblit.why_unavailable())
        return

    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")
    from gi.repository import Gst
    Gst.init(None)

    sink = Gst.ElementFactory.make("appsink", "t")
    ptr = fastblit._pointer(sink)
    check("the GstAppSink pointer comes back out of PyGObject",
          ptr == hash(sink) and ptr != 0, hex(ptr))

    name = fastblit._lib.refract_gtype_name(ctypes.c_void_p(ptr))
    check("C reads the GType back, so a bad pointer fails loudly",
          name == b"GstAppSink", str(name))

    # The sanity hook has to REJECT things too, or it is decoration.
    other = Gst.ElementFactory.make("fakesink", "f")
    check("a non-appsink is rejected before C dereferences it",
          not fastblit._valid_sink(fastblit._lib, fastblit._pointer(other)))

    # No buffers pending: must report "no frame", not an error and not a
    # blocking wait. This is the common case every idle screen hits.
    rc, _, _ = fastblit.blit(sink, 1, 64, 48)
    check("an empty sink reports NO_FRAME without blocking",
          rc == fastblit.NO_FRAME, "rc=%d" % rc)

    # A real frame of a KNOWN size, asked for at the WRONG size. This is the
    # regression that matters: the first version compared byte counts, so a
    # frame that had grown still looked "big enough" and was uploaded skewed,
    # and a rotation (1920x1080 -> 1080x1920) was not a size change at all.
    # The size must come from caps, and must come back to Python.
    pipe = Gst.parse_launch(
        "videotestsrc num-buffers=8 ! video/x-raw,format=RGBA,width=64,"
        "height=48 ! appsink name=out max-buffers=8 drop=false sync=false")
    pipe.set_state(Gst.State.PLAYING)
    real = pipe.get_by_name("out")
    real.get_state(Gst.SECOND)

    def pull(w, h, limit=2.0):
        """blit(), waiting for a buffer to actually turn up.

        try_pull_sample(0) never waits -- that is what keeps it safe to call
        from the render loop -- so NO_FRAME here means "not yet", not "no".
        """
        end = time.monotonic() + limit
        while True:
            rc, gw, gh = fastblit.blit(real, 1, w, h)
            if rc != fastblit.NO_FRAME or time.monotonic() > end:
                return rc, gw, gh
            time.sleep(0.01)

    rc, gw, gh = pull(1920, 1080)
    check("a wrong-sized texture is refused, not filled with a skewed frame",
          rc == fastblit.ERR_SIZE, "rc=%d" % rc)
    check("and the frame's REAL size comes back, so Python can resize",
          (gw, gh) == (64, 48), "%dx%d" % (gw, gh))

    # Same byte count, transposed: the case a byte-count check cannot see.
    rc, gw, gh = pull(48, 64)
    check("a rotation is caught even though the byte count is identical",
          rc == fastblit.ERR_SIZE and (gw, gh) == (64, 48),
          "rc=%d %dx%d" % (rc, gw, gh))
    pipe.set_state(Gst.State.NULL)


def main():
    test_imu_wire_format()
    test_head_math()
    test_head_conventions()
    test_shell_pointer()
    test_head_bob()
    test_temple_tap()
    test_backlight()
    test_vehicle_yaw()
    test_conflicts()
    test_unplug_handoff()
    test_desk_carousel()
    test_desk_layout()
    test_fastblit()
    print("\n  %d checks passed" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
