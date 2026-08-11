"""Head tracking: VITURE IMU orientation, axis calibration, recentring.

Extracted from xrdesk.py with the hard-won knowledge intact. Read the comments
before "simplifying" anything here -- most of this file is the residue of
wrong assumptions that each cost a wearer session to diagnose.
"""

import math
import time

import numpy as np

from refract.core.viture_sdk import FQ, Viture

# Solved on a head with --calibrate, not reasoned about:
#   up        [ 0.015 -0.953  0.301]  30.1 deg
#   right     [ 0.972  0.021 -0.233]  52.0 deg
#   tiltright [-0.220 -0.185 -0.958]  51.8 deg   det -0.991 -> left-handed
# The SDK reports euler in DEGREES and its quaternion is left-handed against
# OpenGL. Note this is the NEGATIVE of what i3d_vr.py uses: that one rotates a
# sampling ray, this drives a camera, and those want opposite mappings. Do not
# "harmonise" them.
IMU_BASIS = np.array([[0, -1, 0], [-1, 0, 0], [0, 0, 1]], dtype="f4")
IMU_MIRROR = np.diag([1.0, -1.0, 1.0]).astype("f4")

# det(B) < 0 for this headset: the basis that maps its axes onto the camera's
# is a REFLECTION, so conjugating by it negates the rotation angle as well as
# moving the axis. `flip` transposes the result to undo that. This is the whole
# handedness story -- it belongs in the solve, not in a pile of toggles.
DEFAULT_FLIP = True

# Conjugating by a 180 degree rotation reverses exactly TWO axes and leaves the
# third, so these four cover every sign arrangement. Which one is right has to
# be found by wearing the thing -- reasoning about it has failed repeatedly.
SIGN_FIX = [
    ("none", np.diag([1.0, 1.0, 1.0])),
    ("yaw+roll", np.diag([1.0, -1.0, -1.0])),
    ("pitch+roll", np.diag([-1.0, 1.0, -1.0])),
    ("pitch+yaw", np.diag([-1.0, -1.0, 1.0])),
]

# Whether the head rotation is applied to the camera or its inverse is applied
# to the world is a real choice, not a formality: getting it backwards inverts
# every axis at once, which reads as "pitch causes roll" once combined with a
# permuted basis.
INVERT = [("camera", False), ("world", True)]


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
# The euler path -- no calibration, because there is nothing to calibrate.
#
# viture.h documents the IMU payload as eulerRoll/eulerPitch/eulerYaw. Those
# are the GLASSES' angles: VITURE knows where the chip sits in the frame and
# has already removed the mounting rotation. The raw quaternion further down
# the payload is in the IMU's own BODY frame, and a body-frame mount rotation
# does not cancel when you recentre -- it conjugates. That is why the old
# quaternion path needed a basis, why a wrong basis makes pitch bleed into
# roll, and why "calibrate again" never converged: it was solving for a
# hardware constant from three hand-held holds, and getting a different
# answer every time (+0.462, +0.301, -0.040 for the same axis across runs).
#
# Using the euler angles, the axes are decoupled by construction. What is
# left is only the sign and composition convention, which IS a hardware
# constant -- measured once with tools/imu-probe.py and set here.
# Measured on a head, 2026-08-11: with the quaternion decode fixed, yaw and
# roll came out correct and pitch came out INVERTED -- looking up moved the
# view down. So the vendor reports pitch positive nose-DOWN. One sign, and
# it is a property of the hardware, not of a session.
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


