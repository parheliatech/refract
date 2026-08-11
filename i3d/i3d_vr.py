#!/usr/bin/env python3
"""
i3d_vr -- 360 / VR180 playback on the VITURE glasses, with head tracking.

Note what this is NOT: VR footage is already stereoscopic, so the i3d depth
pipeline has nothing to do here. The work is projection (equirect / fisheye)
plus orientation -- one perspective camera per eye, sampling the source, driven
by the glasses' IMU.

    ./i3d_vr.py testpat/equirect_sbs.mp4 --windowed
    ./i3d_vr.py clip.mp4 --monitor DP-2 --projection fisheye180 --layout sbs
    ./i3d_vr.py testpat/equirect_sbs.png --yaw 90 --capture out.png

Projection and layout are guessed from the filename and aspect ratio; override
when the guess is wrong. Stills work as well as video, which is what makes the
projection testable.

Head tracking uses the VITURE SDK's IMU. USB access is exclusive, so the
xr-driver service is stopped while tracking and restarted on exit.

Keys:  arrows/drag look   r recenter   space pause   [ ] fov
       i toggle IMU       s screenshot   ESC/q quit
"""

import argparse
import ctypes
import importlib.util
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from i3d_video import Decoder, SENTINEL, probe        # noqa: E402
from i3d_view import pick_monitor                     # noqa: E402

FRAG_VR = os.path.join(HERE, "shaders", "vr", "vr_projection.glsl")
VERT_VR = """
#version 330 core
in vec2 aPos;
out vec2 vNDC;
void main() {
    vNDC = aPos;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

PROJ = {"equirect": 0, "equirect360": 0, "equirect180": 1, "hequirect": 1,
        "fisheye180": 2, "fisheye": 2}


def guess_format(path, w, h):
    """(projection, layout) from filename tags and aspect ratio.

    Nothing in a plain mp4 states this. Real VR files usually say so in the
    name -- and the aspect ratio settles the rest, because the layouts have
    unmistakable shapes: 360 mono is 2:1, SBS 360 is 4:1, over-under 360 is
    1:1, and VR180 SBS is 2:1 made of two squares.
    """
    n = os.path.basename(path).lower()
    layout = ("tb" if any(t in n for t in ("_tb", "-tb", "overunder",
                                           "over_under", "ou_"))
              else "sbs" if any(t in n for t in ("sbs", "_lr", "-lr", "side"))
              else None)
    proj = ("fisheye180" if "fisheye" in n
            else "equirect180" if ("vr180" in n or "180" in n)
            else "equirect" if ("360" in n or "equirect" in n)
            else None)

    ar = w / float(h) if h else 2.0

    # Aspect ratio alone is not enough. A 1920x1080 VR180 SBS file -- an
    # extremely common streaming shape -- is 16:9 like any flat video, and the
    # ratio test calls it mono. So LOOK at a frame: the two eyes of a stereo
    # pair are near-copies of each other, and a fisheye circle leaves the
    # corners black. Both are trivial to measure and neither can be faked by
    # the container.
    probe = probe_layout(path) if (layout is None or proj is None) else {}
    if layout is None and probe:
        layout = probe["layout"]
    if layout is None:
        layout = "sbs" if ar > 3.0 else "tb" if ar < 1.2 else "mono"

    if proj is None:
        eye_ar = (ar / 2.0 if layout == "sbs" else
                  ar * 2.0 if layout == "tb" else ar)
        if probe.get("fisheye"):
            proj = "fisheye180"
        else:
            # a 2:1 eye is a full sphere; a squarish eye is a 180 hemisphere
            proj = "equirect" if eye_ar > 1.6 else "equirect180"
    return proj, layout


def probe_layout(path, at=None):
    """Decide stereo layout by looking at one frame. {} if it cannot."""
    tmp = os.path.join("/tmp", "i3d_layout_probe.pgm")
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if at:
        cmd += ["-ss", str(at)]
    cmd += ["-i", path, "-frames:v", "1", "-vf", "scale=480:270",
            "-pix_fmt", "gray", tmp]
    try:
        if subprocess.run(cmd, capture_output=True, timeout=60).returncode:
            return {}
        from PIL import Image
        im = np.asarray(Image.open(tmp).convert("L")).astype(float)
        os.unlink(tmp)
    except Exception:
        return {}

    def corr(a, b):
        a = a.ravel() - a.mean()
        b = b.ravel() - b.mean()
        return float((a * b).sum()
                     / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))

    h, w = im.shape
    lr = corr(im[:, :w // 2], im[:, w // 2:])
    tb = corr(im[:h // 2, :], im[h // 2:, :])
    k = max(8, min(h, w) // 8)
    corners = np.mean([im[:k, :k].mean(), im[:k, -k:].mean(),
                       im[-k:, :k].mean(), im[-k:, -k:].mean()])
    # Require a clear MARGIN, and decline when there isn't one. Symmetric
    # content correlates strongly both ways -- a synthetic mono test clip
    # scored 0.98 on each axis, and picking the winner of that tie called a
    # mono 360 file over-under. Ambiguous means fall back to aspect ratio,
    # not guess.
    layout = None
    if lr > 0.5 and lr > tb + 0.15:
        layout = "sbs"
    elif tb > 0.5 and tb > lr + 0.15:
        layout = "tb"
    return {"layout": layout, "lr": lr, "tb": tb,
            "fisheye": bool(corners < 16.0)}


def layout_uv(layout, eye):
    """(scale, offset) selecting one eye's half of the frame. eye 0 = left."""
    if layout == "sbs":
        return (0.5, 1.0), (0.5 * eye, 0.0)
    if layout == "tb":
        return (1.0, 0.5), (0.0, 0.5 * eye)
    return (1.0, 1.0), (0.0, 0.0)


