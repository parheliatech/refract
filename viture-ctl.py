#!/usr/bin/env python3
"""
viture-ctl -- control VITURE XR glasses on Linux x86_64.

Thin ctypes binding over the official VITURE Linux SDK v1.0.7
(sdk/libs/libviture_one_sdk.so). Verified working against
VITURE Pro XR (35ca:101d) on Ubuntu 24.04 / x86_64.

    ./viture-ctl.py status            # device + SDK state
    ./viture-ctl.py imu [seconds]     # live head tracking (default 10s)
    ./viture-ctl.py 3d on|off         # 3840x1080 SBS  <->  1920x1080
    ./viture-ctl.py fq 60|90|120|240  # IMU report rate

No root needed: logind uaccess already grants the login user rw on the
usbfs node and hidraw interfaces.
"""

import ctypes
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIBDIR = os.path.join(HERE, "sdk", "libs")
LIBPATH = os.path.join(LIBDIR, "libviture_one_sdk.so")

# SDK error codes (viture.h)
ERR = {
    0: "SUCCESS", 1: "FAILURE", 2: "INVALID_ARGUMENT", 3: "NOT_ENOUGH_MEMORY",
    4: "UNSUPPORTED_CMD", 5: "CRC_MISMATCH", 6: "VER_MISMATCH",
    7: "MSG_ID_MISMATCH", 8: "MSG_STX_MISMATCH", 9: "CODE_NOT_WRITTEN",
    -1: "WRITE_FAIL", -2: "RSP_ERROR", -3: "TIMEOUT",
}
FQ = {60: 0, 90: 1, 120: 2, 240: 3}
FQ_REV = {v: k for k, v in FQ.items()}

CB_IMU = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_uint8),
                          ctypes.c_uint16, ctypes.c_uint32)
CB_MCU = ctypes.CFUNCTYPE(None, ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint8),
                          ctypes.c_uint16, ctypes.c_uint32)


def be_float(buf, off):
    """SDK reports floats byte-swapped (big-endian) -- see vendor sample."""
    return struct.unpack('>f', bytes(buf[off:off + 4]))[0]


def err(code):
    return "%s(%d)" % (ERR.get(code, "?"), code)


class Viture:
    def __init__(self, quiet=True):
        if not os.path.exists(LIBPATH):
            sys.exit("SDK not found at %s\n"
                     "Expected the official VITURE Linux SDK under sdk/." % LIBPATH)
        # the loader needs the lib dir on the search path before dlopen
        self.lib = ctypes.CDLL(LIBPATH)
        self.lib.init.restype = ctypes.c_bool
        self._quiet = quiet
        self._imu_cb = CB_IMU(self._on_imu)
        self._mcu_cb = CB_MCU(self._on_mcu)
        self.handler = None
        self.count = 0
        if not self.lib.init(self._imu_cb, self._mcu_cb):
            sys.exit("SDK init() failed -- are the glasses plugged in?")

    def _on_imu(self, data, ln, ts):
        self.count += 1
        if self.handler:
            buf = [data[i] for i in range(ln)]
            euler = (be_float(buf, 0), be_float(buf, 4), be_float(buf, 8))
            quat = None
            if ln >= 36:
                quat = tuple(be_float(buf, o) for o in (20, 24, 28, 32))
            self.handler(euler, quat, ts, self.count)

    def _on_mcu(self, msgid, data, ln, ts):
        if not self._quiet:
            print("  [mcu event] msgid=0x%04x len=%d" % (msgid, ln), flush=True)

    # NOTE: deinit() hangs in SDK 1.0.7 -- callers should os._exit() instead.
    def close(self):
        try:
            self.lib.set_imu(False)
        except Exception:
            pass


def cmd_status(v):
    d3 = v.lib.get_3d_state()
    imu = v.lib.get_imu_state()
    fq = v.lib.get_imu_fq()
    print("  SDK              : VITURE Linux SDK 1.0.7 (x86_64)")
    print("  display mode     : %s" % (
        "3840x1080 (3D / side-by-side)" if d3 == 1
        else "1920x1080 (2D)" if d3 == 0 else "error %s" % err(d3)))
    print("  IMU reporting    : %s" % (
        "ON" if imu == 1 else "OFF" if imu == 0 else "error %s" % err(imu)))
    print("  IMU rate         : %s Hz" % FQ_REV.get(fq, "? (raw %s)" % fq))


def cmd_imu(v, secs):
    print("  streaming %ds -- move your head to see values change\n" % secs)

    def show(euler, quat, ts, n):
        if n % 10:
            return
        line = "\r  roll %8.2f  pitch %8.2f  yaw %8.2f" % euler
        if quat:
            line += "   quat % .3f % .3f % .3f % .3f" % quat
        sys.stdout.write(line)
        sys.stdout.flush()

    v.handler = show
    r = v.lib.set_imu(True)
    if r != 0:
        print("  set_imu failed: %s" % err(r))
        return
    t0 = time.time()
    time.sleep(secs)
    n = v.count
    v.lib.set_imu(False)
    print("\n\n  %d samples in %.1fs  (~%.0f Hz)"
          % (n, time.time() - t0, n / max(secs, 1)))


def cmd_3d(v, on):
    before = v.lib.get_3d_state()
    r = v.lib.set_3d(bool(on))
    time.sleep(0.3)
    after = v.lib.get_3d_state()
    print("  set_3d(%s) -> %s" % (bool(on), err(r)))
    print("  display mode: %s -> %s" % (before, after))
    if after == 1:
        print("  now 3840x1080 side-by-side: each eye gets 1920x1080.")
        print("  Ubuntu will see a new mode on the DP output; you may need to")
        print("  re-apply resolution in Display settings.")


def cmd_fq(v, hz):
    if hz not in FQ:
        sys.exit("rate must be one of: %s" % ", ".join(map(str, FQ)))
    r = v.lib.set_imu_fq(FQ[hz])
    print("  set_imu_fq(%d Hz) -> %s   now: %s Hz"
          % (hz, err(r), FQ_REV.get(v.lib.get_imu_fq(), "?")))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = args[0]
    v = Viture()
    print()
    try:
        if cmd == "status":
            cmd_status(v)
        elif cmd == "imu":
            cmd_imu(v, int(args[1]) if len(args) > 1 else 10)
        elif cmd == "3d":
            if len(args) < 2 or args[1] not in ("on", "off"):
                sys.exit("usage: viture-ctl.py 3d on|off")
            cmd_3d(v, args[1] == "on")
        elif cmd == "fq":
            if len(args) < 2:
                sys.exit("usage: viture-ctl.py fq 60|90|120|240")
            cmd_fq(v, int(args[1]))
        else:
            sys.exit("unknown command: %s (try --help)" % cmd)
    finally:
        print()
        v.close()
        sys.stdout.flush()
        os._exit(0)  # SDK 1.0.7 deinit() hangs; exit hard


if __name__ == "__main__":
    main()
