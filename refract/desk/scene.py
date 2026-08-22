"""Refract Desk -- the three-monitor virtual desktop.

Ported from xrdesk.py. The layout maths, the follow easing and the reasons
behind them are transplanted verbatim; what is new is the monitor topology
the concept spec calls for:

  centre  a MIRROR of the laptop's own panel -- your real screen, projected
          large, still driven by the laptop's keyboard and trackpad
  left    genuine Mutter virtual monitors: independent extended displays,
  right   with their own windows. The pointer crosses into them, windows
          maximise on them, the clipboard is the session clipboard -- none
          of which has to be implemented here (architecture decision A2).

All three ride in ONE capture session (refract.core.vdisplay.ScreenCapture),
so there is one Start(), one lifetime, one teardown.
"""

import math
import time

import numpy as np

from refract.core import displaymode
from refract.core import fastblit
from refract.core import settings as S
from refract.core.backlight import Backlight
from refract.core.head import rot_y as head_rot_y
from refract.core.render import Scene, WorldScreen, rot_y
from refract.core.vdisplay import VIRTUAL_PRODUCT, ScreenCapture
from refract.desk import layout

DEFAULTS = {
    "distance": 1.2,          # metres from the eye
    "size": 1.15,             # screen width in metres (ignored while filling)
    # Fill the view by default. A screen you are looking straight at should
    # occupy the whole frustum: that puts one source pixel on roughly one
    # panel pixel, which is what makes text readable. Sized smaller, the
    # 1920x1080 capture is minified into a fraction of the panel and the
    # resampling shimmers -- legibility dies long before the pixels do.
    "fill": True,
    "curve": 0.0,             # 0 flat .. 1 fully curved
    "spacing": 2.0,           # degrees of gap between screens (auto mode)
    # Degrees between adjacent screens. 0 = auto (no overlap, big turns);
    # anything smaller tightens the arc and lets them overlap.
    "angle": 0.0,
    # Yaw only, by default: see yaw_only() for why. Turn it off for a fully
    # world-locked desk that also pitches and rolls with your head.
    "yaw_only": True,
    "follow": False,
    "follow_threshold": 12.0,  # degrees of head turn before they follow
    "res": [1920, 1080],      # per virtual monitor
    # Privacy: kill the laptop panel's backlight while Desk runs, so the
    # three screens are visible to you and nobody else. Off by default --
    # blanking someone's screen unasked is not a friendly surprise.
    "blank_panel": False,
    # ON by default. It rearranges the desktop's logical monitor positions,
    # which is intrusive -- but without it Desk is not merely imperfect, it
    # is broken: Mutter parks the virtual monitors beyond the GLASSES output,
    # so dragging a window toward them lands it on the glasses instead, in
    # front of your eyes, hiding the monitors you were aiming for. Restored
    # when Desk exits, and reverts on logout regardless (temporary method).
    "arrange": True,
}

# Easing constant from xrdesk: screens stay put until you look far enough
# away, then ease after you instead of being nailed to your face.
FOLLOW_EASE = 0.08

_FORWARD = np.array([0.0, 0.0, -1.0], dtype="f4")


def _wrap180(d):
    while d > 180.0:
        d -= 360.0
    while d < -180.0:
        d += 360.0
    return d


def head_yaw_deg(rot):
    """Yaw of a head rotation, in degrees, ignoring pitch and roll."""
    fwd = np.asarray(rot) @ _FORWARD
    return math.degrees(math.atan2(float(fwd[0]), -float(fwd[2])))


def yaw_only(rot):
    """Strip pitch and roll, keeping the heading.

    A desk is a place to read, and a head that is merely resting is never
    still: micro-pitch and micro-roll ride on top of every glance and make
    text swim. Yaw is the axis you actually navigate a row of monitors
    with, so Desk can keep that one and discard the two that only add
    jitter. The screens stop bobbing and the text sits still.
    """
    # NOTE the sign: head_yaw_deg() reports yaw in the opposite sense to
    # rot_y (it is atan2(fwd.x, -fwd.z), inherited from xrdesk's follow
    # code, where a positive rot_y reads as a negative yaw). Rebuilding the
    # rotation without negating turns the view the wrong way -- caught by
    # "yaw-only preserves the heading" in tests/selftest.py.
    return head_rot_y(-math.radians(head_yaw_deg(rot)))

