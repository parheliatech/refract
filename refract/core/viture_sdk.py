"""ctypes binding over the official VITURE Linux SDK v1.0.7.

Extracted from tools/viture-ctl.py (which remains the standalone CLI). This is the
PUBLIC vendor SDK, which install.sh downloads from VITURE into sdk/ --
deliberately not libglasses.so from the
XRLinuxDriver tree, which is only present if that driver is installed and
goes away with it (hardware.py binds it for the controls this SDK lacks).

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


# The extended report ("imu aux", msgId 0x53). Same rate as the stock one,
# but raw sensor data instead of the quaternion -- see parse_aux() and
# RE-FINDINGS.md. Told apart from the stock report by length alone.
MSG_IMU_AUX = 0x53
AUX_LEN = 46


def parse_imu(buf):
    """Raw IMU payload -> (euler, quat or None).

    euler is (roll, pitch, yaw) in DEGREES. In the stock 36-byte report it
    is at offsets 0/4/8, per viture.h, and the quaternion follows; quat is
    returned as (x, y, z, w) although the WIRE order is w,x,y,z -- see the
    comment in _on_imu. The extended report has no quaternion and carries
    euler at 28/32/36 instead. Pulled out as a function so the byte layout
    can be regression-tested without the glasses attached; getting this
    wrong is silent and costs days.
    """
    if len(buf) >= AUX_LEN:
        return (be_float(buf, 28), be_float(buf, 32), be_float(buf, 36)), None
    euler = (be_float(buf, 0), be_float(buf, 4), be_float(buf, 8))
    quat = None
    if len(buf) >= 36:
        w, x, y, z = (be_float(buf, o) for o in (20, 24, 28, 32))
        quat = (x, y, z, w)
    return euler, quat


def parse_aux(buf):
    """Extended payload -> (gyro, accel, temp_c), or None for a stock one.

    gyro is rad/s and accel is g, both (x, y, z) in the IMU's own axes:
    the firmware scales the ICM-42688's 20-bit FIFO data (gyro /262.144
    LSB/dps, accel /32768, temperature /132.48 + 25). Verified at rest:
    |accel| = 0.9987.
    """
    if len(buf) < AUX_LEN:
        return None
    return (tuple(be_float(buf, o) for o in (0, 4, 8)),
            tuple(be_float(buf, o) for o in (12, 16, 20)),
            be_float(buf, 24))


class Viture:
    """SDK handle. `handler` gets (euler, quat, ts, count) per IMU sample;
    `mcu_handler` gets (msgid, data, ln, ts) per MCU event (glasses buttons
    arrive here -- ids are undocumented, log them and bind what you see).

    Both dispatch dynamically, so they can be (re)assigned after construction
    -- unlike a monkey-patch, which would have to patch the class
    because the C callback bound the method object at __init__ time.
    """

    def __init__(self, quiet=True):
        if not os.path.exists(LIBPATH):
            raise RuntimeError("SDK not found at %s\n"
                               "Run ./install.sh -- it downloads the official "
                               "VITURE Linux SDK into sdk/." % LIBPATH)
        self.lib = ctypes.CDLL(LIBPATH)
        self.lib.init.restype = ctypes.c_bool
        self._quiet = quiet
        self._imu_cb = CB_IMU(self._on_imu)
        self._mcu_cb = CB_MCU(self._on_mcu)
        self.handler = None
        # Optional: gets (raw_bytes_list, ts, count) for every IMU sample,
        # before parse_imu. Only the probes use it -- to see the whole
        # payload, including the bytes viture.h calls "reserved".
        self.raw_handler = None
        self.mcu_handler = None
        self.count = 0
        # (gyro, accel, temp) from the current sample when the extended
        # report is on, else None. Set before `handler` runs, on the same
        # thread, so a handler can read it as part of the same sample.
        self.last_aux = None
        if not self.lib.init(self._imu_cb, self._mcu_cb):
            raise RuntimeError("SDK init() failed -- are the glasses "
                               "plugged in?")

    def _on_imu(self, data, ln, ts):
        self.count += 1
        if self.handler or self.raw_handler:
            buf = [data[i] for i in range(ln)]
            if self.raw_handler:
                self.raw_handler(buf, ts, self.count)
            self.last_aux = parse_aux(buf)
            if self.handler:
                euler, quat = parse_imu(buf)
                self.handler(euler, quat, ts, self.count)

    def _on_mcu(self, msgid, data, ln, ts):
        if self.mcu_handler:
            self.mcu_handler(msgid, data, ln, ts)
        elif not self._quiet:
            print("  [mcu event] msgid=0x%04x len=%d" % (msgid, ln), flush=True)

    def set_imu_aux(self, on=True):
        """Switch IMU reporting to the extended report (on) or back to the
        stock one (off). Either way the IMU stays on. Returns the SDK rc.

        Neither viture.h nor the SDK's own functions know about msgId 0x53;
        it goes out through mcu_with_rsp, which the library exports without
        documenting -- (msgId, data, len, rsp**, rsplen*), the same call
        set_imu makes internally with 0x15.
        """
        if not on:
            return self.lib.set_imu(True)
        f = self.lib.mcu_with_rsp
        f.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint8),
                      ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        f.restype = ctypes.c_int
        return f(MSG_IMU_AUX, (ctypes.c_uint8 * 1)(1), 1, None, None)

    # NOTE: deinit() hangs in SDK 1.0.7 -- callers should os._exit() instead.
    def close(self):
        try:
            self.lib.set_imu(False)
        except Exception:
            pass
