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

from refract.core.head import Head, axis_of, quat_to_mat   # noqa: E402

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

    Reading the quaternion in the wrong component order is SILENT: it still
    normalises and still produces a valid rotation matrix -- just the wrong
    rotation.
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
    h = Head()
    check("Head.matrix is identity before samples",
          np.allclose(h.matrix(), np.eye(3)))
    h.euler = (5.0, -10.0, 30.0)
    h.recenter()
    check("Head.matrix is identity at the reference pose",
          np.allclose(h.matrix(), np.eye(3), atol=1e-6))

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
    # wider in NDC toward the edges, and must stay symmetric.
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
    h = Head()
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

    # NEVER open the real "quit?" dialog from a test: with no Refract behind
    # it to dismiss it, it would stay on the desktop.
    real_ask = handoff._ask_quit_on_unplug
    asked = []
    handoff._ask_quit_on_unplug = \
        lambda app, reason="unplugged": asked.append(reason)

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
            self.window_calls = []      # window calls park/resume made

        def blank_window(self):
            self.window_calls.append("blank")

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
    # Wayland gives a client no way to focus or un-minimize its own
    # window -- an unplug-triggered park must not iconify, or resume() has
    # no way back (confirmed live: the window stayed hidden behind the
    # desktop until manually clicked).
    check("an unplug-triggered park does NOT iconify the window",
          "iconify" not in app.window_calls, str(app.window_calls))

    t += handoff.DEVICE_POLL
    check("staying unplugged does not park again",
          handoff.poll_device(app, t, lambda: False) is None)
    t += handoff.DEVICE_POLL
    check("replugging resumes immediately -- no debounce that direction",
          handoff.poll_device(app, t, lambda: True) == "replugged")
    check("and it is running again", app.parked is False)
    check("and resume never tried to restore/focus a never-iconified window",
          "restore" not in app.window_calls and "focus" not in app.window_calls,
          str(app.window_calls))

    # A deliberate park does not minimize either (Wayland cannot
    # un-minimize): it blanks the window, and resume needs no restore.
    app5 = FakeApp()
    handoff.park(app5)
    check("a deliberate park blanks the window instead of minimizing",
          app5.window_calls == ["blank"], str(app5.window_calls))
    handoff.resume(app5)
    check("and resume makes no window calls",
          app5.window_calls == ["blank"], str(app5.window_calls))

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
              "iconify" not in app6.window_calls, str(app6.window_calls))
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

    check("losing the glasses asked whether to quit -- through the stub, "
          "not a real dialog", bool(asked), str(asked))
    handoff._ask_quit_on_unplug = real_ask

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

    # what Mutter produces with Desk running: the virtual monitors are
    # placed to the right of everything
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

    # Restoring on park/exit happens while the virtual monitors still exist,
    # so reapplying only the pre-Desk snapshot is not guaranteed adjacent
    # (Mutter rejects that). DeskScene._restore_positions instead plans with
    # the ORIGINAL connectors in `order` and everything else parked.
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


