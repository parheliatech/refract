#!/usr/bin/env python3
"""
imu_calib -- work out the VITURE IMU's axis convention from actual motion.

Guessing has cost us two rounds already. The euler triple's element order is
not what viture-ctl.py's printout claims, and per-axis subtraction cannot
recentre an orientation whose rest pose is pitch ~126 degrees -- a single head
movement smears across several euler components.

So: record raw euler AND quaternion while the wearer performs three named
motions, then report which components actually moved, and the rotation axis of
the quaternion delta for each. That axis, expressed in the rest frame, IS the
answer -- no convention needs to be assumed.

    ./imu_calib.py                  # prompts through the sequence
    ./imu_calib.py --analyse out.npz

Stops xr-driver for exclusive USB access and restarts it afterwards.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# Directional HOLDS, not oscillations. Swinging back and forth makes the
# measured rotation axis point whichever way you happened to be moving at the
# sampled instant, so the sign is luck -- which is why the first attempt
# produced three identical axes. Holding a known direction fixes the sign.
PHASES = [
    ("still", 4.0, "look STRAIGHT AHEAD and hold"),
    ("right", 5.0, "turn to look RIGHT about 45 deg, and HOLD"),
    ("centre1", 3.0, "back to straight ahead"),
    ("up", 5.0, "look UP about 30 deg, and HOLD"),
    ("centre2", 3.0, "back to straight ahead"),
    ("tiltright", 5.0, "TILT your RIGHT ear toward your shoulder, and HOLD"),
]

# Where each of those motions must end up in camera axes (x right, y up, -z
# forward): turning right is a rotation about -Y, looking up about +X, tilting
# the right ear down about -Z.
TARGETS = {"up": (1.0, 0.0, 0.0),
           "right": (0.0, -1.0, 0.0),
           "tiltright": (0.0, 0.0, -1.0)}


def load_viture():
    path = os.path.join(os.path.dirname(HERE), "viture-ctl.py")
    spec = importlib.util.spec_from_file_location("viture_ctl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def quat_mat(q, order):
    """Unit quaternion -> rotation matrix."""
    x, y, z, w = q if order == "xyzw" else (q[1], q[2], q[3], q[0])
    n = math_sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=float)


def math_sqrt(v):
    return v ** 0.5


def axis_of(rel):
    """Rotation axis (unit) and angle (deg) of a rotation matrix."""
    ang = np.arccos(np.clip((np.trace(rel) - 1.0) / 2.0, -1.0, 1.0))
    if ang < 1e-6:
        return np.zeros(3), 0.0
    ax = np.array([rel[2, 1] - rel[1, 2],
                   rel[0, 2] - rel[2, 0],
                   rel[1, 0] - rel[0, 1]]) / (2.0 * np.sin(ang))
    return ax, np.degrees(ang)


def record(args):
    mod = load_viture()
    was_up = subprocess.run(["pgrep", "-x", "xrDriver"],
                            capture_output=True).returncode == 0
    if was_up:
        subprocess.run(["systemctl", "--user", "stop", "xr-driver"],
                       capture_output=True, timeout=25)
        time.sleep(1.5)

    v = mod.Viture()
    samples = []
    phase = {"name": "idle"}

    def on(euler, quat, ts, n):
        samples.append((phase["name"], time.time(), euler, quat))

    v.handler = on
    if v.lib.set_imu(True) != 0:
        sys.exit("set_imu failed")

    try:
        print("\n  Follow the prompts. Movements should be SLOW and clear.\n")
        for name, secs, prompt in PHASES:
            for c in range(3, 0, -1):
                sys.stdout.write("\r  next: %-42s in %d " % (prompt, c))
                sys.stdout.flush()
                time.sleep(1.0)
            phase["name"] = name
            t0 = time.time()
            while time.time() - t0 < secs:
                sys.stdout.write("\r  >>> %-46s %.1fs  "
                                 % (prompt, secs - (time.time() - t0)))
                sys.stdout.flush()
                time.sleep(0.1)
            phase["name"] = "idle"
        print("\n\n  done, %d samples" % len(samples))
    finally:
        v.lib.set_imu(False)
        try:
            v.close()
        except Exception:
            pass
        if was_up:
            subprocess.run(["systemctl", "--user", "start", "xr-driver"],
                           capture_output=True, timeout=25)

    names = np.array([s[0] for s in samples])
    eul = np.array([s[2] for s in samples], dtype=float)
    quat = np.array([s[3] if s[3] else (0, 0, 0, 1) for s in samples],
                    dtype=float)
    np.savez(args.out, names=names, euler=eul, quat=quat)
    print("  wrote %s" % args.out)
    return args.out


def analyse(path, order):
    d = np.load(path)
    names, eul, quat = d["names"], d["euler"], d["quat"]
    ref_idx = np.flatnonzero(names == "still")
    if not len(ref_idx):
        sys.exit("no 'still' phase recorded")
    ref = quat_mat(quat[ref_idx[len(ref_idx) // 2]], order)

    print("\n  quaternion order assumed: %s" % order)
    meas = {}
    for name in ("up", "right", "tiltright"):
        sel = np.flatnonzero(names == name)
        if not len(sel):
            print("  %-10s MISSING" % name)
            continue
        bax, bang = None, 0.0
        for i in sel:
            ax, ang = axis_of(ref.T @ quat_mat(quat[i], order))
            if ang > bang:
                bax, bang = ax, ang
        meas[name] = bax
        flag = "" if bang > 12 else "   <-- too small, motion not registered"
        print("  %-10s axis [%6.3f %6.3f %6.3f]  %5.1f deg%s"
              % (name, bax[0], bax[1], bax[2], bang, flag))

    if len(meas) != 3:
        sys.exit("\n  need all three holds to solve the basis")

    a_mat = np.column_stack([meas["up"], meas["right"], meas["tiltright"]])
    t_mat = np.column_stack([TARGETS["up"], TARGETS["right"],
                             TARGETS["tiltright"]])

    print("\n  orthogonality (should be close to identity):")
    for row in a_mat.T @ a_mat:
        print("    [%6.3f %6.3f %6.3f]" % tuple(row))

    det = np.linalg.det(a_mat)
    b = t_mat @ np.linalg.inv(a_mat)
    print("\n  det(measured) = %+.3f  ->  IMU frame is %s-handed relative to"
          " the camera" % (det, "RIGHT" if det > 0 else "LEFT"))
    print("  paste this into i3d_vr.py BASES, and set mirror = %s:\n"
          % (det < 0))
    print("    np.array([[%5.2f, %5.2f, %5.2f],\n"
          "              [%5.2f, %5.2f, %5.2f],\n"
          "              [%5.2f, %5.2f, %5.2f]], dtype=\"f4\")"
          % tuple(np.round(b, 2).ravel()))


def main():
    ap = argparse.ArgumentParser(description="IMU axis calibration")
    ap.add_argument("--out", default=os.path.join(HERE, "imu_calib.npz"))
    ap.add_argument("--analyse", metavar="NPZ")
    ap.add_argument("--quat-order", default="xyzw", choices=["xyzw", "wxyz"])
    a = ap.parse_args()
    recorded = not a.analyse
    path = a.analyse or record(a)
    analyse(path, a.quat_order)
    # os._exit skips stdout flushing, so piping this anywhere silently ate the
    # entire analysis. Only the SDK path needs the hard exit (1.0.7 deinit
    # hangs); flush first either way.
    sys.stdout.flush()
    if recorded:
        os._exit(0)


if __name__ == "__main__":
    main()