def rot_matrix(yaw, pitch, roll):
    """Camera->world for yaw/pitch/roll in DEGREES.

    +yaw looks right, +pitch looks up, +roll tilts the horizon clockwise.
    Verified against vr_testpattern.py: at yaw=+90 the RIGHT block must sit in
    the centre of the view.
    """
    y, p, r = (math.radians(v) for v in (yaw, pitch, roll))
    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)
    # note the sign: with +sy here, +yaw swings the camera to the LEFT, because
    # the camera looks down -Z. Caught by vr_testpattern.py, which is the only
    # reason it is right.
    ry = np.array([[cy, 0, -sy], [0, 1, 0], [sy, 0, cy]], dtype="f4")
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], dtype="f4")
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]], dtype="f4")
    return ry @ rx @ rz


# Axis SIGNS, separate from the axis permutation. Conjugating by a 180 degree
# rotation reverses exactly two axes and leaves the third -- and those three
# rotations plus the identity are the only options (reversing a single axis
# would be a reflection, which is what `mirror` handles). So four states cover
# every sign arrangement, and the wearer can cycle them with 'f' instead of
# waiting for a relaunch per guess.
SIGN_FIX = [
    ("none", np.diag([1.0, 1.0, 1.0])),
    ("flip yaw+roll", np.diag([1.0, -1.0, -1.0])),
    ("flip pitch+roll", np.diag([-1.0, 1.0, -1.0])),
    ("flip pitch+yaw", np.diag([-1.0, -1.0, 1.0])),
]


def quat_to_mat(q, order="xyzw"):
    """Unit quaternion -> rotation matrix (body -> world)."""
    x, y, z, w = q if order == "xyzw" else (q[1], q[2], q[3], q[0])
    n = (x * x + y * y + z * z + w * w) ** 0.5 or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype="f4")


# How the IMU's body axes sit relative to the camera's (x right, y up, -z
# forward) is a fixed property of how the chip is mounted, and it is not
# documented anywhere we have. Rather than guess again, carry the plausible
# mountings and let the wearer cycle them live with 'b' -- each is a two second
# test, and only one makes pan, nod and tilt all behave.
BASES = [
    # SOLVED from measurement (--calibrate), not guessed. Held rotations gave:
    #   look up    -> IMU axis [ 0.145 -0.989 -0.040]   ~ -Y
    #   turn right -> IMU axis [ 0.999 -0.048  0.024]   ~ +X
    #   tilt right -> IMU axis [ 0.224 -0.136 -0.965]   ~ -Z
    # det -0.946, so the IMU frame is LEFT-handed relative to the camera and
    # `mirror` must stay on. Snapped to the exact permutation those imply.
    # ...then corrected once more from wearing it: yaw AND pitch came out
    # reversed while roll was right, which is a 180 degree rotation about the
    # roll axis -- compose with diag(-1,-1,1). Two axes flipping together is
    # what you get when the reference holds were taken in the opposite
    # direction to the prompt, so the solve was self-consistent but mirrored.
    ("calibrated (VITURE Pro XR)",
     np.array([[0, 1, 0], [1, 0, 0], [0, 0, 1]], dtype="f4")),
    ("calibrated, pre-correction",
     np.array([[0, -1, 0], [-1, 0, 0], [0, 0, 1]], dtype="f4")),
    ("cycle x->y->z->x",
     np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]], dtype="f4")),
    ("identity", np.eye(3, dtype="f4")),
    ("z-up -> y-up", np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype="f4")),
    ("z-up -> y-up, flipped",
     np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype="f4")),
    ("x-fwd -> -z-fwd",
     np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], dtype="f4")),
    ("x-fwd y-left z-up",
     np.array([[0, -1, 0], [0, 0, 1], [-1, 0, 0]], dtype="f4")),
    ("x-fwd y-right z-up",
     np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], dtype="f4")),
    ("y-fwd", np.array([[1, 0, 0], [0, 0, -1], [0, -1, 0]], dtype="f4")),
    ("180 about up", np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]], dtype="f4")),
]