def test_plugin_discovery():
    """A folder + manifest dropped onto a search root becomes a registered,
    launchable sub-experience -- and one bad folder cannot break the shell.
    """
    print("plugin discovery")
    import shutil
    import tempfile

    from refract.core.render import Scene
    from refract.shell import registry

    base = tempfile.mkdtemp(prefix="refract-plugins-")

    def write(folder, files):
        d = os.path.join(base, folder)
        os.makedirs(d)
        for fn, body in files.items():
            with open(os.path.join(d, fn), "w") as fh:
                fh.write(body)

    # a well-formed plugin
    write("stars", {
        "experience.toml": ('title = "Stars"\nsubtitle = "sky"\n'
                            'scene = "scene:StarsScene"\naccent = [1, 2, 3]\n'),
        "scene.py": ("from refract.core.render import Scene\n"
                     "class StarsScene(Scene):\n"
                     "    name = 'stars'\n    title = 'Stars'\n"),
    })
    # id from an explicit key, JSON manifest, ":attr" left to default to Scene
    write("planet", {
        "experience.json": '{"name": "orrery", "title": "Orrery", '
                           '"scene": "scene"}',
        "scene.py": "from refract.core.render import Scene\n",
    })
    write("notaplugin", {"readme.txt": "no manifest here\n"})
    write("broken", {"experience.json": "{ not valid json "})
    write("nokey", {"experience.toml": 'title = "Nope"\n'})
    write("desk", {"experience.json": '{"title": "Fake", "scene": "x:Y"}'})
    # a plugin folder named like a stdlib module must not replace it
    write("json", {
        "experience.toml": 'title = "J"\nscene = "scene:JScene"\n',
        "scene.py": ("from refract.core.render import Scene\n"
                     "from . import helper\n"
                     "class JScene(Scene):\n    name = helper.NAME\n"),
        "helper.py": "NAME = 'json-plugin'\n",
    })
    path_before = list(sys.path)

    old = os.environ.get("REFRACT_PLUGIN_PATH")
    os.environ["REFRACT_PLUGIN_PATH"] = base
    try:
        registry.reload()
        names = [e.name for e in registry.REGISTRY]
        check("built-ins keep their order, ahead of any plugin",
              names[:4] == ["desk", "three60", "play", "tak"])
        check("a folder + manifest is discovered", "stars" in names)
        check("the 'name' key overrides the folder name",
              "orrery" in names and "planet" not in names)
        check("a folder with no manifest is ignored", "notaplugin" not in names)
        check("an unparseable manifest is skipped, not raised",
              "broken" not in names)
        check("a manifest with no 'scene' key is skipped", "nokey" not in names)
        check("a plugin cannot shadow a built-in",
              sum(n == "desk" for n in names) == 1
              and registry.by_name("desk").title == "Desk")

        stars = registry.by_name("stars")
        check("manifest accent is carried onto the tile",
              stars.accent == (1, 2, 3))
        check("a discovered plugin is available with no phase",
              stars.available and stars.phase == 0)
        check("the scene factory builds a Scene subclass",
              isinstance(stars.make_scene(), Scene))
        check("':attr' defaults to a module-level Scene",
              isinstance(registry.by_name("orrery").make_scene(), Scene))
        jscene = registry.by_name("json").make_scene()
        import json as stdlib_json
        check("a plugin folder named 'json' does not shadow the stdlib",
              hasattr(stdlib_json, "dumps")
              and jscene.name == "json-plugin")
        check("discovery leaves sys.path untouched", sys.path == path_before)
    finally:
        if old is None:
            os.environ.pop("REFRACT_PLUGIN_PATH", None)
        else:
            os.environ["REFRACT_PLUGIN_PATH"] = old
        if base in sys.path:
            sys.path.remove(base)
        for m in [k for k in sys.modules if k.split(".")[0]
                  in ("stars", "planet", "refract_plugins")]:
            del sys.modules[m]
        registry.reload()
        shutil.rmtree(base, ignore_errors=True)
    check("reload() with the root gone restores just the built-ins",
          [e.name for e in registry.REGISTRY] == ["desk", "three60",
                                                  "play", "tak"])


def test_config_load():
    """A broken config.json must not stop boot, and must not be lost."""
    import glob
    import json
    import shutil
    import tempfile
    from refract.core import config
    d = tempfile.mkdtemp(prefix="refract-cfg-")
    saved = config.CONFIG_DIR, config.CONFIG_PATH, config.XRDESK_LEGACY_PATH
    try:
        config.CONFIG_DIR = d
        config.CONFIG_PATH = os.path.join(d, "config.json")
        config.XRDESK_LEGACY_PATH = os.path.join(d, "absent.json")
        with open(config.CONFIG_PATH, "w") as f:
            f.write('{"global": {"imu_rate": 2')          # truncated write
        cfg = config.load()
        check("corrupt config loads as defaults",
              set(cfg) == set(config.SECTIONS)
              and all(v == {} for v in cfg.values()))
        check("corrupt config is kept aside, not overwritten",
              not os.path.exists(config.CONFIG_PATH)
              and len(glob.glob(config.CONFIG_PATH + ".bad-*")) == 1)
        with open(config.CONFIG_PATH, "w") as f:
            json.dump({"desk": [1, 2], "global": {"keep_sbs": True}}, f)
        cfg = config.load()
        check("a non-object section is replaced, the rest kept",
              cfg["desk"] == {} and cfg["global"] == {"keep_sbs": True})
    finally:
        config.CONFIG_DIR, config.CONFIG_PATH, config.XRDESK_LEGACY_PATH = saved
        shutil.rmtree(d, ignore_errors=True)


