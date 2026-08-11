"""ctypes binding over the official VITURE Linux SDK v1.0.7.

Extracted from viture-ctl.py (which remains the standalone CLI). This is the
PUBLIC vendor SDK bundled in sdk/ -- deliberately not libglasses.so from the
XRLinuxDriver tree: that one belongs to Breezy and disappears with it
(hardware.py binds it for the controls this SDK lacks).

Verified against VITURE Pro XR (35ca:101d) on Ubuntu 24.04 / x86_64.

Gotchas that are easy to re-lose:
- IMU floats arrive BIG-ENDIAN; byte-swap before unpack (vendor sample does
  the same via its makeFloat helper).
- deinit() hangs in SDK 1.0.7 -- processes must os._exit() instead of
  returning through the interpreter's normal teardown.
"""

import ctypes
import os
import struct

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIBDIR = os.path.join(REPO, "sdk", "libs")
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


def parse_imu(buf):
    """Raw IMU payload -> (euler, quat or None).

    euler is (roll, pitch, yaw) in DEGREES at offsets 0/4/8, per viture.h.
    quat is returned as (x, y, z, w) although the WIRE order is w,x,y,z --
    see the comment in _on_imu. Pulled out as a function so the byte layout
    can be regression-tested without the glasses attached; getting this
    wrong is silent and costs days.
    """
    euler = (be_float(buf, 0), be_float(buf, 4), be_float(buf, 8))
    quat = None
    if len(buf) >= 36:
        w, x, y, z = (be_float(buf, o) for o in (20, 24, 28, 32))
        quat = (x, y, z, w)
    return euler, quat


class Viture:
    """SDK handle. `handler` gets (euler, quat, ts, count) per IMU sample;
    `mcu_handler` gets (msgid, data, ln, ts) per MCU event (glasses buttons
    arrive here -- ids are undocumented, log them and bind what you see).

    Both dispatch dynamically, so they can be (re)assigned after construction
    -- unlike the old viture-ctl monkey-patch, which had to patch the class
    because the C callback bound the method object at __init__ time.
    """

    def __init__(self, quiet=True):
        if not os.path.exists(LIBPATH):
            raise RuntimeError("SDK not found at %s\n"
                               "Expected the official VITURE Linux SDK under "
                               "sdk/." % LIBPATH)
        self.lib = ctypes.CDLL(LIBPATH)
        self.lib.init.restype = ctypes.c_bool
        self._quiet = quiet
        self._imu_cb = CB_IMU(self._on_imu)
        self._mcu_cb = CB_MCU(self._on_mcu)
        self.handler = None
        self.mcu_handler = None
        self.count = 0
        if not self.lib.init(self._imu_cb, self._mcu_cb):
            raise RuntimeError("SDK init() failed -- are the glasses "
                               "plugged in?")

    def _on_imu(self, data, ln, ts):
        self.count += 1
        if self.handler:
            buf = [data[i] for i in range(ln)]
            euler, quat = parse_imu(buf)
            self.handler(euler, quat, ts, self.count)

    def _on_mcu(self, msgid, data, ln, ts):
        if self.mcu_handler:
            self.mcu_handler(msgid, data, ln, ts)
        elif not self._quiet:
            print("  [mcu event] msgid=0x%04x len=%d" % (msgid, ln), flush=True)

    # NOTE: deinit() hangs in SDK 1.0.7 -- callers should os._exit() instead.
    def close(self):
        try:
            self.lib.set_imu(False)
        except Exception:
            pass
