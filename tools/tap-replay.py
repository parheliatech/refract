#!/usr/bin/env python3
"""Replay a temple-tap capture through the tap detectors, offline.

    .venv/bin/python tools/tap-replay.py tap-accel.json [--needed 3]

Takes a JSON written by tools/temple-tap-probe.py and, phase by phase,
reports what each detector would have done: every tap AccelTap accepted
(with its side) and every gesture that FIRED, for AccelTap (accelerometer,
needs a capture made with the extended report) and the older yaw-pulse
TempleTap (orientation only). Control phases (nods, head turns, walking...)
must show no fires; right_x3 / left_x3 should fire once per triple, on the
right side.

2026-10-04, tap-accel.json: AccelTap 13/13 triples on the correct side and
zero false fires; TempleTap 1/13. Re-run this after touching either
detector's gates -- it is the only test made of real taps.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.gesture import AccelTap, TempleTap   # noqa: E402


def side(f):
    return "R" if f > 0 else "L"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--needed", type=int, default=3)
    a = ap.parse_args()
    phases = json.load(open(a.capture))["phases"]

    has_accel = any("accel" in s for v in phases.values()
                    for s in v["samples"][:1])
    print("%-13s  %-26s  %s" % ("phase", "AccelTap taps -> FIRES",
                                "TempleTap FIRES"))
    for ph, v in phases.items():
        s = v["samples"]
        acc = "(no accel in capture)"
        if has_accel:
            det, taps = AccelTap(needed=a.needed), []
            det.debug = lambda ev, kw: (taps.append(kw["sign"])
                                        if ev == "tap" else None)
            fires = [f for x in s
                     if (f := det.update(x["ts"] / 1000.0, x["accel"]))]
            acc = "%2dR %2dL -> %s" % (taps.count(1), taps.count(-1),
                                      " ".join(map(side, fires)) or "-")
        old = TempleTap(needed=a.needed)
        ofires = [f for x in s
                  if (f := old.update(x["ts"] / 1000.0, *x["euler"]))]
        print("%-13s  %-26s  %s" % (ph, acc,
                                    " ".join(map(side, ofires)) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