def test_find_glasses():
    """By EDID only -- an ordinary 1080p monitor is never 'the glasses'."""
    from refract.core.displaymode import find_glasses
    mode = (0, 1920, 1080, 60.0, 1.0, [1.0], {})

    def mon(conn, vendor, product):
        return ((conn, vendor, product, "0"), [mode], {})

    laptop = mon("eDP-1", "LGD", "0x0542")
    desk_monitor = mon("DP-2", "DEL", "DELL U2419H")
    glasses = mon("DP-1", "CVT", "VITURE")
    check("glasses found by EDID",
          find_glasses([laptop, desk_monitor, glasses]) == "DP-1")
    check("a 1080p desk monitor is not mistaken for the glasses",
          find_glasses([laptop, desk_monitor]) is None)


def test_device_prober():
    """The background probe hands over each answer once, and drops one that
    straddled a park/resume (those change the display mode on purpose)."""
    print("device prober")
    from refract.core import handoff
    orig_probe, orig_poll = handoff._probe, handoff.DEVICE_POLL
    gate = [None]
    waiting = [0]               # probes that have started (and snapshotted
                                # app.parked) and are blocked on the gate

    def fake_probe(app):
        waiting[0] += 1
        while gate[0] is None:
            time.sleep(0.001)
        r, gate[0] = gate[0], None
        return r

    handoff._probe = fake_probe
    handoff.DEVICE_POLL = 0.001
    try:
        app = type("A", (), {"parked": False})()
        prober = handoff._Prober(app)
        check("nothing before the first probe finishes", prober.take() is None)
        gate[0] = (True, True)
        deadline = time.time() + 2.0
        got = None
        while got is None and time.time() < deadline:
            got = prober.take()
            time.sleep(0.002)
        check("a finished probe is handed over", got == (True, True))
        check("...exactly once", prober.take() is None)
        # a probe that started unparked, finishing after a park
        while waiting[0] < 2 and time.time() < deadline:
            time.sleep(0.001)
        app.parked = True
        gate[0] = (True, False)
        time.sleep(0.05)
        check("a probe from before a park is discarded",
              prober.take() is None)
    finally:
        handoff._probe, handoff.DEVICE_POLL = orig_probe, orig_poll


def test_imu_aux():
    """The extended report (msgId 0x53): raw gyro + accel, euler moved to
    28, no quaternion. Told apart from the stock report by length -- and
    misreading it as stock is silent (it parses as a 'quaternion' made of
    accel, temperature and euler bytes), so pin both paths down."""
    print("imu aux report")
    import struct

    from refract.core.head import Head
    from refract.core.viture_sdk import AUX_LEN, parse_aux, parse_imu

    gyro, accel, temp = (0.01, -0.02, 0.03), (-0.2285, -0.0193, 0.9719), 28.6
    euler = (-0.9, 17.16, -36.74)
    buf = bytearray(AUX_LEN)
    for off, val in zip(range(0, 40, 4), gyro + accel + (temp,) + euler):
        buf[off:off + 4] = struct.pack(">f", val)
    buf[40:46] = (3089198179).to_bytes(6, "big")
    buf = list(buf)

    e, q = parse_imu(buf)
    check("aux: euler comes from 28/32/36",
          all(abs(e[i] - euler[i]) < 1e-4 for i in range(3)), str(e))
    check("aux: no quaternion is invented", q is None)
    g, a, t = parse_aux(buf)
    check("aux: gyro, accel, temp decode",
          all(abs(g[i] - gyro[i]) < 1e-6 and abs(a[i] - accel[i]) < 1e-6
              for i in range(3)) and abs(t - temp) < 1e-4)
    check("stock report is not mistaken for aux",
          parse_aux(buf[:36]) is None and parse_imu(buf[:36])[1] is not None)

    # Head passes the sample's aux data to the per-sample hook, and keeps it
    got = []
    h = Head()
    h.v = type("V", (), {"last_aux": (g, a, t)})()
    h.on_sample = lambda *args: got.append(args)
    h._on(e, None, 1234, 1)
    check("Head hands aux to on_sample with the sample",
          got and got[0][3] == (g, a, t) and h.aux == (g, a, t))

    # set_aux switches, and drops the stale stock quaternion
    sent = []
    h.v.set_imu_aux = lambda on: sent.append(on) or 0
    h.quat = (0.0, 0.0, 0.0, 1.0)
    check("set_aux switches to it and drops the stale quaternion",
          h.set_aux(True) and sent == [True] and h.aux_mode
          and h.quat is None)
    check("and switches back",
          h.set_aux(False) and sent == [True, False] and not h.aux_mode)

    # the startup countdown asks head.centered; with no quaternion (aux)
    # recentering must still count, or it re-fires on every frame
    h2 = Head()
    h2.euler, h2.quat = (1.0, 2.0, 3.0), None
    check("not centered before a recenter", not h2.centered)
    h2.recenter()
    check("with no quaternion, one recenter is enough",
          h2.centered)