class HeadTracker(threading.Thread):
    """VITURE IMU -> yaw/pitch/roll.

    The SDK claims the USB device exclusively, so xr-driver has to be stopped
    for the duration -- the same dance viture-hw.py does.
    """

    def __init__(self, stop, rate=120, signs=(-1.0, 1.0, -1.0)):
        super().__init__(daemon=True)
        self.stop = stop
        self.rate = rate
        self.signs = signs         # (yaw, pitch, roll) -- see orientation()
        self.euler = None          # degrees, raw; element order is not obvious
        self.quat = None
        self.qref = None           # orientation at recentre
        self.mirror = True         # SDK quaternion is left-handed; see matrix()
        # 1 = flip yaw+roll: measured on the head. Pitch tracked correctly
        # while turning right panned left and rolling right rolled left.
        self.signfix = 1
        self.zero = None           # recenter offset
        self.samples = 0
        self.error = None
        self.v = None
        self.driver_was_up = False

    def _load_ctl(self):
        path = os.path.join(os.path.dirname(HERE), "viture-ctl.py")
        spec = importlib.util.spec_from_file_location("viture_ctl", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def run(self):
        try:
            mod = self._load_ctl()
            if subprocess.run(["pgrep", "-x", "xrDriver"],
                              capture_output=True).returncode == 0:
                self.driver_was_up = True
                subprocess.run(["systemctl", "--user", "stop", "xr-driver"],
                               capture_output=True, timeout=25)
                time.sleep(1.5)
            self.v = mod.Viture()
            self.v.handler = self._on_sample
            if self.rate in mod.FQ:
                self.v.lib.set_imu_fq(mod.FQ[self.rate])
            rc = self.v.lib.set_imu(True)
            if rc != 0:
                self.error = "set_imu failed: %s" % mod.err(rc)
                return
            while not self.stop.is_set():
                time.sleep(0.1)
        except BaseException as e:            # noqa: BLE001 -- report, not die
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.shutdown()

    def _on_sample(self, euler, quat, ts, n):
        self.euler = euler
        self.quat = quat
        self.samples = n
        if self.zero is None:
            # Rest orientation is nowhere near zero -- measured 33 / 126 / -75
            # just sitting on a desk. Without an immediate recentre the first
            # frame points somewhere absurd.
            self.zero = euler
        # NOTE: deliberately no auto-recentre here. It used to fire on the
        # first sample -- which arrives while the glasses are still on the desk
        # or in your hand, making the reference pose arbitrary and the view
        # start upside down. main() recentres on a countdown instead, once the
        # wearer has had time to put them on and look forward.
        pass

    def recenter(self):
        if self.euler:
            self.zero = self.euler
        if self.quat:
            self.qref = self.quat

    def matrix(self, basis, order="xyzw"):
        """Head rotation relative to the recentre pose, in camera axes.

        This is the part euler could never do. Subtracting euler angles is not
        a valid way to remove a reference orientation, and with a rest pose of
        pitch ~126 degrees the error is enormous -- it showed up as the picture
        sitting at a 45 degree cant. Composing rotations properly,
        R_ref^T * R_current, is identity at the recentre pose by construction,
        so the view starts upright no matter how the glasses are sitting.
        """
        if not self.quat or not self.qref:
            return np.eye(3, dtype="f4")
        rel = quat_to_mat(self.qref, order).T @ quat_to_mat(self.quat, order)
        b = BASES[basis % len(BASES)][1]
        r = b @ rel @ b.T
        if self.mirror:
            # Reversing exactly ONE axis is impossible by conjugation:
            # conjugating by orthogonal B maps a rotation about `a` to one
            # about `Ba`, negating the angle only when det(B) = -1, which
            # reverses all three at once. Needing one reversed means the SDK's
            # quaternion is LEFT-handed relative to OpenGL. For det(M) = -1,
            # M·R(a,t)·M = R(Ma,-t), so M·Rt·M = R(Ma,t); with M = diag(1,-1,1)
            # that negates only the y component of the axis.
            m = np.diag([1.0, -1.0, 1.0]).astype("f4")
            r = m @ r.T @ m
        s = SIGN_FIX[self.signfix % len(SIGN_FIX)][1].astype("f4")
        return (s @ r @ s.T).astype("f4")

    def orientation(self):
        """(yaw, pitch, roll) in degrees, recentred.

        The SDK reports euler in DEGREES already -- measured, not assumed:
        roll 33.58 / pitch 126.90 / yaw -71.14 at rest, 67 Hz. An earlier
        version scaled by 180/pi on the theory that they were radians, which
        would have swung the view by whole revolutions per degree of head
        movement.

        Signs: the IMU's yaw grows turning left while the camera's grows
        turning right, hence the negation. Applied per axis via SIGNS so a
        wrong guess is one number to change, not a rewrite.
        """
        if not self.euler:
            return 0.0, 0.0, 0.0
        # Element order is (YAW, pitch, roll), NOT the (roll, pitch, yaw) that
        # viture-ctl.py's printout claims. Established by observation: with the
        # old unpacking, nodding worked correctly while turning the head rolled
        # the horizon -- i.e. element 0 was driving roll, so element 0 is yaw.
        yaw, pitch, roll = self.euler
        if self.zero:
            roll -= self.zero[0]
            pitch -= self.zero[1]
            yaw -= self.zero[2]
        wrap = lambda d: (d + 180.0) % 360.0 - 180.0        # noqa: E731
        sy, sp, sr = self.signs
        return sy * wrap(yaw), sp * wrap(pitch), sr * wrap(roll)

    def shutdown(self):
        try:
            if self.v:
                self.v.close()
        except Exception:
            pass
        if self.driver_was_up:
            subprocess.run(["systemctl", "--user", "start", "xr-driver"],
                           capture_output=True, timeout=25)


def axis_of(rel):
    """Rotation axis (unit) and angle (degrees) of a rotation matrix."""
    ang = np.arccos(np.clip((np.trace(rel) - 1.0) / 2.0, -1.0, 1.0))
    if ang < 1e-6:
        return np.zeros(3), 0.0
    ax = np.array([rel[2, 1] - rel[1, 2],
                   rel[0, 2] - rel[2, 0],
                   rel[1, 0] - rel[0, 1]]) / (2.0 * np.sin(ang))
    return ax, np.degrees(ang)


# Guided calibration, run INSIDE the viewer so the prompts appear on the
# glasses. A terminal tool cannot work here: the wearer is looking at the
# headset, not the laptop.
#
# Directional HOLDS, not oscillations -- swinging back and forth makes the
# measured axis point whichever way you happened to be moving when sampled, so
# its sign is luck. Holding a known direction fixes it.
CAL_PHASES = [
    ("still", 4.0, "look STRAIGHT AHEAD"),
    ("right", 5.0, "turn RIGHT ~45 and HOLD"),
    ("c1", 3.0, "back to centre"),
    ("up", 5.0, "look UP ~30 and HOLD"),
    ("c2", 3.0, "back to centre"),
    ("tiltright", 5.0, "TILT RIGHT ear to shoulder, HOLD"),
]

# Where each motion must land in camera axes (x right, y up, -z forward).
CAL_TARGETS = {"up": (1.0, 0.0, 0.0),
               "right": (0.0, -1.0, 0.0),
               "tiltright": (0.0, 0.0, -1.0)}


def solve_basis(ref_q, holds, order):
    """Measured holds -> (basis matrix, mirror flag, report lines)."""
    ref = quat_to_mat(ref_q, order)
    meas, lines = {}, []
    for name in ("up", "right", "tiltright"):
        best, bang = None, 0.0
        for q in holds.get(name, []):
            ax, ang = axis_of(ref.T @ quat_to_mat(q, order))
            if ang > bang:
                best, bang = ax, ang
        if best is None or bang < 12.0:
            lines.append("%s: only %.0f deg -- not enough motion"
                         % (name, bang))
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
    lines.append("det %+.3f -> IMU is %s-handed" % (det, "R" if det > 0 else "L"))

    # Snap to the nearest signed permutation. The true relationship between a
    # chip's axes and the camera's is exactly one of these; the fractional
    # parts are just how imprecisely a person can hold their head at 45
    # degrees. Keeping them would bake that error in as permanent skew.
    snap = np.zeros((3, 3))
    for col in range(3):
        row = int(np.argmax(np.abs(b[:, col])))
        snap[row, col] = np.sign(b[row, col])
    ok = (np.abs(snap).sum() == 3 and np.abs(snap).sum(0).max() == 1
          and np.abs(snap).sum(1).max() == 1)
    if ok:
        err = float(np.abs(b - snap).max())
        lines.append("snapped to exact permutation (max adj %.2f)" % err)
        b = snap
    else:
        lines.append("NOT a clean permutation -- using raw, holds were sloppy")
    return b.astype("f4"), det < 0, lines


HUD_VERT = """
#version 330 core
in vec2 aPos;
out vec2 vUV;
uniform vec4 uRect;          // x0,y0,x1,y1 in NDC
void main() {
    vec2 t = aPos * 0.5 + 0.5;
    vUV = vec2(t.x, 1.0 - t.y);
    vec2 p = mix(uRect.xy, uRect.zw, t);
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

HUD_FRAG = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D sHud;
void main() { fragColor = texture(sHud, vUV); }
"""

HUD_W, HUD_H = 1024, 96


def hud_image(lines):
    """Status text -> RGBA. The viewer's controls were unusable without this:
    cycling the axis basis printed its state to the terminal, which nobody
    wearing the glasses can see."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (HUD_W, HUD_H), (0, 0, 0, 150))
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except OSError:
        f = ImageFont.load_default()
    for i, ln in enumerate(lines[:2]):
        d.text((HUD_W // 2, 26 + i * 40), ln, font=f, anchor="mm",
               fill=(255, 255, 255, 255))
    return np.asarray(img, dtype=np.uint8)


def yuv_params(path):
    """(3x3 matrix, (luma,chroma) scale, black level) from the FILE's tags.

    Untagged files are the common case; ffmpeg's own rule is BT.601 for
    standard definition and BT.709 above it, so match that rather than
    assuming one and being wrong half the time.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_space,color_range,height", "-of", "json", path],
        capture_output=True, text=True).stdout
    try:
        st = json.loads(out)["streams"][0]
    except (KeyError, IndexError, ValueError):
        st = {}
    cs = (st.get("color_space") or "").lower()
    height = int(st.get("height") or 0)
    if cs in ("bt709",):
        kr, kb, name = 0.2126, 0.0722, "bt709"
    elif cs in ("smpte170m", "bt470bg", "bt601", "smpte240m"):
        kr, kb, name = 0.299, 0.114, "bt601"
    else:
        # Untagged: swscale treats unspecified as BT.601 whatever the
        # resolution, so every other player shows it that way. Matching the
        # reference matters more than guessing the "intended" matrix -- and
        # real VR masters carry a bt709 tag, so this path is the odd case.
        kr, kb, name = 0.299, 0.114, "bt601 (untagged)"

    full = (st.get("color_range") or "").lower() in ("pc", "jpeg", "full")
    kg = 1.0 - kr - kb
    mat = np.array([
        [1.0, 0.0, 2.0 * (1.0 - kr)],
        [1.0, -kb * 2.0 * (1.0 - kb) / kg, -kr * 2.0 * (1.0 - kr) / kg],
        [1.0, 2.0 * (1.0 - kb), 0.0],
    ], dtype="f4")
    scale = (1.0, 1.0) if full else (255.0 / 219.0, 255.0 / 224.0)
    black = 0.0 if full else 16.0 / 255.0
    return mat, scale, black, "%s %s" % (name, "full" if full else "limited")