class Head:
    """VITURE IMU orientation. Vendor SDK only -- no XR driver involved."""

    def __init__(self, button_msgid=None, mode="euler"):
        # "euler" needs no calibration (see the note above); "quat" is the
        # old basis path, kept so the two can be compared on a head.
        self.mode = mode
        self.euler = None
        self.eref = None
        self.signs = EULER_SIGNS
        self.order = EULER_ORDER
        self.quat = None
        self.qref = None
        self.samples = 0
        self.error = None
        self.v = None
        # The glasses' own buttons arrive as MCU messages. Which id belongs to
        # which button is not documented anywhere we have, so unknown ids are
        # logged once each: press a button, read the id, bind it.
        self.button_msgid = button_msgid
        self.log_all_mcu = False
        self.seen_msgids = {}
        self.button_hits = 0
        # Runtime, so calibration can replace them without a restart -- and the
        # ONLY copy. There used to be a second signfix on the render state,
        # which silently overrode whatever calibration had just solved.
        self.basis = IMU_BASIS.copy()
        self.flip = DEFAULT_FLIP
        self.signfix = 0
        self.invert = False

    def _on_mcu(self, msgid, data, ln, ts):
        try:
            payload = bytes(data[i] for i in range(min(ln, 8))).hex()
        except Exception:
            payload = ""
        # Once per id by default, so the shell stays quiet. That is exactly
        # wrong for DISCOVERY though -- an event that fires when you take the
        # glasses off has already fired at startup, so its repeats are the
        # interesting part and they were being swallowed. log_all_mcu (or
        # tools/mcu-log.py) prints every one.
        if self.log_all_mcu or msgid not in self.seen_msgids:
            self.seen_msgids[msgid] = payload
            print("  [glasses] MCU msgid=0x%04x len=%d data=%s"
                  % (msgid, ln, payload), flush=True)
        if self.button_msgid is not None and msgid == self.button_msgid:
            self.button_hits += 1
            self.recenter()
            print("  recentred (glasses button)", flush=True)

    def start(self):
        try:
            self.v = Viture(quiet=True)
            self.v.handler = self._on
            self.v.mcu_handler = self._on_mcu
            self.v.lib.set_imu_fq(FQ[120])
            if self.v.lib.set_imu(True) != 0:
                self.error = "set_imu failed"
        except BaseException as e:                       # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)

    def _on(self, euler, quat, ts, n):
        if quat:
            self.quat = quat
        if euler:
            self.euler = euler
        self.samples = n

    def set_sbs(self, on=True):
        """Put the glasses into 3840x1080 side-by-side.

        Through the VENDOR SDK (sdk/libs/libviture_one_sdk.so), deliberately
        not through libglasses.so -- that one loads out of the XRLinuxDriver
        tree, which belongs to Breezy and disappears with it.
        """
        if not self.v:
            return -1
        try:
            return self.v.lib.set_3d(bool(on))
        except Exception:
            return -1

    def recenter(self):
        if self.quat:
            self.qref = self.quat
        if self.euler:
            self.eref = self.euler

    def rel(self):
        """Raw rotation since the reference pose, before any convention fixes.
        Calibration solves against this, not against the corrected matrix."""
        if not self.quat or not self.qref:
            return np.eye(3, dtype="f4")
        return quat_to_mat(self.qref).T @ quat_to_mat(self.quat)

    def matrix(self):
        """Head rotation in camera axes.

        `basis` alone is the entire mapping when it is solved by calibration:
        B·rel·Bᵀ sends each measured head axis to its camera axis. The one
        subtlety is handedness -- if B is a reflection (det < 0) the
        conjugation negates the ANGLE as well as mapping the axis, so the
        result must be transposed to undo that. `flip` carries exactly that.

        The old chain applied a mirror, a sign state and an inversion on top,
        which meant every calibration was immediately overwritten by leftover
        guesswork. signfix/invert survive only as manual overrides and default
        to doing nothing.

        In "euler" mode none of that applies: the vendor's angles are already
        in the glasses' frame, so composing them and taking R_ref^T R_cur
        gives the head rotation in camera axes directly. No basis, no
        handedness argument, no calibration.
        """
        if self.mode == "euler":
            if not self.euler or not self.eref:
                return np.eye(3, dtype="f4")
            ref = euler_to_mat(self.eref, self.signs, self.order)
            cur = euler_to_mat(self.euler, self.signs, self.order)
            return np.ascontiguousarray(ref.T @ cur, dtype="f4")
        if not self.quat or not self.qref:
            return np.eye(3, dtype="f4")
        b = self.basis
        r = b @ self.rel() @ b.T
        if self.flip:
            r = r.T.copy()
        if self.signfix:
            s = SIGN_FIX[self.signfix % len(SIGN_FIX)][1].astype("f4")
            r = s @ r @ s.T
        return (r.T.copy() if self.invert else r).astype("f4")

    # Bumped when a stored calibration stops meaning what it used to. v2 is
    # "fitted after the quaternion component order was fixed"; anything
    # earlier was solved against a scrambled quaternion and describes a
    # mapping that does not physically exist, so it must be discarded rather
    # than carried forward.
    SETTINGS_VERSION = 2

    def settings(self):
        return {"v": self.SETTINGS_VERSION,
                "mode": self.mode, "signs": list(self.signs),
                "order": self.order,
                "basis": self.basis.tolist(), "flip": bool(self.flip),
                "signfix": int(self.signfix), "invert": bool(self.invert)}

    def load_settings(self, d):
        # euler settings are hardware constants; a stored copy may override
        # them while the convention is being pinned down, but never silently
        # reintroduces a stale per-session calibration in euler mode
        self.mode = d.get("mode", self.mode)
        if "signs" in d:
            self.signs = tuple(float(s) for s in d["signs"])
        self.order = d.get("order", self.order)
        if int(d.get("v", 1)) < self.SETTINGS_VERSION:
            print("  imu config   : ignoring a pre-fix calibration (it was "
                  "solved against a scrambled quaternion)", flush=True)
            return False
        try:
            self.basis = np.array(d["basis"], dtype="f4")
            # accept the older "mirror" key so an existing config still loads
            self.flip = bool(d.get("flip", d.get("mirror", DEFAULT_FLIP)))
            self.signfix = int(d["signfix"])
            self.invert = bool(d["invert"])
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def stop(self):
        try:
            if self.v:
                self.v.lib.set_imu(False)
                self.v.close()
        except Exception:
            pass