def test_accel_tap():
    """Accelerometer temple taps: a sharp jolt along IMU Y, +Y right temple,
    -Y left. Shapes taken from the worn capture (tap-accel.json): a ~1 g
    one-sample spike ~0.33 s apart, at ~200 Hz, on top of gravity."""
    print("accel tap")
    from refract.core.gesture import AccelTap
    G = (-0.23, -0.02, 0.97)                     # gravity as worn

    def stream(events, secs=4.0, hz=200.0):
        """events: [(t, (dx, dy, dz))] one-sample jolts added to gravity."""
        out, n = [], int(secs * hz)
        for i in range(n):
            t = i / hz
            a = list(G)
            for te, d in events:
                if abs(t - te) < 0.5 / hz:
                    a = [a[k] + d[k] for k in range(3)]
                elif 0 < t - te < 2.5 / hz:        # small ring after the jolt
                    a = [a[k] - 0.2 * d[k] for k in range(3)]
            out.append((t, a))
        return out

    def run(samples, needed=3):
        det = AccelTap(needed=needed)
        return [f for t, a in samples if (f := det.update(t, a))]

    right = [(1.0 + 0.33 * k, (-0.1, 1.1, 0.0)) for k in range(3)]
    left = [(1.0 + 0.33 * k, (-0.1, -1.1, 0.0)) for k in range(3)]
    check("three right-temple taps fire +1", run(stream(right)) == [1])
    check("three left-temple taps fire -1", run(stream(left)) == [-1])
    check("two taps do not fire at needed=3", run(stream(right[:2])) == [])
    check("...but do at needed=2", run(stream(right[:2]), needed=2) == [1])
    check("mixed sides do not fire",
          run(stream(right[:2] + [(1.66, (-0.1, -1.1, 0.0))])) == [])
    slow = [(1.0 + 1.2 * k, (-0.1, 1.1, 0.0)) for k in range(3)]
    check("taps too far apart do not fire", run(stream(slow)) == [])
    # marching: big jolts, but along Z/X -- the worn max was 0.78 g at 10 % Y
    march = [(0.5 + 0.3 * k, (0.3, 0.08, 0.75)) for k in range(10)]
    check("big jolts off the Y axis (marching) never fire",
          run(stream(march)) == [])
    # taking the glasses off: Y-aligned but small (worn max 0.43 g)
    off = [(1.0 + 0.33 * k, (0.0, 0.43, 0.05)) for k in range(3)]
    check("small Y jolts (glasses off/on) never fire", run(stream(off)) == [])
    # a slow tilt -- a head turn changes the gravity vector smoothly
    tilt = [(i / 200.0, (G[0], G[1] + 0.5 * i / 800.0, G[2]))
            for i in range(800)]
    check("a slow tilt toward Y never fires", run(tilt) == [])


def test_launch_guard():
    """A sub-experience whose factory or enter() raises must not take the
    shell down: the half-started scene is removed, home stays, and the
    wearer is told."""
    print("launch guard")
    from refract.core.render import App, Scene
    from refract.shell.registry import SubExperience

    class Fake:
        launch = App.launch
        _launch_failed = App._launch_failed

        def __init__(self):
            self.scenes, self.quit, self.said = [], False, []
            self.status = type("S", (), {
                "set_lines": lambda s, lines, ttl=None:
                    self.said.append(lines)})()

    class Home(Scene):
        name = "home"

    exited = []

    class Explodes(Scene):
        name = "boom"

        def enter(self, app):
            raise RuntimeError("no GPU model")

        def exit(self, app):
            exited.append(True)

    class Fine(Scene):
        name = "fine"

    app = Fake()
    home = Home()
    app.scenes.append(home)
    bad = SubExperience("boom", "Boom", scene_factory=Explodes)
    check("a scene whose enter() raises is reported as not launched",
          app.launch(bad) is False)
    check("...and removed again, with home still running",
          app.scenes == [home] and not app.quit)
    check("...its exit() got a chance to clean up", exited == [True])
    check("...and the wearer is told", app.said
          and "Boom failed to start" in app.said[-1][0])

    def factory_raises():
        raise ImportError("no module named torch")
    check("a factory that raises is survived too",
          app.launch(SubExperience("x", "X", scene_factory=factory_raises))
          is False and app.scenes == [home])
    ok = SubExperience("fine", "Fine", scene_factory=Fine)
    check("a working scene launches on top of home",
          app.launch(ok) and [s.name for s in app.scenes] == ["home", "fine"])
    check("replace=True swaps it, keeping home",
          app.launch(ok, replace=True)
          and [s.name for s in app.scenes] == ["home", "fine"])


