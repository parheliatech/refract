#!/usr/bin/env python3
"""Turn on the glasses' extended IMU report (raw gyro + ACCEL) and record it.

    .venv/bin/python tools/imu-aux-probe.py [--seconds 8] [--out FILE]

The stock report (msg 0x308, what set_imu / msgId 0x15 turns on) carries
orientation only -- euler + quaternion, 36 bytes. The N6PL firmware has a
second mode, "open imu aux" (msgId 0x53, payload 01), that sends msg 0x307
instead: 46 bytes, big-endian floats --

    0-11   gyro  x, y, z     rad/s   (ICM-42688 20-bit FIFO, /262.144 LSB/dps)
    12-23  accel x, y, z     ~g      (raw / 32768 -- unit to be confirmed)
    24-27  temperature       deg C   (raw / 132.48 + 25)
    28-39  euler roll, pitch, yaw    deg (same as the stock report's 0-11)
    40-45  timestamp, 48-bit big-endian

Found by disassembling the IMU report builder (image offset 0x5FC8) and the
0x53 handler (0x20F8) -- see RE-FINDINGS.md. The stock SDK passes any
payload length through to the callback, and its undocumented export
`mcu_with_rsp` sends any msgId, so no firmware change is involved.

Sit still for the first two seconds (that gives the at-rest accel
magnitude, which settles the unit), then tap a temple a few times. At the
end the report is switched back to the stock one (msgId 0x15), and the
process exits hard -- the SDK's own teardown hangs.
"""

import argparse
import ctypes
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.viture_sdk import FQ, Viture   # noqa: E402

MSG_IMU_AUX = 0x53


def be(buf, off):
    return struct.unpack(">f", bytes(buf[off:off + 4]))[0]


def decode_aux(buf):
    return {
        "gyro": [be(buf, o) for o in (0, 4, 8)],
        "accel": [be(buf, o) for o in (12, 16, 20)],
        "temp": be(buf, 24),
        "euler": [be(buf, o) for o in (28, 32, 36)],
        "fw_ts": int.from_bytes(bytes(buf[40:46]), "big") if len(buf) >= 46
        else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--rate", type=int, default=240, choices=sorted(FQ))
    ap.add_argument("--out", help="write every sample here as JSON")
    a = ap.parse_args()

    samples = []

    def on_raw(buf, ts, n):
        samples.append((time.monotonic(), ts, list(buf)))

    v = Viture(quiet=True)
    v.raw_handler = on_raw
    lib = v.lib
    lib.mcu_with_rsp.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte),
                                 ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_void_p]
    lib.mcu_with_rsp.restype = ctypes.c_int
    rc = 0
    try:
        lib.set_imu_fq(FQ[a.rate])
        one = (ctypes.c_ubyte * 1)(1)
        rc = lib.mcu_with_rsp(MSG_IMU_AUX, one, 1, None, None)
        print("  imu aux on   : rc=%d -- sit STILL for 2 s, then tap a "
              "temple a few times" % rc, flush=True)
        time.sleep(a.seconds)
    finally:
        # back to the stock 36-byte report, whatever happened above
        lib.set_imu(True)
        time.sleep(0.2)
        lib.set_imu(False)

    lens = {}
    for _, _, buf in samples:
        lens[len(buf)] = lens.get(len(buf), 0) + 1
    print("  samples      : %d   payload lengths %s" % (len(samples), lens))
    aux = [(t, ts, decode_aux(b)) for t, ts, b in samples if len(b) >= 40]
    if not aux:
        print("  no extended reports arrived -- the 0x53 switch did not take")
        return 1
    span = (aux[-1][1] - aux[0][1]) / 1000.0
    print("  rate         : ~%.0f Hz over %.1f s (SDK ts)"
          % (len(aux) / span if span > 0 else 0, span))

    rest = [d for t, ts, d in aux if ts - aux[0][1] < 1500]
    mag = [math.sqrt(sum(c * c for c in d["accel"])) for d in rest]
    if mag:
        m = sum(mag) / len(mag)
        print("  accel at rest: |a| mean %.4f (min %.4f max %.4f) -- "
              "1.0 means the unit is g" % (m, min(mag), max(mag)))
    first = aux[0][2]
    print("  first sample : gyro %s  accel %s  temp %.1f  euler %s"
          % (["%.4f" % x for x in first["gyro"]],
             ["%.4f" % x for x in first["accel"]], first["temp"],
             ["%.2f" % x for x in first["euler"]]))
    jerk = []
    for (t0, ts0, d0), (t1, ts1, d1) in zip(aux, aux[1:]):
        dt = (ts1 - ts0) / 1000.0
        if dt > 0:
            jerk.append((math.dist(d1["accel"], d0["accel"]) / dt, ts1))
    jerk.sort(reverse=True)
    print("  biggest accel jumps (|da|/dt, at SDK ms): %s"
          % ", ".join("%.1f@%d" % j for j in jerk[:8]))

    if a.out:
        with open(a.out, "w") as f:
            json.dump({"samples": [{"t": t, "ts": ts, "raw": b}
                                   for t, ts, b in samples]}, f)
        print("  wrote        : %s" % a.out)
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        sys.stdout.flush()
    os._exit(code if isinstance(code, int) else 1)