# Guided calibration, prompted ON THE GLASSES. A terminal sequence is useless
# here: the wearer is looking at the headset, not the laptop. Directional HOLDS,
# never oscillations -- swinging back and forth makes the measured axis point
# whichever way you happened to be moving when sampled, so its sign is luck.
CAL_PHASES = [
    ("ready", 12.0, "PUT THE GLASSES ON"),
    ("still", 4.0, "look STRAIGHT AHEAD"),
    ("up", 5.0, "look UP ~30 and HOLD"),
    ("c1", 3.0, "back to centre"),
    ("right", 5.0, "turn RIGHT ~45 and HOLD"),
    ("c2", 3.0, "back to centre"),
    ("tiltright", 5.0, "TILT RIGHT ear down, HOLD"),
]

# where each motion must land in camera axes (x right, y up, -z forward)
CAL_TARGETS = {"up": (1.0, 0.0, 0.0),
               "right": (0.0, -1.0, 0.0),
               "tiltright": (0.0, 0.0, -1.0)}


def solve_basis(ref_q, holds):
    """Measured holds -> (basis, mirror, report). None if the data is unusable."""
    ref = quat_to_mat(ref_q)
    meas, lines = {}, []
    for name in ("up", "right", "tiltright"):
        best, bang = None, 0.0
        for q in holds.get(name, []):
            ax, ang = axis_of(ref.T @ quat_to_mat(q))
            if ang > bang:
                best, bang = ax, ang
        if best is None or bang < 12.0:
            lines.append("%s: only %.0f deg, not enough" % (name, bang))
            continue
        meas[name] = best
        lines.append("%-9s [%6.3f %6.3f %6.3f] %5.1f deg"
                     % (name, best[0], best[1], best[2], bang))
    if len(meas) != 3:
        return None, None, lines

    a_mat = np.column_stack([meas["up"], meas["right"], meas["tiltright"]])
    t_mat = np.column_stack([CAL_TARGETS["up"], CAL_TARGETS["right"],
                             CAL_TARGETS["tiltright"]])
    det = float(np.linalg.det(a_mat))
    b = t_mat @ np.linalg.inv(a_mat)
    lines.append("det %+.3f -> %s-handed" % (det, "R" if det > 0 else "L"))

    # ORTHONORMALISE, do not snap to a permutation.
    #
    # Snapping assumed the IMU's axes differ from the camera's only by which
    # axis is which. They do not: the chip sits at an angle to the optical
    # axis, and the measurements say so -- "look up" came back as
    # [0.015 -0.953 0.301], a ~17 degree tilt that is consistent across
    # calibrations. Snapping discarded exactly that, which is what left pitch
    # bleeding into roll.
    #
    # The nearest orthogonal matrix (polar decomposition via SVD) keeps the
    # mounting angle while removing only the measurement noise.
    u, _, vt = np.linalg.svd(b)
    ortho = u @ vt
    lines.append("orthonormalised (max adj %.3f, mounting tilt kept)"
                 % float(np.abs(b - ortho).max()))
    return ortho.astype("f4"), det < 0, lines


class CalibrationSession:
    """The guided-calibration state machine, extracted from xrdesk's main loop
    so any scene (or the shell) can run it. Call update() every frame; it
    returns prompt lines for the headset HUD until it finishes, then None.
    Result lands in .result: (basis, mirror, report_lines) or (None, None,
    report_lines) on failure.
    """

    def __init__(self, head):
        self.head = head
        self.t0 = time.time()
        self.ref = None
        self.holds = {}
        self.done = False
        self.result = None

    def update(self):
        if self.done:
            return None
        el = time.time() - self.t0
        acc, phase, prompt, left = 0.0, None, "", 0.0
        for nm, dur, txt in CAL_PHASES:
            if el < acc + dur:
                phase, prompt, left = nm, txt, acc + dur - el
                break
            acc += dur
        if phase is None:
            self.done = True
            self.result = solve_basis(self.ref, self.holds)
            return None
        if self.head.quat:
            if phase == "still":
                self.ref = self.head.quat
            elif phase in CAL_TARGETS:
                self.holds.setdefault(phase, []).append(self.head.quat)
        return [prompt, "hold... %.0f" % max(1, round(left))]

    def apply(self):
        """Apply a successful solve to the head. True if applied."""
        b, mirror, _lines = self.result
        if b is None:
            return False
        self.head.basis = b
        self.head.flip = mirror
        self.head.signfix = 0
        self.head.invert = False
        self.head.recenter()
        return True
