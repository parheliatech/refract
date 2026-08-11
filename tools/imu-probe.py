#!/usr/bin/env python3
"""Determine the VITURE IMU orientation convention -- ONCE, for good.

    .venv/bin/python tools/imu-probe.py [--out imu-probe.json]

The vendor SDK reports eulerRoll/eulerPitch/eulerYaw (viture.h) in the
GLASSES' own frame: VITURE already knows where their IMU sits and has
corrected for it. That mapping is a property of the hardware, identical
session to session and unit to unit -- so it is a constant to be measured
once and baked in, NOT something a user should ever calibrate.

This tool records that constant. You do NOT need to wear the glasses; hold
them in your hands and move them like a head, watching this terminal. Being
able to see the numbers while you move is the whole point -- every previous
attempt at this was done blind inside a headset.

It only records. It changes nothing.
"""

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.head import quat_to_mat                 # noqa: E402
from refract.core.viture_sdk import FQ, Viture            # noqa: E402

# Hold each pose steadily; the analysis wants the STEADY part, so a slow
# deliberate move and a still hold beat a fast waggle.
PHASES = [
    ("rest", 6.0, "hold the glasses LEVEL, lenses facing away from you"),
    ("nose_up", 6.0, "tip them NOSE UP about 30 degrees, and HOLD"),
    ("rest2", 4.0, "back to LEVEL"),
    ("turn_right", 6.0, "turn them RIGHT (nose toward your right) ~45, HOLD"),
    ("rest3", 4.0, "back to LEVEL"),
    ("roll_right", 6.0, "ROLL them clockwise, right lens down ~45, HOLD"),
    ("rest4", 4.0, "back to LEVEL"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="imu-probe.json")
    ap.add_argument("--rate", type=int, default=120, choices=sorted(FQ))
    a = ap.parse_args()

    print(__doc__.split("\n\n")[0])
    print("\n  Quit any running Refract first -- USB access is exclusive.\n")

    try:
        v = Viture(quiet=True)
    except RuntimeError as e:
        sys.exit("  %s\n  (is `python -m refract` still running?)" % e)

    latest = {}

    def on_imu(euler, quat, ts, n):
        latest["euler"] = euler
        latest["quat"] = quat
        latest["n"] = n

    v.handler = on_imu
    v.lib.set_imu_fq(FQ[a.rate])
    if v.lib.set_imu(True) != 0:
        sys.exit("  set_imu failed")

    time.sleep(1.0)
    if not latest:
        sys.exit("  no IMU data arriving")

    log = {"rate": a.rate, "phases": {}}
    print("  recording. euler is (roll, pitch, yaw) in DEGREES per viture.h\n")
    for name, secs, prompt in PHASES:
        print("  >> %-11s %s" % (name.upper(), prompt))
        samples = []
        t0 = time.time()
        last_print = 0.0
        while time.time() - t0 < secs:
            time.sleep(0.02)
            if "euler" not in latest:
                continue
            samples.append({"euler": list(latest["euler"]),
                            "quat": list(latest["quat"] or []),
                            "t": time.time() - t0})
            if time.time() - last_print > 0.4:
                last_print = time.time()
                e = latest["euler"]
                q = latest["quat"] or (0, 0, 0, 1)
                print("     roll %+7.1f  pitch %+7.1f  yaw %+7.1f   "
                      "quat %+.3f %+.3f %+.3f %+.3f   %.0fs left"
                      % (e[0], e[1], e[2], q[0], q[1], q[2], q[3],
                         secs - (time.time() - t0)))
        # keep the steady tail of each hold, where the motion has settled
        log["phases"][name] = samples[len(samples) // 2:]
        print()

    with open(a.out, "w") as f:
        json.dump(log, f)
    print("  wrote %s  (%d phases)" % (a.out, len(log["phases"])))

    # immediate readable summary: what moved, and by how much
    print("\n  summary -- change from rest, in degrees:")
    rest = mean_euler(log["phases"]["rest"])
    print("    %-11s roll %+7.1f  pitch %+7.1f  yaw %+7.1f   (reference)"
          % ("rest", *rest))
    for name in ("nose_up", "turn_right", "roll_right"):
        m = mean_euler(log["phases"][name])
        d = [wrap(m[i] - rest[i]) for i in range(3)]
        dominant = "roll pitch yaw".split()[max(range(3),
                                                key=lambda i: abs(d[i]))]
        print("    %-11s roll %+7.1f  pitch %+7.1f  yaw %+7.1f   -> %s"
              % (name, d[0], d[1], d[2], dominant.upper()))

    v.lib.set_imu(False)
    v.close()
    sys.stdout.flush()
    os._exit(0)      # SDK 1.0.7 deinit() hangs


def wrap(d):
    while d > 180.0:
        d -= 360.0
    while d < -180.0:
        d += 360.0
    return d


def mean_euler(samples):
    """Circular mean per axis -- plain averaging breaks across the wrap."""
    out = []
    for i in range(3):
        xs = [math.radians(s["euler"][i]) for s in samples]
        out.append(math.degrees(math.atan2(
            sum(math.sin(x) for x in xs) / len(xs),
            sum(math.cos(x) for x in xs) / len(xs))))
    return out


if __name__ == "__main__":
    main()