def test_follow_easing():
    """Desk's follow easing is per second, not per frame."""
    print("follow easing")
    from refract.desk.scene import follow_alpha
    check("one 60 Hz frame closes 8 % of the gap (xrdesk's feel)",
          abs(follow_alpha(1 / 60) - 0.08) < 1e-9)
    two_halves = 1 - (1 - follow_alpha(1 / 120)) ** 2
    check("two 120 Hz frames close the same gap as one 60 Hz frame",
          abs(two_halves - follow_alpha(1 / 60)) < 1e-9)
    check("no time, no movement", follow_alpha(0.0) == 0.0)


def test_control_socket():
    """refract.ctl <-> the shell over the control socket: every command is
    processed (the old control file kept only the last), the sender gets
    an answer, and a dead socket reads as 'not running'."""
    print("control socket")
    import contextlib
    import io
    import tempfile
    import threading
    from refract import ctl
    from refract.core import render
    from refract.core.control import ControlSocket

    d = tempfile.mkdtemp(prefix="refract-ctl-")
    path = os.path.join(d, "refract.sock")
    cs = ControlSocket(path, legacy_path=os.path.join(d, "refract.ctl"))
    got = []

    def handle(cmd):
        got.append(cmd)
        return cmd != "bogus"

    try:
        check("socket bound and private (0600)",
              cs.ok and (os.stat(path).st_mode & 0o777) == 0o600)
        results, stop = {}, threading.Event()

        def serve():
            while not stop.is_set():
                cs.poll(handle)
                time.sleep(0.005)
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        for cmd in ("park", "resume", "bogus"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                results[cmd] = (ctl.send(cmd, timeout=2.0, path=path),
                                buf.getvalue().strip())
        stop.set()
        th.join(1.0)
        check("commands in quick succession all arrive, in order",
              got == ["park", "resume", "bogus"], str(got))
        check("a known command is answered ok",
              results["park"] == (0, "ok park"), str(results["park"]))
        check("an unknown one is answered as such, exit code 1",
              results["bogus"] == (1, "unknown bogus"))
    finally:
        cs.close()
    check("close removes the socket", not os.path.exists(path))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ctl.send("park", timeout=0.5, path=path)
    check("no listener reads as 'not running'",
          rc == 1 and "not appear to be running" in buf.getvalue())
    check("is_refract_cmdline: python -m refract yes",
          render.is_refract_cmdline(["/x/python", "-m", "refract", "--scene",
                                     "desk"]))
    check("is_refract_cmdline: an editor on a refract file no",
          not render.is_refract_cmdline(["vim", "refract/core/render.py"]))
    check("is_refract_cmdline: refract.ctl itself no",
          not render.is_refract_cmdline(["python", "-m", "refract.ctl",
                                         "handoff"]))
    os.rmdir(d)


def test_head_input():
    """Gesture wiring: taps detected on the IMU thread are acted on in
    poll() -- right toggles the HUD, left recenters -- and the accel
    detector is the one used whenever accelerometer data comes with the
    sample."""
    print("head input")
    from refract.core.headinput import HeadInput

    did = []
    app = type("A", (), {})()
    app.hud = type("H", (), {"toggle": lambda s: did.append("hud")})()
    app.recenter = lambda: did.append("recenter")
    app.head = None                          # no nod detection without a head
    hi = HeadInput({"global": {}})
    hi.pending = 1
    hi.poll(app, 0.0)
    hi.pending = -1
    hi.poll(app, 0.1)
    check("right tap toggles the HUD, left recenters, each once",
          did == ["hud", "recenter"] and hi.pending == 0)
    hi.poll(app, 0.2)
    check("nothing pending, nothing done", did == ["hud", "recenter"])

    # three right-temple jolts through on_sample with accel data -> +1
    g = (-0.23, -0.02, 0.97)
    t = 0
    for k in range(3):
        for i in range(66):                 # ~0.33 s at 200 Hz
            t += 5
            a = (g[0], g[1] + (1.1 if i == 30 else 0.0), g[2])
            hi.on_sample((0.0, 0.0, 0.0), None, t, ((0, 0, 0), a, 30.0))
    check("accel taps arriving with the sample are detected",
          hi.pending == 1, str(hi.pending))
    hi2 = HeadInput({"global": {"temple_tap": False}})
    hi2.on_sample((0.0, 0.0, 0.0), None, 5, ((0, 0, 0), (0, 2.0, 1), 30.0))
    check("temple_tap off: nothing is detected", hi2.pending == 0)

def test_prediction():
    """Motion prediction: extrapolates a turning head, leaves a still one
    alone, and both eyes of a frame share one pose."""
    print("motion prediction")
    from refract.core.head import Head, predict_euler

    check("a still head is not moved by noise-sized velocity",
          predict_euler((1.0, 2.0, 3.0), (1.0, -1.5, 0.5), 0.03)
          == (1.0, 2.0, 3.0))
    p = predict_euler((0.0, 0.0, 10.0), (0.0, 0.0, 100.0), 0.03)
    check("a fast turn is carried forward along its velocity",
          abs(p[2] - 13.0) < 1e-6 and p[0] == 0.0 and p[1] == 0.0, str(p))

    h = Head()
    h.predict_s = 0.03
    # yaw turning at 60 deg/s, samples every 5 ms
    for i in range(40):
        h._track_velocity((0.0, 0.0, 0.3 * i), 1000 + 5 * i)
    vel = h._vel
    check("velocity estimate tracks a steady 60 deg/s turn",
          abs(vel[2] - 60.0) < 3.0 and abs(vel[0]) < 1e-6, str(vel))
    h.euler = (0.0, 0.0, 0.3 * 39)
    pe = h.predicted_euler()
    check("predicted yaw leads the measured one",
          pe[2] > h.euler[2] + 1.5, "%.2f vs %.2f" % (pe[2], h.euler[2]))
    h.predict_s = 0.0
    check("prediction off returns the raw angles", h.predicted_euler()
          == h.euler)
    # yaw wraps from +179 to -179: that is +2 deg, not -358
    h2 = Head()
    for i in range(20):
        h2._track_velocity((0.0, 0.0, (178.0 + 0.2 * i + 180.0) % 360.0 - 180.0),
                           5 * i)
    check("velocity is wrap-aware across +-180",
          abs(h2._vel[2] - 40.0) < 3.0, str(h2._vel))

    # one pose per frame: head_rot() inside render_frame is a snapshot
    from refract.core.render import App
    calls = []

    class FakeApp:
        head_rot = App.head_rot
        sim_rot = None

        def __init__(self):
            self._frame_rot = None
            self.head = type("H", (), {"matrix": lambda s: calls.append(1)
                                       or np.eye(3, dtype="f4") * len(calls)})()
    fa = FakeApp()
    fa._frame_rot = fa.head_rot()
    a, b = fa.head_rot(), fa.head_rot()
    check("within a frame both eyes get the same pose",
          a is b and len(calls) == 1)


def test_sbs_unreachable():
    """If the glasses refuse side-by-side, being in 2D is not a lost
    display -- otherwise park/resume cycles every few seconds."""
    print("sbs unreachable")
    import unittest.mock as mock
    from refract.core import displaymode, handoff
    app = type("A", (), {"windowed": False, "head": object(),
                         "parked": False})()
    mon2d = [(("DP-1", "CVT", "VITURE", "0"),
              [(0, 1920, 1080, 60.0, 1.0, [1.0], {"is-current": True})], {})]
    with mock.patch.object(displaymode, "get_state",
                           return_value=(None, 0, mon2d, [], {})):
        app.sbs_ok = True
        check("2D after SBS had been working: unhealthy (resume will fix)",
              handoff._output_healthy(app) is False)
        app.sbs_ok = False
        check("2D when SBS was never reachable: healthy, no park loop",
              handoff._output_healthy(app) is True)
    with mock.patch.object(displaymode, "get_state",
                           return_value=(None, 0, [], [], {})):
        check("...but a vanished connector is still a loss",
              handoff._output_healthy(app) is False)


def test_keys():
    """GTK key events -> the GLFW-numbered codes every scene compares
    against. Physical keys: Shift+= must still be KEY_EQUAL."""
    print("keys")
    from refract.core import keys as k
    check("letters and digits by name",
          k.from_gdk_name("a") == k.KEY_A and k.from_gdk_name("A") == k.KEY_A
          and k.from_gdk_name("7") == k.KEY_7)
    check("named keys",
          k.from_gdk_name("Escape") == k.KEY_ESCAPE
          and k.from_gdk_name("bracketleft") == k.KEY_LEFT_BRACKET
          and k.from_gdk_name("KP_Enter") == k.KEY_KP_ENTER
          and k.from_gdk_name("Page_Down") == k.KEY_PAGE_DOWN)
    check("F1..F12, and nothing past",
          k.from_gdk_name("F1") == k.KEY_F1
          and k.from_gdk_name("F12") == k.KEY_F12
          and k.from_gdk_name("F13") == k.KEY_UNKNOWN)
    check("unmapped and empty names are KEY_UNKNOWN",
          k.from_gdk_name("Shift_L") == k.KEY_UNKNOWN
          and k.from_gdk_name("") == k.KEY_UNKNOWN
          and k.from_gdk_name(None) == k.KEY_UNKNOWN)

    import gi
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk
    M = Gdk.ModifierType
    check("modifier mask",
          k.mods_from_gdk(M.SHIFT_MASK | M.CONTROL_MASK)
          == k.MOD_SHIFT | k.MOD_CONTROL
          and k.mods_from_gdk(M.ALT_MASK | M.SUPER_MASK)
          == k.MOD_ALT | k.MOD_SUPER
          and k.mods_from_gdk(M(0)) == 0)

    # The HUD's key combos are parsed against these codes
    from refract.shell.hud import parse_combo
    check("HUD combo parses to (mods, key)",
          parse_combo(k, "ctrl+super+r") == (k.MOD_CONTROL | k.MOD_SUPER,
                                             k.KEY_R),
          str(parse_combo(k, "ctrl+super+r")))

    try:
        import glfw
    except ImportError:
        glfw = None
    if glfw is not None:
        names = [n for n in dir(k) if n.startswith(("KEY_", "MOD_",
                                                     "MOUSE_BUTTON_"))
                 or n in ("PRESS", "RELEASE", "REPEAT")]
        bad = [n for n in names if getattr(glfw, n, None) != getattr(k, n)]
        check("every code equals GLFW's (%d)" % len(names), not bad, str(bad))

    # Translating a real event needs the session's keymap
    Gtk = None
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        Gtk = Gtk if Gtk.init_check() else None
    except (ValueError, ImportError):
        pass
    display = Gdk.Display.get_default() if Gtk else None
    if display is None:
        print("    (no display: skipping keymap translation)")
        return

    def keycode(keyval):
        ok, ks = display.map_keyval(keyval)
        return ks[0].keycode if ok and ks else None
    kc = keycode(Gdk.KEY_equal)
    check("the '=' key, shifted or not, is KEY_EQUAL",
          kc is not None
          and k.from_gdk(display, Gdk.KEY_equal, kc) == k.KEY_EQUAL
          and k.from_gdk(display, Gdk.KEY_plus, kc) == k.KEY_EQUAL)
    kc = keycode(Gdk.KEY_h)
    check("Shift+H is KEY_H (the HUD key)",
          kc is not None and k.from_gdk(display, Gdk.KEY_H, kc) == k.KEY_H)
    kc = keycode(Gdk.KEY_Escape)
    check("Escape", kc is not None
          and k.from_gdk(display, Gdk.KEY_Escape, kc) == k.KEY_ESCAPE)


def main():
    test_imu_wire_format()
    test_keys()
    test_imu_aux()
    test_head_math()
    test_head_conventions()
    test_shell_pointer()
    test_head_bob()
    test_temple_tap()
    test_accel_tap()
    test_prediction()
    test_backlight()
    test_vehicle_yaw()
    test_conflicts()
    test_unplug_handoff()
    test_sbs_unreachable()
    test_desk_carousel()
    test_desk_layout()
    test_fastblit()
    test_plugin_discovery()
    test_launch_guard()
    test_control_socket()
    test_head_input()
    test_follow_easing()
    test_config_load()
    test_find_glasses()
    test_device_prober()
    print("\n  %d checks passed" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
