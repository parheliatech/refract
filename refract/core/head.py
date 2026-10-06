"""Head tracking: VITURE IMU orientation, recentering, motion prediction.

The sign and axis conventions here were measured on a head, not reasoned
about -- see DEVELOPMENT_PLAN.md before changing any of them.
"""

import math
import time

import numpy as np

from refract.core.viture_sdk import FQ, Viture

# The stock report's quaternion is parsed (quat_to_mat) for the wire-format
# tests and tools/imu-probe.py only; tracking uses the euler angles.


def quat_to_mat(q):
    x, y, z, w = q
    n = (x * x + y * y + z * z + w * w) ** 0.5 or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype="f4")


# ---------------------------------------------------------------------------
# Tracking uses the vendor's euler angles, which are the GLASSES' angles --
# VITURE has already removed the IMU's mounting rotation -- so there is
# nothing to calibrate. (The quaternion is in the IMU's body frame, where the
# mount rotation does not cancel on recenter.) Only the sign and composition
# convention remain, and they are hardware constants, measured with
# tools/imu-probe.py: the vendor reports pitch positive nose-DOWN.
EULER_SIGNS = (1.0, -1.0, 1.0)     # roll, pitch, yaw
EULER_ORDER = "yxz"                # yaw, then pitch, then roll (intrinsic)


def rot_x(t):
    c, s = math.cos(t), math.sin(t)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype="f4")


def rot_y(t):
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype="f4")


def rot_z(t):
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype="f4")


# Motion prediction: what reaches your eyes is a vsync or two old, so render
# where the head WILL be. Velocity is taken over ~30 ms, not between adjacent
# ~5 ms samples (still-head noise would read as ~8 deg/s), then smoothed, and
# the prediction fades out below a few deg/s so a resting head stays steady.
PREDICT_SPAN = 0.030       # s of history the velocity is measured over
PREDICT_TAU = 0.030        # s, smoothing of that velocity
PREDICT_FADE = (3.0, 15.0)  # deg/s: no prediction below, full above
PREDICT_MAX_AGE = 0.050    # s; a stale sample is not extrapolated further


def _wrap180(d):
    return (d + 180.0) % 360.0 - 180.0


def predict_euler(euler, vel, horizon):
    """euler (deg) moved `horizon` seconds along vel (deg/s), with the
    still-head fade applied. Pure, so it can be tested without a head."""
    speed = math.sqrt(sum(v * v for v in vel))
    lo, hi = PREDICT_FADE
    k = min(1.0, max(0.0, (speed - lo) / (hi - lo)))
    k = k * k * (3.0 - 2.0 * k)                    # smoothstep
    return tuple(e + v * horizon * k for e, v in zip(euler, vel))


def euler_to_mat(euler, signs=EULER_SIGNS, order=EULER_ORDER):
    """(roll, pitch, yaw) in DEGREES -> rotation in camera axes.

    Camera axes are x right, y up, -z forward, so pitch turns about X, yaw
    about Y and roll about Z.
    """
    roll, pitch, yaw = (math.radians(euler[i] * signs[i]) for i in range(3))
    parts = {"x": rot_x(pitch), "y": rot_y(yaw), "z": rot_z(roll)}
    out = np.eye(3, dtype="f4")
    for ch in order:
        out = out @ parts[ch]
    return out


def axis_of(rel):
    """Rotation axis (unit) and angle (degrees) of a rotation matrix."""
    ang = np.arccos(np.clip((np.trace(rel) - 1.0) / 2.0, -1.0, 1.0))
    if ang < 1e-6:
        return np.zeros(3), 0.0
    ax = np.array([rel[2, 1] - rel[1, 2],
                   rel[0, 2] - rel[2, 0],
                   rel[1, 0] - rel[0, 1]]) / (2.0 * np.sin(ang))
    return ax, np.degrees(ang)


# Ask the glasses for the extended IMU report (raw accelerometer + gyro) by
# default: it is what makes the temple taps reliable (the orientation-only
# fallback caught 1 tap sequence in 13), and head tracking is identical
# either way. If a pair cannot send it, Head.set_aux() reports that and the
# stock report carries on.
IMU_AUX_DEFAULT = True