# Pulling a stream is expensive -- measured, three 1920x1080 streams:
#
#   pull samples only ......  19.7 ms/frame   (PyGObject copies 8 MB/stream)
#   + upload to textures ...  27.2 ms/frame   -> 31.7 fps
#   neither ................   0.4 ms/frame   -> 52.4 fps (vsync bound)
#
# ~6.6 ms per stream per pull. Throttling ALL of them equally made typing
# feel laggy: the screen you are working on is the one that must be current,
# and it was waiting its turn behind two idle ones. So the screen you are
# FACING refreshes every frame and the rest tick over slowly. Same total
# cost as a flat 30 Hz across three, spent where it is visible.
#
# Since then refract.core.fastblit does the pull-and-upload in C, which
# halves what a frame costs (3.38 -> 1.72 ms per 1080p stream, measured by
# tools/blit-bench.py). That buys headroom rather than removing the need for
# these throttles: the saving is ~5 ms per frame across three screens, out of
# the 16.7 ms a 60 fps frame gets.
CONTENT_HZ_FOCUS = 0.0      # 0 = every frame
CONTENT_HZ_IDLE = 8.0


class DeskScene(Scene):
    name = "desk"
    title = "Refract Desk"

    def __init__(self):
        self.app = None                   # set in enter(); guard before that
        self.cap = None                   # the two virtual monitors
        self.mcap = None                  # the mirror, its own session
        self.screens = []
        self.labels = []
        self.follow_yaw = 0.0
        self.mirror_connector = None
        self.mirror_index = 1
        self.started = False
        self.failed = None
        self._dirty = True
        self._saved_positions = None      # restored when Desk exits
        self._last_content = [0.0, 0.0, 0.0]
        self.backlight = Backlight()
        self.carousel = 0.0               # degrees the arc is swung by
        self.carousel_target = 0.0
        # frames actually put on each screen. A static virtual monitor stops
        # producing buffers once it has painted, so "is a sample pending?" is
        # not a health check -- "has this screen ever had content?" is.
        self.frames_written = [0, 0, 0]
        # The C blit is a speed-up, never a requirement: if it was not built
        # or stops working mid-run, Desk keeps going on the Python path.
        self._fast = fastblit.available()
        self._fast_retired = None

    # -- config -----------------------------------------------------------

    def _cfg(self, key):
        return self.app.config.setdefault("desk", {}).get(key,
                                                          DEFAULTS[key])

    def settings_schema(self):
        def rebuild(app, _value):
            self._dirty = True

        def set_follow(app, value):
            if not value:
                self.follow_yaw = 0.0

        def reset(app):
            app.config["desk"] = {}
            app.config_dirty = True
            self.follow_yaw = 0.0
            self._dirty = True
            S.apply_all(app, self.settings_schema())

        # The size row is adjustable only when the screens are NOT filling the
        # view: while filling, the width is derived, so an editable number
        # that silently does nothing would be worse than no number at all.
        if self._cfg("fill"):
            size_row = S.Setting("Screen size", S.INFO,
                                 text="%.2f m (filling view)"
                                      % self._screen_width())
        else:
            size_row = S.Setting("Screen size", S.FLOAT, key="size",
                                 section="desk", default=DEFAULTS["size"],
                                 lo=0.3, hi=6.0, step=0.05, unit=" m",
                                 on_change=rebuild)

        return [
            S.Setting("Distance", S.FLOAT, key="distance", section="desk",
                      default=DEFAULTS["distance"], lo=0.4, hi=6.0, step=0.1,
                      unit=" m", on_change=rebuild),
            S.Setting("Fill view", S.BOOL, key="fill", section="desk",
                      default=DEFAULTS["fill"], on_change=rebuild),
            size_row,
            S.Setting("Curve", S.FLOAT, key="curve", section="desk",
                      default=DEFAULTS["curve"], lo=0.0, hi=1.0, step=0.25,
                      on_change=rebuild),
            S.Setting("Spacing", S.FLOAT, key="spacing", section="desk",
                      default=DEFAULTS["spacing"], lo=0.0, hi=15.0, step=0.5,
                      unit=" deg", on_change=rebuild),
            S.Setting("Screen angle (0=auto)", S.FLOAT, key="angle",
                      section="desk", default=DEFAULTS["angle"], lo=0.0,
                      hi=110.0, step=5.0, unit=" deg", on_change=rebuild),
            S.Setting("Yaw only (steady)", S.BOOL, key="yaw_only",
                      section="desk", default=DEFAULTS["yaw_only"]),
            S.Setting("Follow my head", S.BOOL, key="follow", section="desk",
                      default=DEFAULTS["follow"], on_change=set_follow),
            S.Setting("Follow threshold", S.FLOAT, key="follow_threshold",
                      section="desk", default=DEFAULTS["follow_threshold"],
                      lo=2.0, hi=45.0, step=1.0, unit=" deg"),
            S.Setting("Blank laptop screen", S.BOOL, key="blank_panel",
                      section="desk", default=DEFAULTS["blank_panel"],
                      on_change=lambda app, value: (
                          self.backlight.blank() if value
                          else self.backlight.restore())),
            S.Setting("Match desktop layout", S.BOOL, key="arrange",
                      section="desk", default=DEFAULTS["arrange"],
                      on_change=lambda app, value: self._set_arrange(value)),
            S.Setting("Reset layout", S.ACTION, run=reset),
        ]

    # -- desktop monitor arrangement --------------------------------------

    def _set_arrange(self, wanted):
        """Line the desktop's logical monitors up with what the wearer sees,
        so the pointer crosses screens in the order they appear in 3D."""
        if not self.started:
            return                      # monitors do not exist yet
        try:
            if wanted:
                current = displaymode.logical_layout()
                if self._saved_positions is None:
                    self._saved_positions = layout.positions_of(current)
                order = self._connector_order()
                if not all(order):
                    return
                park = [displaymode.glasses_connector()]
                positions = layout.plan_positions(current, order, park=park)
                displaymode.apply_positions(positions)
                print("  desk arrange : %s" % "  ".join(
                    "%s@(%d,%d)" % (c, p[0], p[1]) for c, p in sorted(
                        positions.items(), key=lambda kv: (kv[1][1],
                                                           kv[1][0]))),
                    flush=True)
            elif self._saved_positions:
                displaymode.apply_positions(self._saved_positions)
                self._saved_positions = None
                print("  desk arrange : restored", flush=True)
            else:
                return
            # the layout just changed, so the mirror stream is dead; it only
            # exists once started, so this is a no-op during first bring-up
            if self.mcap:
                self._start_mirror()
        except Exception as e:                          # noqa: BLE001
            print("  desk arrange failed: %s" % e, flush=True)

    def _connector_order(self):
        """The connectors behind each 3D screen, left to right."""
        virtuals = [row[0] for row in displaymode.logical_layout()
                    if row[0].lower().startswith("meta")]
        order = []
        vi = 0
        for i in range(len(self.screens)):
            if i == self.mirror_index:
                order.append(self.mirror_connector)
            elif vi < len(virtuals):
                order.append(virtuals[vi])
                vi += 1
            else:
                order.append(None)
        return order

    # -- lifecycle --------------------------------------------------------

    def enter(self, app):
        self.app = app
        res = tuple(self._cfg("res"))

        # Mirror whichever output is not the glasses. Auto rather than
        # hardcoded: the connector name moves with the hardware.
        try:
            glasses = displaymode.glasses_connector()
            outs = displaymode.list_outputs()
            self.mirror_connector = next(
                (o["connector"] for o in outs
                 if o["connector"] != glasses
                 and VIRTUAL_PRODUCT.lower() not in (o["product"] or "").lower()
                 ), None)
        except Exception as e:                          # noqa: BLE001
            self.failed = "display query failed: %s" % e
            return

        if not self.mirror_connector:
            self.failed = "no laptop panel found to mirror"
            return

        self.labels = ["left (virtual)",
                       "centre (%s)" % self.mirror_connector,
                       "right (virtual)"]
        self.mirror_index = 1

        for _ in range(3):
            self.screens.append(WorldScreen(app, res, width_m=self._cfg("size"),
                                            distance=self._cfg("distance"),
                                            curve=self._cfg("curve")))
        self._rebuild()

        # The virtual monitors and the mirror live in SEPARATE sessions, and
        # the order matters: rearranging the desktop kills a RecordMonitor
        # stream stone dead (measured -- the mirror went from 105 frames in
        # 3 s to zero and never recovered), while RecordVirtual streams
        # survive. So the virtual monitors come up first, the layout is
        # arranged while no mirror exists to be killed, and only then is the
        # mirror started. Any later re-arrange restarts the mirror too.
        self.cap = ScreenCapture([("virtual", res), ("virtual", res)],
                                 capture=True)
        try:
            self.cap.start()
        except Exception as e:                          # noqa: BLE001
            self.failed = "capture failed: %s" % e
            return
        # NOT pumped to completion here: a 6 second block at scene entry
        # freezes the whole shell. update() pumps a little each frame and
        # says so on the status bar until the monitors materialise.

    def exit(self, app):
        self.backlight.restore()
        # restore the desktop BEFORE the virtual monitors vanish, or the
        # positions being restored refer to outputs that no longer exist
        if self._saved_positions:
            try:
                displaymode.apply_positions(self._saved_positions)
                print("  desk arrange : restored", flush=True)
            except Exception as e:                      # noqa: BLE001
                print("  desk arrange restore failed: %s" % e, flush=True)
            self._saved_positions = None
        if self.mcap:
            self.mcap.stop()
            self.mcap = None
        if self.cap:
            self.cap.stop()
            self.cap = None
        self.screens = []
        # Re-entering (a resume from park) must redo the whole bring-up:
        # arrange the layout, then start the mirror. Leaving this True made
        # resume come back with Mutter's default monitor placement and no
        # mirror at all -- the exact symptoms park is supposed to undo.
        self.started = False
        self._last_content = [0.0, 0.0, 0.0]
        if self._fast_retired is not None:
            # Worth saying out loud: the run silently got slower, and the
            # code that caused it is the code that just stopped being used.
            print("  desk blit    : C fast path retired mid-run (rc=%d), "
                  "ran on the python path" % self._fast_retired, flush=True)

    # -- layout -----------------------------------------------------------

    def _screen_width(self):
        """Metres wide. In fill mode this is derived from the FOV and the
        distance, so the screen keeps filling the view when it is moved --
        constant ANGULAR size, which is the property that matters through
        optics. The same fov drives the projection, so a screen this size
        lands exactly on the viewport edges whatever the number is set to.
        """
        if not self._cfg("fill"):
            return self._cfg("size")
        distance = self._cfg("distance")
        aspect = (self.screens[0].size_px[0] / float(self.screens[0].size_px[1])
                  if self.screens else 16.0 / 9.0)
        height = 2.0 * distance * math.tan(math.radians(self.app.fov) * 0.5)
        return height * aspect

    def _step(self):
        """Radians between adjacent screens.

        Auto (angle=0) is xrdesk's rule: each screen's own angular width plus
        a gap, so they never overlap. But a screen that FILLS the view is ~74
        deg wide, which puts its neighbour ~76 deg away -- a big head turn.
        Setting an explicit angle tightens the arc; screens then overlap, and
        the ones off-centre are pushed slightly further out so the one you
        are facing occludes them cleanly instead of intersecting.
        """
        angle = self._cfg("angle")
        if angle > 0.0:
            return math.radians(angle)
        return (2.0 * math.atan2(self._screen_width() * 0.5,
                                 self._cfg("distance"))
                + math.radians(self._cfg("spacing")))

    def _rebuild(self):
        n = len(self.screens)
        width = self._screen_width()
        distance = self._cfg("distance")
        step = self._step()
        overlapping = step < 2.0 * math.atan2(width * 0.5, distance) - 1e-6
        for i, screen in enumerate(self.screens):
            yaw = (i - (n - 1) / 2.0) * step
            # depth-stagger only when they overlap, so the focused screen
            # wins the depth test rather than intersecting its neighbours
            d = distance + (0.04 * abs(i - (n - 1) / 2.0) if overlapping
                            else 0.0)
            screen.set_geometry(width_m=width, distance=d, yaw=yaw,
                                curve=self._cfg("curve"))
        self._dirty = False

    def _source(self, screen_index):
        """(capture, stream index) behind a screen. The mirror has its own
        session; the two virtual monitors share the other one."""
        if screen_index == self.mirror_index:
            return self.mcap, 0
        return self.cap, 0 if screen_index < self.mirror_index else 1

    def _start_mirror(self):
        """(Re)start the mirror session. Must run AFTER any layout change --
        a RecordMonitor stream does not survive one."""
        if self.mcap:
            self.mcap.stop()
            self.mcap = None
        try:
            self.mcap = ScreenCapture([("monitor", self.mirror_connector)],
                                      capture=True)
            self.mcap.start()
            self.mcap.pump(2.0)
        except Exception as e:                          # noqa: BLE001
            print("  desk: mirror failed: %s" % e, flush=True)
            self.mcap = None

    def _focused_index(self, app):
        """Which screen the wearer is facing, in the rotated world."""
        if not self.screens:
            return 0
        view = head_yaw_deg(app.head_rot())
        best, best_d = 0, 1e9
        for i, screen in enumerate(self.screens):
            at = math.degrees(screen._geom["yaw"]) - self.follow_yaw \
                - self.carousel
            d = abs(_wrap180(at - view))
            if d < best_d:
                best, best_d = i, d
        return best

    def centre_on(self, index):
        """Swing the arc so screen `index` is straight ahead -- reaching the
        left monitor should not require turning 76 degrees."""
        if not self.screens:
            return
        index = max(0, min(len(self.screens) - 1, index))
        self.carousel_target = math.degrees(
            self.screens[index]._geom["yaw"]) - self.follow_yaw
        print("  desk: centring %s" % self.labels[index], flush=True)
        self._say(self.labels[index], secs=1.2)

    # -- frame ------------------------------------------------------------

    def update(self, app, dt):
        if self.failed:
            app.status.set_lines(["Refract Desk: %s" % self.failed,
                                  "Esc  back to Refract home"])
            return

        self.cap.pump(0.0)
        if self.mcap:
            self.mcap.pump(0.0)
        if not self.cap.ready():
            app.status.set_lines(["Refract Desk: bringing up displays...",
                                  "Esc  cancel"], ttl=0.5)
            return
        if not self.started:
            self.started = True
            # arrange FIRST (no mirror yet to kill), then start the mirror
            S.apply_all(app, self.settings_schema())
            self._start_mirror()
            self._report_layout()

        if self._dirty:
            self._rebuild()

        now = time.monotonic()
        focus = self._focused_index(app)
        for i, screen in enumerate(self.screens):
            hz = CONTENT_HZ_FOCUS if i == focus else CONTENT_HZ_IDLE
            if hz > 0.0 and now - self._last_content[i] < 1.0 / hz:
                continue
            src, idx = self._source(i)
            if not src:
                continue

            # Fast path: the frame goes from the PipeWire buffer into the
            # texture in C, without PyGObject copying 8 MB through a `bytes`.
            # Anything it cannot handle falls through to the path below, and
            # a hard failure retires it for the rest of the run.
            if self._fast:
                rc, gw, gh = src.blit_into(idx, screen.tex.glo,
                                           *screen.size_px)
                if rc == fastblit.OK:
                    self._last_content[i] = now
                    self.frames_written[i] += 1
                    continue
                if rc == fastblit.NO_FRAME:
                    continue
                if rc == fastblit.ERR_SIZE and gw > 0 and gh > 0:
                    # The frame carries its real size, so adopt it here and
                    # take the next one -- no round trip through the slow
                    # path to discover what a mirror has become.
                    screen.resize((gw, gh))
                    self._dirty = True
                    continue
                self._fast = False
                self._fast_retired = rc

            got = src.latest(idx)
            if not got:
                continue
            self._last_content[i] = now
            data, gw, gh = got
            if (gw, gh) != tuple(screen.size_px):
                screen.resize((gw, gh))
                self._dirty = True
            screen.write(data)
            self.frames_written[i] += 1

        # smooth follow, verbatim from xrdesk: the screens stay put until the
        # head turns past the threshold, then ease after it
        rot = app.head_rot()
        head_yaw = head_yaw_deg(rot)
        if self._cfg("follow"):
            threshold = self._cfg("follow_threshold")
            delta = head_yaw - self.follow_yaw
            if abs(delta) > threshold:
                target = head_yaw - math.copysign(threshold, delta)
                self.follow_yaw += (target - self.follow_yaw) * FOLLOW_EASE

        # ease the carousel rather than snapping: an instant 76 degree jump
        # of everything you are looking at is exactly the kind of motion that
        # makes people ill
        if abs(self.carousel_target - self.carousel) > 0.01:
            self.carousel += (self.carousel_target - self.carousel) \
                * min(1.0, dt * 7.0)
        else:
            self.carousel = self.carousel_target

        # Nothing is pinned to the view. Desk speaks only when something
        # changes (see _say) -- the old two-line readout was left over from
        # bringing the port up, and a permanent box floating in front of a
        # desktop you are trying to read is exactly the clutter the HUD
        # exists to replace.

    def _report_layout(self):
        """Print where Mutter actually put the monitors.

        The 3D order is the spec's (mirror in the centre); the POINTER
        crosses monitors in the desktop's logical order. If those disagree,
        dragging a window off the centre screen arrives on the wrong side --
        which only a wearer can judge, so state the facts and let the wearer
        test settle it.
        """
        try:
            _, _, monitors, logical, _ = displaymode.get_state()
        except Exception:                                # noqa: BLE001
            return
        order = []
        for (x, y, scale, transform, primary, mons, props) in logical:
            for (conn, vendor, product, ser) in mons:
                order.append((x, conn, product))
        order.sort()
        print("  desk layout  : 3D = %s" % " | ".join(self.labels))
        print("  desktop x    : %s" % "  ".join(
            "%s@%d" % (conn, x) for x, conn, _p in order))
        print("  desk blit    : %s" % (
            "C fast path" if self._fast
            else "python (%s)" % fastblit.why_unavailable()), flush=True)

    def render_eye(self, app, eye):
        if self.failed or not self.screens:
            return
        world = rot_y(math.radians(self.follow_yaw + self.carousel))
        rot = app.head_rot()
        if self._cfg("yaw_only"):
            rot = yaw_only(rot)
        mvp = app.mvp(eye, world=world, rot=rot)
        for screen in self.screens:
            screen.render(mvp)

    # -- input ------------------------------------------------------------

    def _say(self, text, secs=2.0):
        """Transient feedback. The wearer cannot see a terminal, so changes
        still have to be visible -- just not forever."""
        if self.app:
            self.app.status.set_lines([text, ""], ttl=secs)

    def _manual_size(self):
        """Leaving fill mode: seed the manual width with what is on screen
        now, so the first keypress nudges from where you are rather than
        jumping to an old stored number."""
        cfg = self.app.config.setdefault("desk", {})
        if self._cfg("fill"):
            cfg["size"] = round(self._screen_width(), 4)
            cfg["fill"] = False
            self.app.config_dirty = True

    def _bump(self, key, delta, lo, hi, unit=" m"):
        cfg = self.app.config.setdefault("desk", {})
        value = round(min(hi, max(lo, self._cfg(key) + delta)), 4)
        cfg[key] = value
        self.app.config_dirty = True
        self._dirty = True
        self._say("%s %.2f%s" % (key, value, unit))
        return value

    def on_key(self, app, key, scancode, action, mods):
        g = app.glfw
        if key == g.KEY_LEFT_BRACKET:
            self._bump("distance", -0.1, 0.4, 6.0)
        elif key == g.KEY_RIGHT_BRACKET:
            self._bump("distance", 0.1, 0.4, 6.0)
        elif key in (g.KEY_MINUS, g.KEY_EQUAL):
            # reaching for the size keys means "I want it manual"
            self._manual_size()
            self._bump("size", -0.05 if key == g.KEY_MINUS else 0.05,
                       0.3, 6.0)
        elif key == g.KEY_C:
            cfg = app.config.setdefault("desk", {})
            cfg["curve"] = 0.0 if self._cfg("curve") > 0.5 else 1.0
            app.config_dirty = True
            self._dirty = True
            self._say("curve %s" % ("on" if cfg["curve"] else "off"))
        elif key == g.KEY_F:
            self.on_command(app, "follow")
        elif key in (g.KEY_1, g.KEY_2, g.KEY_3):
            self.centre_on({g.KEY_1: 0, g.KEY_2: 1, g.KEY_3: 2}[key])
        elif key == g.KEY_COMMA:
            self.centre_on(self._focused_index(app) - 1)
        elif key == g.KEY_PERIOD:
            self.centre_on(self._focused_index(app) + 1)
        else:
            return False
        return True

    def on_command(self, app, cmd):
        cfg = app.config.setdefault("desk", {})
        if cmd == "follow":
            cfg["follow"] = not self._cfg("follow")
            if not cfg["follow"]:
                self.follow_yaw = 0.0
            app.config_dirty = True
            self._say("follow %s" % ("on" if cfg["follow"] else "off"))
        elif cmd == "curve":
            cfg["curve"] = 0.0 if self._cfg("curve") > 0.5 else 1.0
            app.config_dirty = True
            self._dirty = True
        elif cmd in ("nearer", "farther"):
            self._bump("distance", -0.1 if cmd == "nearer" else 0.1, 0.4, 6.0)
        elif cmd in ("smaller", "bigger"):
            self._manual_size()
            self._bump("size", -0.05 if cmd == "smaller" else 0.05, 0.3, 6.0)
        elif cmd in ("left", "centre", "center", "right"):
            self.centre_on({"left": 0, "centre": 1, "center": 1,
                            "right": 2}[cmd])
        elif cmd == "fill":
            cfg["fill"] = not self._cfg("fill")
            app.config_dirty = True
            self._dirty = True
        else:
            return False
        return True
