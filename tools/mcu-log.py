#!/usr/bin/env python3
"""Log EVERY MCU message from the glasses, with timing.

    .venv/bin/python tools/mcu-log.py [seconds]

The glasses send MCU events for things the IMU stream does not cover:
buttons, display-mode changes, and possibly wear detection. `get_wear_status`
does not exist in the Linux libglasses, so these events are the only channel
that could tell Refract you have taken the glasses off -- which is what
Display Handoff wants in order to park automatically.

Refract's own logging prints each message id ONCE, to keep the shell quiet.
That is wrong for discovery: the second and later occurrences are exactly
what we need, because an event that fires on wear-off will already have
fired at startup. This prints everything, every time, with a timestamp and
the raw bytes.

Quit any running Refract first -- USB access is exclusive.

Suggested run: start it, then
  - hold still 5 s          (baseline)
  - TAKE THE GLASSES OFF, wait 5 s
  - PUT THEM BACK ON,      wait 5 s
  - repeat twice
and watch which id repeats in step with what you did.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.viture_sdk import FQ, Viture           # noqa: E402


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    try:
        v = Viture(quiet=True)
    except RuntimeError as e:
        sys.exit("  %s\n  (is Refract still running?)" % e)

    t0 = time.time()
    seen = {}

    def on_mcu(msgid, data, ln, ts):
        try:
            payload = bytes(data[i] for i in range(min(ln, 16))).hex()
        except Exception:                                 # noqa: BLE001
            payload = "?"
        seen[msgid] = seen.get(msgid, 0) + 1
        print("  %6.1fs  msgid=0x%04x  len=%-3d data=%-12s  (#%d for this id)"
              % (time.time() - t0, msgid, ln, payload, seen[msgid]),
              flush=True)

    v.mcu_handler = on_mcu
    v.lib.set_imu_fq(FQ[120])
    v.lib.set_imu(True)          # MCU events only flow while the IMU is on

    print(__doc__.split("\n\n")[0])
    print("\n  listening %.0fs -- take the glasses off and on a few times\n"
          % secs)
    try:
        while time.time() - t0 < secs:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    print("\n  summary:")
    if not seen:
        print("    NO MCU messages at all. Wear detection is not available "
              "through this channel.")
    for msgid, n in sorted(seen.items()):
        print("    0x%04x  %d time%s" % (msgid, n, "" if n == 1 else "s"))
    v.lib.set_imu(False)
    v.close()
    sys.stdout.flush()
    os._exit(0)                  # SDK 1.0.7 deinit() hangs


if __name__ == "__main__":
    main()