class Head:
    """VITURE IMU orientation. Vendor SDK only -- no XR driver involved."""

    def __init__(self, button_msgid=None):
        self.euler = None
        self.eref = None
        self.signs = EULER_SIGNS
        self.order = EULER_ORDER
        self.quat = None                   # stock report only; not used to track
        self.samples = 0
        self.error = None
        self.v = None
        # MCU messages are logged once per id. (The glasses' buttons do not
        # produce any -- the firmware handles them itself.)
        self.button_msgid = button_msgid
        self.log_all_mcu = False
        self.seen_msgids = {}
        self.button_hits = 0
        # Optional: called on the IMU thread for every sample, as
        # (euler, quat, ts, aux) -- aux is (gyro, accel, temp) or None.
        # Taps need the full ~200 Hz stream, not one sample per frame. Keep
        # it cheap and non-blocking -- it runs in the SDK's read thread.
        self.on_sample = None
        self._on_sample_failed = False
        # Extended report (msgId 0x53): raw gyro + accel instead of the
        # quaternion, which tracking does not use.
        self.aux_mode = False
        self.aux = None                    # (gyro, accel, temp) or None
        # Prediction horizon in seconds; 0 = off. See PREDICT_* above.
        self.predict_s = 0.0
        self._hist = []                    # [(ts_s, euler)] last PREDICT_SPAN
        self._vel = (0.0, 0.0, 0.0)        # deg/s, smoothed
        self._pred = None                  # (euler, vel, monotonic arrival)

    def _on_mcu(self, msgid, data, ln, ts):
        try:
            payload = bytes(data[i] for i in range(min(ln, 8))).hex()
        except Exception:
            payload = ""
        # once per id, unless log_all_mcu (--log-mcu) asks for every one
        if self.log_all_mcu or msgid not in self.seen_msgids:
            self.seen_msgids[msgid] = payload
            print("  [glasses] MCU msgid=0x%04x len=%d data=%s"
                  % (msgid, ln, payload), flush=True)
        if self.button_msgid is not None and msgid == self.button_msgid:
            self.button_hits += 1
            self.recenter()
            print("  recentered (glasses button)", flush=True)

    def start(self, rate_hz=240, aux=False):
        # 240 Hz requested (the glasses deliver ~200): taps are 20-40 ms
        # pulses and need every sample
        try:
            self.v = Viture(quiet=True)
            self.v.handler = self._on
            self.v.mcu_handler = self._on_mcu
            self.v.lib.set_imu_fq(FQ.get(rate_hz, FQ[240]))
            self.rate_hz = rate_hz
            if self.v.lib.set_imu(True) != 0:
                self.error = "set_imu failed"
        except BaseException as e:                       # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
        if aux and not self.error:
            self.set_aux(True)

    def set_aux(self, on=True):
        """Switch to (or away from) the extended IMU report. True if the
        glasses are now in the asked-for mode.

        Head tracking is unaffected either way -- the euler angles are in
        both reports. Failure leaves the stock report running.
        """
        if not self.v:
            return False
        try:
            rc = self.v.set_imu_aux(on)
        except Exception as e:                           # noqa: BLE001
            print("  imu aux      : %s" % e, flush=True)
            return False
        if rc != 0:
            print("  imu aux      : switch %s failed (rc=%s)"
                  % ("on" if on else "off", rc), flush=True)
            return False
        self.aux_mode = bool(on)
        if on:
            # the last stock quaternion would otherwise sit there looking
            # current; anything that needs one should see that there is none
            self.quat = None
        else:
            self.aux = None
        return True

    def _on(self, euler, quat, ts, n):
        if quat:
            self.quat = quat
        if euler:
            self.euler = euler
        aux = self.v.last_aux if self.v else None
        if aux:
            self.aux = aux
        if euler:
            self._track_velocity(euler, ts)
        self.samples = n
        if self.on_sample is not None:
            try:
                self.on_sample(euler, quat, ts, aux)
            except Exception as e:                       # noqa: BLE001
                # must not kill the SDK read thread -- but say so, once
                if not self._on_sample_failed:
                    self._on_sample_failed = True
                    print("  on_sample hook raised (gestures may be dead "
                          "this session): %s: %s" % (type(e).__name__, e),
                          flush=True)

    def _track_velocity(self, euler, ts_ms):
        """IMU thread: update the smoothed angular velocity and publish one
        (euler, vel, arrival) snapshot for matrix() to extrapolate from."""
        t = ts_ms / 1000.0
        hist = self._hist
        hist.append((t, euler))
        while len(hist) > 2 and t - hist[1][0] >= PREDICT_SPAN:
            hist.pop(0)
        t0, e0 = hist[0]
        dt = t - t0
        if dt >= PREDICT_SPAN * 0.5:
            raw = tuple(_wrap180(e - e_old) / dt
                        for e, e_old in zip(euler, e0))
            a = min(1.0, (t - hist[-2][0]) / (PREDICT_TAU + 1e-9)) \
                if len(hist) > 1 else 1.0
            self._vel = tuple(v + (r - v) * a for v, r in zip(self._vel, raw))
        self._pred = (euler, self._vel, time.monotonic())

    def predicted_euler(self):
        """Euler angles to render with: extrapolated by predict_s plus the
        age of the newest sample, or the raw angles with prediction off."""
        snap = self._pred
        if not self.predict_s or snap is None:
            return self.euler
        euler, vel, arrived = snap
        age = min(PREDICT_MAX_AGE, max(0.0, time.monotonic() - arrived))
        return predict_euler(euler, vel, self.predict_s + age)

    def set_sbs(self, on=True):
        """Put the glasses into 3840x1080 side-by-side.

        Through the VENDOR SDK (sdk/libs/libviture_one_sdk.so), deliberately
        not through libglasses.so -- that one loads out of the XRLinuxDriver
        tree, which is only there if that driver is installed.
        """
        if not self.v:
            return -1
        try:
            return self.v.lib.set_3d(bool(on))
        except Exception:
            return -1

    def recenter(self):
        if self.euler:
            self.eref = self.euler

    @property
    def centered(self):
        """Has a reference pose been taken? The startup countdown waits on
        this -- not on the quaternion, which the extended report never
        sends."""
        return self.eref is not None

    def matrix(self):
        """Head rotation in camera axes, since the reference pose.

        The vendor's euler angles are already in the glasses' frame (VITURE
        removed the chip's mounting rotation), so composing them and taking
        R_ref^T R_cur gives the head rotation in camera axes directly -- no
        basis, no handedness argument, no calibration. EULER_SIGNS and
        EULER_ORDER are the only conventions, and they are hardware
        constants measured once (tools/imu-probe.py + tools/imu-solve.py).
        """
        if not self.euler or not self.eref:
            return np.eye(3, dtype="f4")
        ref = euler_to_mat(self.eref, self.signs, self.order)
        cur = euler_to_mat(self.predicted_euler(), self.signs, self.order)
        return np.ascontiguousarray(ref.T @ cur, dtype="f4")

    def stop(self):
        try:
            if self.v:
                self.v.lib.set_imu(False)
                self.v.close()
        except Exception:
            pass
