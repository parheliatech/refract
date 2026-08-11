#!/usr/bin/env python3
"""Derive the IMU euler convention from an imu-probe.json recording.

    .venv/bin/python tools/imu-solve.py imu-probe.json

Searches the whole (composition order x sign) space -- 48 candidates -- and
scores each against the recorded motions: nose-up must come back as a
rotation about camera +X and nothing else, turn-right about +Y, roll-right
about +Z. The winner is a HARDWARE CONSTANT: paste it into
refract/core/head.py as EULER_SIGNS / EULER_ORDER and no one calibrates
anything ever again.

Exhaustive search rather than reasoning, because reasoning about this has
failed repeatedly -- and unlike the old three-hold calibration this is
scored against data, so a bad recording FAILS loudly instead of quietly
fitting noise.
"""

import argparse
import itertools
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.head import axis_of, euler_to_mat       # noqa: E402

ORDERS = ["".join(p) for p in itertools.permutations("xyz")]
SIGNSETS = list(itertools.product((1.0, -1.0), repeat=3))

# what each recorded motion must come out as, in camera axes
WANT = {"nose_up": (1.0, 0.0, 0.0),        # pitch about +X
        "turn_right": (0.0, -1.0, 0.0),    # yaw about -Y
        "roll_right": (0.0, 0.0, -1.0)}    # roll about -Z


def mean_euler(samples):
    out = []
    for i in range(3):
        xs = [math.radians(s["euler"][i]) for s in samples]
        out.append(math.degrees(math.atan2(
            sum(math.sin(x) for x in xs) / len(xs),
            sum(math.cos(x) for x in xs) / len(xs))))
    return out


def score(log, order, signs):
    """Worst-case angular error, in degrees, across the three motions."""
    rest = mean_euler(log["phases"]["rest"])
    ref = euler_to_mat(rest, signs, order)
    worst, detail = 0.0, {}
    for name, want in WANT.items():
        if name not in log["phases"] or not log["phases"][name]:
            return 999.0, {}
        cur = euler_to_mat(mean_euler(log["phases"][name]), signs, order)
        ax, ang = axis_of(np.asarray(ref).T @ np.asarray(cur))
        if ang < 8.0:
            return 999.0, {}          # motion too small to judge
        err = math.degrees(math.acos(
            max(-1.0, min(1.0, float(np.dot(ax, want))))))
        detail[name] = (ax, ang, err)
        worst = max(worst, err)
    return worst, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default="imu-probe.json")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args()

    with open(a.log) as f:
        log = json.load(f)

    results = []
    for order in ORDERS:
        for signs in SIGNSETS:
            worst, detail = score(log, order, signs)
            results.append((worst, order, signs, detail))
    results.sort(key=lambda r: r[0])

    if results[0][0] > 900:
        sys.exit("  no candidate fits -- the holds were too small or the "
                 "recording is missing phases. Re-run tools/imu-probe.py and "
                 "move the glasses further (30-45 degrees) in each pose.")

    print("  best candidates (worst-axis error, lower is better):\n")
    for worst, order, signs, detail in results[:a.top]:
        print("    order %s  signs %+d %+d %+d   worst error %5.1f deg"
              % (order, signs[0], signs[1], signs[2], worst))
    best_worst, order, signs, detail = results[0]
    print("\n  winner: order=%r signs=%s\n" % (order, tuple(signs)))
    for name, (ax, ang, err) in detail.items():
        print("    %-11s moved %5.1f deg about [%+.3f %+.3f %+.3f]  "
              "err %4.1f deg" % (name, ang, ax[0], ax[1], ax[2], err))

    runner_up = next((r[0] for r in results[1:] if r[0] > best_worst + 1e-6),
                     999.0)
    print("\n  margin over the next distinct candidate: %.1f deg"
          % (runner_up - best_worst))
    if best_worst > 20.0:
        print("\n  WARNING: even the best fit is %.0f deg off. The recording "
              "is probably contaminated -- each pose should move ONE way "
              "only." % best_worst)
    else:
        print("\n  Paste into refract/core/head.py:")
        print("    EULER_SIGNS = (%.1f, %.1f, %.1f)" % signs)
        print("    EULER_ORDER = %r" % order)
    return 0


if __name__ == "__main__":
    sys.exit(main())