def load_still(path, max_w):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, max(1, round(img.height * max_w / img.width))),
                         Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)


def main():
    ap = argparse.ArgumentParser(description="360 / VR180 player")
    ap.add_argument("source", help="video or still image")
    ap.add_argument("--projection", choices=sorted(PROJ), help="override guess")
    ap.add_argument("--layout", choices=["mono", "sbs", "tb"],
                    help="override guess")
    ap.add_argument("--fov", type=float, default=90.0,
                    help="horizontal field of view, degrees (default 90)")
    ap.add_argument("--fisheye-fov", type=float, default=180.0,
                    help="lens FOV of a fisheye source, degrees (default 180). "
                         "VR180 rigs also ship 190 and 200; if the source's "
                         "circle spans the frame DIAGONAL rather than its "
                         "width, the effective figure is larger still.")
    ap.add_argument("--monitor", help="monitor name (e.g. DP-2) or index")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--size", default="1920x540", help="windowed size WxH")
    ap.add_argument("--imu", choices=["auto", "on", "off"], default="auto",
                    help="head tracking (auto = on when fullscreen)")
    ap.add_argument("--imu-rate", type=int, default=120,
                    choices=[60, 90, 120, 240])
    ap.add_argument("--imu-mode", choices=["quat", "euler"], default="quat",
                    help="quat composes rotations properly and is upright at "
                         "recentre by construction; euler is the old path, "
                         "kept only for comparison")
    ap.add_argument("--quat-order", choices=["xyzw", "wxyz"], default="xyzw")
    ap.add_argument("--ensure-3d", action="store_true", default=True,
                    help="switch the glasses to SBS first if they are in 2D "
                         "(on by default; --no-ensure-3d to skip)")
    ap.add_argument("--no-ensure-3d", dest="ensure_3d", action="store_false")
    ap.add_argument("--cal-lead", type=float, default=20.0,
                    help="seconds of lead-in before the first calibration "
                         "prompt, to get the glasses on (default 20)")
    ap.add_argument("--calibrate", action="store_true",
                    help="run the guided axis calibration at startup, prompts "
                         "shown ON the glasses. Press 'c' to redo it.")
    ap.add_argument("--recenter-after", type=float, default=6.0,
                    help="seconds before taking the reference pose. The first "
                         "IMU sample arrives while the glasses are still in "
                         "your hand, so recentring on it points the view "
                         "somewhere arbitrary -- often upside down.")
    ap.add_argument("--no-hud", action="store_true",
                    help="hide the on-screen status overlay")
    ap.add_argument("--imu-basis", type=int, default=0,
                    help="index into BASES -- press 'b' in the viewer to cycle")
    ap.add_argument("--imu-signs", default="-1,1,-1",
                    help="yaw,pitch,roll sign multipliers (default -1,1,-1). "
                         "If turning your head sends the view the wrong way, "
                         "flip the offending axis here.")
    ap.add_argument("--yaw", type=float, default=0.0)
    ap.add_argument("--pitch", type=float, default=0.0)
    ap.add_argument("--roll", type=float, default=0.0)
    ap.add_argument("--swap-eyes", action="store_true",
                    help="source has the right eye first")
    ap.add_argument("--hwaccel", action="store_true", default=True,
                    help="VAAPI hardware decode (on by default). Measured on "
                         "4K here: software 16 fps, VAAPI+CPU-scale 31 fps, "
                         "VAAPI+nv12 57 fps. --no-hwaccel to disable.")
    ap.add_argument("--no-hwaccel", dest="hwaccel", action="store_false")
    ap.add_argument("--pix-fmt", choices=["nv12", "rgb24"], default="nv12",
                    help="nv12 halves the bytes per frame and moves colour "
                         "conversion to the shader")
    ap.add_argument("--decode-width", type=int, default=7680,
                    help="downscale source to this width before decode "
                         "(VR masters are 4K-8K; this CPU is not)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--capture", metavar="FILE",
                    help="render one frame to FILE and exit")
    ap.add_argument("--platform", choices=["x11", "wayland", "any"],
                    default="any")
    a = ap.parse_args()

    still = os.path.splitext(a.source)[1].lower() in (
        ".png", ".jpg", ".jpeg", ".webp", ".bmp")
    if still:
        from PIL import Image
        with Image.open(a.source) as im:
            src_w, src_h = im.size
        fps = 0.0
    else:
        src_w, src_h, fps, _ = probe(a.source)

    proj, layout = guess_format(a.source, src_w, src_h)
    proj = a.projection or proj
    layout = a.layout or layout
    print("  source       : %s  %dx%d%s"
          % (os.path.basename(a.source), src_w, src_h,
             "  %.3f fps" % fps if fps else "  (still)"))
    print("  format       : %s / %s%s" % (
        proj, layout, "  [guessed]" if not (a.projection and a.layout) else ""))

    # The glasses silently fall back to 2D (1920x1080) and then BOTH eyes see
    # one squeezed frame containing both halves -- it looks like stereo broke
    # in software. Our own IMU handling causes it: xr-driver is stopped for
    # exclusive USB and appears to reset the panel to 2D when it restarts.
    if not a.windowed and a.ensure_3d and a.monitor:
        geom = subprocess.run(["xrandr"], capture_output=True,
                              text=True).stdout
        line = next((ln for ln in geom.splitlines()
                     if ln.startswith(a.monitor + " ")), "")
        if "3840x1080" not in line:
            print("  %s is not in SBS mode -- switching" % a.monitor)
            subprocess.run([sys.executable,
                            os.path.join(os.path.dirname(HERE),
                                         "viture-hw.py"), "3d", "on"],
                           capture_output=True, timeout=120)
            time.sleep(5.0)

    import glfw
    if a.platform != "any":
        # Two different GLFWs are reachable here and they disagree about what
        # exists. The pip wheel is 3.4 but ships a WAYLAND-ONLY libglfw in a
        # Wayland session; the system lib (PYGLFW_LIBRARY=/lib/.../libglfw.so)
        # is 3.3, which is X11-only and has no platform hint at all -- asking
        # it for X11 raises rather than complies, even though X11 is all it
        # can do.
        try:
            glfw.init_hint(glfw.PLATFORM,
                           {"x11": glfw.PLATFORM_X11,
                            "wayland": glfw.PLATFORM_WAYLAND}[a.platform])
        except AttributeError:
            print("  note         : this libglfw predates the platform hint "
                  "(3.3, X11-only) -- using it as-is")
    if not glfw.init():
        sys.exit("glfw init failed")

    mon, _ = pick_monitor(glfw, a.monitor)
    if a.windowed:
        win_w, win_h = (int(v) for v in a.size.lower().split("x"))
        mon = None
    else:
        mon = mon or glfw.get_primary_monitor()
        vm = glfw.get_video_mode(mon)
        win_w, win_h = vm.size.width, vm.size.height

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.FOCUSED, True)
    glfw.window_hint(glfw.FOCUS_ON_SHOW, True)
    # AUTO_ICONIFY defaults to TRUE: GLFW iconifies a FULLSCREEN window the
    # moment it loses input focus. Launched from a background shell it never
    # has focus, so it minimises itself instantly -- mapped, fullscreen,
    # correctly placed on the glasses, and showing the desktop. This is the
    # whole bug; the window was never in the wrong place.
    glfw.window_hint(glfw.AUTO_ICONIFY, False)
    win = glfw.create_window(win_w, win_h, "i3d vr", mon, None)
    if not win:
        glfw.terminate()
        sys.exit("could not create window")

    # A fullscreen window that appears without user interaction trips GNOME's
    # focus-stealing prevention: it is mapped, correctly sized and positioned
    # on the right output, and _NET_WM_STATE_HIDDEN -- i.e. minimised, showing
    # the desktop instead. Launching from a background shell looks exactly like
    # the case that protection exists for, so ask for focus explicitly.
    glfw.show_window(win)
    glfw.restore_window(win)          # in case it iconified before the hint
    for fn in ("focus_window", "request_window_attention"):
        try:
            getattr(glfw, fn)(win)
        except Exception:
            pass
    glfw.make_context_current(win)
    glfw.swap_interval(1)

    import moderngl
    ctx = moderngl.create_context()
    fb_w, fb_h = ctx.screen.size          # not glfw: see i3d_view.py
    eye_w, eye_h = fb_w // 2, fb_h
    print("  window       : %dx%d  (%s)  eye %dx%d" % (
        fb_w, fb_h,
        "fullscreen %s" % glfw.get_monitor_name(mon).decode() if mon
        else "windowed", eye_w, eye_h))

    prog = ctx.program(vertex_shader=VERT_VR,
                       fragment_shader=open(FRAG_VR).read())
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1],
                               dtype="f4").tobytes())
    vao = ctx.simple_vertex_array(prog, quad, "aPos")
    prog["sTexture"].value = 0

    # VR180 sources store a 180-degree hemisphere in a frame whose eye is
    # usually square; nothing to squeeze. 360 equirect eyes are 2:1.
    prog["uAspectFix"].value = 1.0
    prog["uFisheyeHalf"].value = math.radians(a.fisheye_fov * 0.5)

    stop = threading.Event()
    tracker = None
    use_imu = a.imu == "on" or (a.imu == "auto" and mon is not None)
    if use_imu:
        signs = tuple(float(v) for v in a.imu_signs.split(","))
        tracker = HeadTracker(stop, a.imu_rate, signs)
        tracker.start()
        print("  head tracking: VITURE IMU @ %d Hz (xr-driver stopped)"
              % a.imu_rate)
    else:
        print("  head tracking: off -- arrows/drag to look")

    dec = None
    q_dec = None
    frame = None
    if still:
        frame = load_still(a.source, a.decode_width)
        tex_w, tex_h = frame.shape[1], frame.shape[0]
    else:
        # With hardware decode, resize at NATIVE resolution -- this iGPU has no
        # VAAPI scaler, so any downscale runs on the CPU and gives back most of
        # what the GPU decode just won. The projection shader samples whatever
        # resolution it is handed, so there is nothing to gain by shrinking.
        if a.hwaccel and a.decode_width >= src_w:
            tex_w, tex_h = src_w & ~1, src_h & ~1
        else:
            tex_w = min(src_w, a.decode_width) & ~1
            tex_h = max(2, int(round(src_h * tex_w / float(src_w))) & ~1)
        q_dec = queue.Queue(maxsize=4)
        dec = Decoder(a.source, tex_w, tex_h, fps, a.start, a.duration,
                      True, q_dec, stop,      # stretch: no letterbox for VR
                      hwaccel=a.hwaccel, pix_fmt=a.pix_fmt)
        dec.start()
        print("  decoding     : %dx%d  %s%s"
              % (tex_w, tex_h, a.pix_fmt,
                 "  vaapi" if a.hwaccel else "  software"))

    hud_prog = ctx.program(vertex_shader=HUD_VERT, fragment_shader=HUD_FRAG)
    hud_vao = ctx.simple_vertex_array(hud_prog, quad, "aPos")
    hud_tex = ctx.texture((HUD_W, HUD_H), 4)
    hud_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    hud_prog["sHud"].value = 2
    hud_state = {"text": None}

    def set_hud(lines):
        key = tuple(lines)
        if hud_state["text"] != key:
            hud_tex.write(hud_image(lines))
            hud_state["text"] = key

    nv12 = (not still) and a.pix_fmt == "nv12"
    prog["uNV12"].value = 1 if nv12 else 0
    tex = tex_y = tex_uv = None
    if nv12:
        tex_y = ctx.texture((tex_w, tex_h), 1)
        tex_uv = ctx.texture((tex_w // 2, tex_h // 2), 2)
        for t, unit in ((tex_y, 3), (tex_uv, 4)):
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x, t.repeat_y = True, False
        prog["sY"].value = 3
        prog["sUV"].value = 4
        y_bytes = tex_w * tex_h
        ymat, yscale, yblack, yname = yuv_params(a.source)
        prog["uYUV"].write(ymat.T.copy())        # GL is column-major
        prog["uYUVScale"].value = yscale
        prog["uYBlack"].value = yblack
        print("  colour       : %s" % yname)
    else:
        tex = ctx.texture((tex_w, tex_h), 3)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = True           # equirect wraps in longitude
        tex.repeat_y = False
        if still:
            tex.write(frame)

    look = {"yaw": a.yaw, "pitch": a.pitch, "roll": a.roll, "fov": a.fov,
            "paused": False, "shot": bool(a.capture), "quit": False,
            "drag": None, "basis": a.imu_basis, "recal": False,
            # Zoom is FIELD OF VIEW, not translation. A quaternion has no
            # position in it, the Pro XR has no cameras to derive one from, and
            # 360 footage is sampled from a single point anyway -- moving
            # "into" it would need per-pixel depth that does not exist. Narrow
            # the FOV and the sphere magnifies, which is what zoom means here.
            "fov_now": a.fov}
    def new_cal():
        # A lead-in phase, because the first attempt started prompting before
        # the glasses were even on -- the wearer missed it and the run was
        # wasted. Nothing here can be followed until they are on your head.
        return {"t0": time.time(), "ref": None, "holds": {},
                "schedule": ([("ready", a.cal_lead,
                               "PUT THE GLASSES ON")] + CAL_PHASES)}

    cal = new_cal() if (a.calibrate and use_imu) else None

    def on_key(w, key, sc, action, mods):
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        step = 5.0
        if key in (glfw.KEY_ESCAPE, glfw.KEY_Q):
            look["quit"] = True
        elif key == glfw.KEY_LEFT:
            look["yaw"] -= step
        elif key == glfw.KEY_RIGHT:
            look["yaw"] += step
        elif key == glfw.KEY_UP:
            look["pitch"] += step
        elif key == glfw.KEY_DOWN:
            look["pitch"] -= step
        elif key == glfw.KEY_R:
            look["yaw"] = look["pitch"] = look["roll"] = 0.0
            if tracker:
                tracker.recenter()
        elif key == glfw.KEY_SPACE:
            look["paused"] = not look["paused"]
        elif key == glfw.KEY_LEFT_BRACKET:      # zoom IN = narrower FOV
            look["fov"] = max(20.0, look["fov"] - 5.0)
        elif key == glfw.KEY_RIGHT_BRACKET:
            look["fov"] = min(140.0, look["fov"] + 5.0)
        elif key == glfw.KEY_0:
            look["fov"] = a.fov                 # back to the starting zoom
        elif key == glfw.KEY_S:
            look["shot"] = True
        elif key == glfw.KEY_B:
            look["basis"] = (look["basis"] + 1) % len(BASES)
            print("  imu basis    : [%d] %s"
                  % (look["basis"], BASES[look["basis"]][0]), flush=True)
        elif key == glfw.KEY_F and tracker:
            tracker.signfix = (tracker.signfix + 1) % len(SIGN_FIX)
            print("  sign fix     : %s" % SIGN_FIX[tracker.signfix][0],
                  flush=True)
        elif key == glfw.KEY_C and tracker:
            look["recal"] = True
        elif key == glfw.KEY_M and tracker:
            tracker.mirror = not tracker.mirror
            print("  imu mirror   : %s (yaw direction)"
                  % ("on" if tracker.mirror else "off"), flush=True)
        elif key == glfw.KEY_I and tracker:
            tracker.zero = tracker.euler
            tracker.qref = tracker.quat

    def on_mouse(w, x, y):
        if look["drag"] is None:
            return
        px, py = look["drag"]
        look["yaw"] += (x - px) * 0.2
        look["pitch"] -= (y - py) * 0.2
        look["drag"] = (x, y)

    def on_scroll(w, dx, dy):
        look["fov"] = max(20.0, min(140.0, look["fov"] - dy * 4.0))

    def on_button(w, button, action, mods):
        if button == glfw.MOUSE_BUTTON_LEFT:
            look["drag"] = glfw.get_cursor_pos(w) if action == glfw.PRESS \
                else None

    glfw.set_key_callback(win, on_key)
    glfw.set_cursor_pos_callback(win, on_mouse)
    glfw.set_mouse_button_callback(win, on_button)
    glfw.set_scroll_callback(win, on_scroll)

    shown = 0
    t_start = time.time()
    last_report = t_start
    try:
        while not glfw.window_should_close(win) and not look["quit"]:
            if not still and not look["paused"]:
                try:
                    item = q_dec.get(timeout=1.0)
                except queue.Empty:
                    glfw.poll_events()
                    continue
                if item is SENTINEL:
                    break
                if nv12:
                    # NV12: full-res luma plane, then half-res interleaved
                    # chroma. Written as two textures, joined in the shader.
                    tex_y.write(item[:y_bytes])
                    tex_uv.write(item[y_bytes:])
                else:
                    tex.write(item)
                shown += 1

            yaw = pitch = roll = 0.0
            if tracker and tracker.quat and a.imu_mode == "quat":
                rot = tracker.matrix(look["basis"], a.quat_order)
                # report where we are actually looking; the status line used to
                # print a hardcoded 0.0 here, which reads as "tracking dead"
                fwd = rot @ np.array([0.0, 0.0, -1.0], dtype="f4")
                yaw = math.degrees(math.atan2(float(fwd[0]), -float(fwd[2])))
                pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[1])))))
            elif tracker and tracker.euler and a.imu_mode == "euler":
                yaw, pitch, roll = tracker.orientation()
                rot = rot_matrix(yaw, pitch, roll)
            else:
                yaw, pitch, roll = look["yaw"], look["pitch"], look["roll"]
                rot = rot_matrix(yaw, pitch, roll)
            # ease toward the target: stepping FOV instantly reads as the
            # picture jumping, which in a headset is unpleasant
            look["fov_now"] += (look["fov"] - look["fov_now"]) * 0.25
            tan_x = math.tan(math.radians(look["fov_now"]) * 0.5)
            tan_y = tan_x * eye_h / float(eye_w)
            prog["uRot"].write(rot.T.copy())      # GL is column-major
            prog["uTanHalfFov"].value = (tan_x, tan_y)
            prog["uProjection"].value = PROJ[proj]

            if look["recal"] and tracker:
                cal = new_cal()
                look["recal"] = False
                print("  recalibrating", flush=True)

            if cal is not None and tracker:
                el = time.time() - cal["t0"]
                acc = 0.0
                phase, prompt, left = None, "", 0.0
                for nm, dur, txt in cal["schedule"]:
                    if el < acc + dur:
                        phase, prompt, left = nm, txt, acc + dur - el
                        break
                    acc += dur
                if phase is None:
                    b, mirror, lines = solve_basis(cal["ref"], cal["holds"],
                                                   a.quat_order)
                    print("\n  calibration:")
                    for ln in lines:
                        print("    %s" % ln)
                    if b is None:
                        set_hud(["calibration FAILED", "press c to retry"])
                    else:
                        BASES.append(("calibrated", b))
                        look["basis"] = len(BASES) - 1
                        tracker.mirror = mirror
                        tracker.recenter()
                        print("    applied: basis[%d], mirror=%s"
                              % (look["basis"], mirror))
                        print("    %s" % np.round(b, 3).tolist())
                        set_hud(["CALIBRATED  mirror=%s" % mirror,
                                 "look around -- r recentres"])
                    cal = None
                else:
                    if tracker.quat:
                        if phase == "still":
                            cal["ref"] = tracker.quat
                        elif phase in CAL_TARGETS:
                            cal["holds"].setdefault(phase, []).append(
                                tracker.quat)
                    set_hud([prompt, "hold... %.0f" % max(1, round(left))])
            # recentre on a countdown, not on the first sample -- see _on_sample
            elif tracker and tracker.qref is None:
                left = a.recenter_after - (time.time() - t_start)
                if left <= 0:
                    tracker.recenter()
                    print("  recentred", flush=True)
                    set_hud(["recentred", "b basis   m yaw   r recentre"])
                else:
                    set_hud(["look STRAIGHT AHEAD",
                             "recentring in %.0f" % max(1, round(left))])
            else:
                set_hud(["zoom %d deg" % round(look["fov_now"]),
                         "scroll / [ ] zoom   r recentre   f signs"])

            if nv12:
                tex_y.use(3)
                tex_uv.use(4)
            else:
                tex.use(0)
            hud_tex.use(2)
            ctx.screen.use()
            ctx.clear(0.0, 0.0, 0.0)
            for eye, vp in ((0, (0, 0, eye_w, eye_h)),
                            (1, (eye_w, 0, eye_w, eye_h))):
                src_eye = (1 - eye) if a.swap_eyes else eye
                scale, offset = layout_uv(layout, src_eye)
                prog["uUVScale"].value = scale
                prog["uUVOffset"].value = offset
                ctx.viewport = vp
                vao.render(moderngl.TRIANGLE_STRIP)
                if not a.no_hud:
                    ctx.enable(moderngl.BLEND)
                    hud_prog["uRect"].value = (-0.55, -0.85, 0.55, -0.62)
                    hud_vao.render(moderngl.TRIANGLE_STRIP)
                    ctx.disable(moderngl.BLEND)
            ctx.finish()

            if look["shot"]:
                from PIL import Image
                buf = ctx.screen.read(components=3)
                shot = np.frombuffer(buf, dtype=np.uint8).reshape(
                    fb_h, fb_w, 3)[::-1]
                out = a.capture or "i3d_vr_shot.png"
                Image.fromarray(shot).save(out)
                print("  wrote        : %s  %dx%d  (yaw %.1f pitch %.1f)"
                      % (out, fb_w, fb_h, yaw, pitch))
                look["shot"] = False
                if a.capture:
                    break

            glfw.swap_buffers(win)
            glfw.poll_events()

            now = time.time()
            if now - last_report >= 3.0 and not still:
                el = now - t_start
                msg = "  %6.1fs  %5d frames  %.2f fps" % (el, shown, shown / el)
                if tracker:
                    msg += ("  imu %d samples  yaw %.1f" % (tracker.samples, yaw)
                            if tracker.samples else "  imu: no samples yet")
                    if tracker.error:
                        msg += "  ERROR %s" % tracker.error
                print(msg)
                last_report = now
    finally:
        stop.set()
        if dec:
            dec.close()
        if tracker:
            tracker.shutdown()
            if tracker.error:
                print("  imu error    : %s" % tracker.error)
        el = time.time() - t_start
        if shown:
            print("\n  played       : %d frames in %.1f s = %.2f fps"
                  % (shown, el, shown / el))
        glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
