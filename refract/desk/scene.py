"""Refract Desk -- the three-monitor virtual desktop.

The monitor topology:

  centre  a MIRROR of the laptop's own panel -- your real screen, projected
          large, still driven by the laptop's keyboard and trackpad
  left    genuine Mutter virtual monitors: independent extended displays,
  right   with their own windows. The pointer crosses into them, windows
          maximise on them, the clipboard is the session clipboard -- none
          of which has to be implemented here (architecture decision A2).

The two virtual monitors share one capture session; the mirror has its own,
because rearranging the desktop kills a mirror stream (see enter()).
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
from refract.core.vehicle import LaptopIMU
from refract.desk import layout

DEFAULTS = {
    "distance": 1.2,          # metres from the eye
    "size": 1.15,             # screen width in metres (ignored while filling)
    # Fill the view by default: one source pixel on roughly one panel pixel
    # is what makes text readable; minified, it shimmers.
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
    # Privacy: kill the laptop panel's backlight while Desk runs. Off by
    # default -- blanking someone's screen unasked is a nasty surprise.
    "blank_panel": False,
    # On by default, though it rearranges the desktop: Mutter places the
    # virtual monitors beyond the GLASSES output, so a window dragged toward
    # them lands on the glasses instead. Restored when Desk exits, and
    # reverts on logout regardless (temporary method).
    "arrange": True,
    # PROTOTYPE: cancels a vehicle's yaw using the laptop's own motion
    # sensor as the reference. Off by default -- outside a vehicle it would
    # cancel real head movement.
    "vehicle_mode": False,
}

# Layout presets: how far the head must turn to reach a side screen. In fill
# mode each screen is ~75 deg wide, so neighbours sit ~77 deg apart. "angle"
# is the degrees between adjacent screens (0 = auto: width + spacing);
# "width_deg" sizes the screens when not filling the view.
PRESETS = {
    "wide": dict(fill=True, angle=0.0, spacing=2.0),
    "tight": dict(fill=True, angle=40.0, spacing=2.0),
    "compact": dict(fill=False, angle=0.0, spacing=2.0, width_deg=50.0),
}
PRESET_OPTIONS = (("wide", "Wide (77 deg apart)"),
                  ("tight", "Tight (40 deg, overlapping)"),
                  ("compact", "Compact (52 deg, smaller)"),
                  ("custom", "Custom"))

# UNVERIFIED sign: not yet checked in a moving vehicle. If "Cancel vehicle
# motion" makes the panning worse instead of steadier, flip it to -1.0.
VEHICLE_YAW_SIGN = 1.0

# Follow easing: screens stay put until you look far enough away, then ease
# after you. Defined per second (Desk's frame rate varies with capture
# load); this is the feel of 8 % of the gap per frame at 60 fps.
FOLLOW_EASE_PER_FRAME_AT_60 = 0.08


def follow_alpha(dt):
    """Fraction of the remaining gap to close in `dt` seconds."""
    return 1.0 - (1.0 - FOLLOW_EASE_PER_FRAME_AT_60) ** (max(0.0, dt) * 60.0)

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


def yaw_only(rot, offset_deg=0.0):
    """Strip pitch and roll, keeping the heading.

    A resting head is never still: micro-pitch and micro-roll make text
    swim. Yaw is the axis you navigate a row of monitors with, so Desk keeps
    that one and discards the two that only add jitter.

    `offset_deg` adds to the extracted yaw before rebuilding the rotation --
    used to subtract a vehicle's own yaw (see VEHICLE_YAW_SIGN) so a bus
    turning a corner doesn't read as a head turn. 0.0 (default) changes
    nothing.
    """
    # head_yaw_deg() reports yaw in the opposite sense to rot_y, hence the
    # negation.
    return head_rot_y(-math.radians(head_yaw_deg(rot) + offset_deg))

# Moving a 1080p frame into a texture costs ~1.7 ms (C fast path) to ~6.6 ms
# (Python), and only when a new frame arrived. With the C path every screen
# updates every frame: a throttled neighbour made a window dragged across, or
# text typed on a screen at the edge of the view, lag by up to 125 ms (8 Hz;
# measured 74 ms median, 250 ms worst, against 50 ms median unthrottled).
# Without it the screens you are NOT facing tick over slowly to stay in
# budget.
CONTENT_HZ_FOCUS = 0.0      # 0 = every frame
CONTENT_HZ_IDLE = 0.0       # with the C fast path
CONTENT_HZ_IDLE_SLOW = 15.0  # the Python path


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
        self._loading = False             # applying stored/preset values
        self.mirror_connector = None
        self.mirror_index = 1
        self.started = False
        self.failed = None
        self._dirty = True
        self._saved_positions = None      # restored when Desk exits
        self._last_content = [0.0, 0.0, 0.0]
        self.backlight = Backlight()
        self.vehicle = LaptopIMU()        # vehicle-motion prototype, see below
        self.carousel = 0.0               # degrees the arc is swung by
        self.carousel_target = 0.0
        # frames put on each screen (a static monitor stops sending buffers
        # once painted, so "has it ever had content?" is the health check)
        self.frames_written = [0, 0, 0]
        # The C blit is a speed-up, never a requirement: if it was not built
        # or stops working mid-run, Desk keeps going on the Python path.
        self._fast = fastblit.available()
        self._fast_retired = None

    # -- config -----------------------------------------------------------

    def _cfg(self, key):
        return self.app.config.setdefault("desk", {}).get(key,
                                                          DEFAULTS[key])

    def _apply_stored(self, app):
        """Push the stored settings through their on_change handlers without
        that counting as the wearer editing a layout row."""
        self._loading = True
        try:
            S.apply_all(app, self.settings_schema())
        finally:
            self._loading = False

    def _detect_preset(self):
        """The preset the stored layout rows amount to, else "custom" -- so a
        config from before presets existed shows what it really is."""
        for name, spec in PRESETS.items():
            if all(self._cfg(k) == spec[k] for k in ("fill", "angle",
                                                     "spacing")):
                return name
        return "custom"

    def apply_preset(self, app, name):
        """Set the layout rows a preset stands for. "custom" changes nothing."""
        spec = PRESETS.get(name)
        if not spec or self._loading:
            return
        cfg = app.config.setdefault("desk", {})
        self._loading = True
        try:
            for key in ("fill", "angle", "spacing"):
                cfg[key] = spec[key]
            if "width_deg" in spec:
                cfg["size"] = round(2.0 * self._cfg("distance") * math.tan(
                    math.radians(spec["width_deg"]) * 0.5), 2)
        finally:
            self._loading = False
        app.config_dirty = True
        self._dirty = True
        self._say("layout: %s" % dict(PRESET_OPTIONS)[name], secs=2.0)

    def settings_schema(self):
        def rebuild(app, _value):
            self._dirty = True
            # editing any layout row by hand leaves the preset behind -- but
            # not while a preset (or the stored config) is being applied
            if not self._loading:
                app.config.setdefault("desk", {})["preset"] = "custom"

        def set_follow(app, value):
            if not value:
                self.follow_yaw = 0.0

        def set_vehicle_mode(app, value):
            if not value:
                self.vehicle.calibrating = False
                self.vehicle.integrator.reset()
                return
            msg = "hold the laptop still -- calibrating..."
            if not self._cfg("yaw_only"):
                # the compensation is only meaningful with pitch/roll
                # already stripped -- see the render_eye() comment
                app.config.setdefault("desk", {})["yaw_only"] = True
                app.config_dirty = True
                msg = "yaw only turned on too -- " + msg
            self.vehicle.begin_calibrate()
            self._say(msg, secs=3.0)

        def reset(app):
            app.config["desk"] = {}
            app.config_dirty = True
            self.follow_yaw = 0.0
            self.vehicle.calibrating = False
            self.vehicle.integrator.reset()
            self._dirty = True
            self._apply_stored(app)

        # while filling the view the width is derived, so show it read-only
        if self._cfg("fill"):
            size_row = S.Setting("Screen size", S.INFO,
                                 text="%.2f m (filling view)"
                                      % self._screen_width())
        else:
            size_row = S.Setting("Screen size", S.FLOAT, key="size",
                                 section="desk", default=DEFAULTS["size"],
                                 lo=0.3, hi=6.0, step=0.05, unit=" m",
                                 on_change=rebuild)

        # a real toggle only if this machine has the motion sensor it needs
        if self.vehicle.available:
            vehicle_row = S.Setting(
                "Cancel vehicle motion", S.BOOL, key="vehicle_mode",
                section="desk", default=DEFAULTS["vehicle_mode"],
                on_change=set_vehicle_mode)
        else:
            vehicle_row = S.Setting(
                "Cancel vehicle motion", S.INFO,
                text="unavailable (no built-in motion sensor found)")

        return [
            S.Setting("Layout preset", S.ENUM, key="preset", section="desk",
                      default=self._detect_preset(), options=PRESET_OPTIONS,
                      on_change=lambda app, value: self.apply_preset(
                          app, value)),
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
            vehicle_row,
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
                self._apply_layout(positions)
                print("  desk arrange : %s" % "  ".join(
                    "%s@(%d,%d)" % (c, p[0], p[1]) for c, p in sorted(
                        positions.items(), key=lambda kv: (kv[1][1],
                                                           kv[1][0]))),
                    flush=True)
            elif self._saved_positions:
                self._restore_positions()
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

    def _restore_positions(self):
        """Put the real monitors back where `_saved_positions` found them,
        without tripping Mutter's "Logical monitors not adjacent" rejection.

        The snapshot only knows the real outputs, but the virtual monitors
        still exist when this runs (see exit()). plan_positions() places
        every current monitor validly, parking the virtual ones on a second
        row.
        """
        current = displaymode.logical_layout()
        order = sorted(self._saved_positions,
                       key=lambda c: self._saved_positions[c][0])
        virtuals = [row[0] for row in current
                    if row[0] not in self._saved_positions]
        positions = layout.plan_positions(current, order, park=virtuals)
        self._apply_layout(positions)

    def _apply_layout(self, positions):
        """Move the logical monitors. Mutter moves our fullscreen window
        onto the laptop panel while it applies a layout, so have the app put
        it back on the glasses once the layout has settled."""
        displaymode.apply_positions(positions)
        # the virtual monitors' streams survive a layout change, but our
        # connection to them drops to ~5 fps until the pipelines restart
        if self.cap and self.cap.pipelines:
            self.cap.restart_pipelines()
        if self.app:
            self.app.reassert_output_soon()

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

        # mirror whichever real output is not the glasses
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
                                            curve=self._cfg("curve"),
                                            mipmaps=True))
        self._rebuild()

        # Order matters: rearranging the desktop kills a RecordMonitor (mirror)
        # stream for good, while RecordVirtual streams survive. So: virtual
        # monitors first, arrange the layout, THEN start the mirror (in
        # update()). Any later re-arrange restarts the mirror too.
        self.cap = ScreenCapture([("virtual", res), ("virtual", res)],
                                 capture=True)
        try:
            self.cap.start()
        except Exception as e:                          # noqa: BLE001
            self.failed = "capture failed: %s" % e
            return
        # not pumped to completion here (that blocks the shell for seconds);
        # update() pumps a little each frame until the monitors appear

    def exit(self, app):
        self.backlight.restore()
        self.vehicle.calibrating = False
        self.vehicle.integrator.reset()
        # restore the desktop BEFORE the virtual monitors vanish
        if self._saved_positions:
            try:
                self._restore_positions()
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
        # a resume from park must redo the whole bring-up
        self.started = False
        self._last_content = [0.0, 0.0, 0.0]
        if self._fast_retired is not None:
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

        Auto (angle=0): each screen's angular width plus a gap, so they never
        overlap -- but a screen that fills the view is ~74 deg wide, a big
        head turn to its neighbour. An explicit angle tightens the arc; the
        screens then overlap, and the off-centre ones are pushed slightly
        further out so the one you face occludes them cleanly.
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

        # Runs every frame regardless of what else is going on, so the
        # integration's own timing (see YawIntegrator) stays continuous.
        if self._cfg("vehicle_mode") and self.vehicle.available:
            if self.vehicle.calibrating:
                if self.vehicle.poll_calibrate():
                    self._say("vehicle motion: %s" % (
                        "calibrated -- watch for slow drift, recalibrate if "
                        "it creeps" if self.vehicle.calibrated
                        else "calibration failed"), secs=3.0)
            else:
                self.vehicle.update()

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
            self._apply_stored(app)
            self._start_mirror()
            self._report_layout()

        if self._dirty:
            self._rebuild()

        now = time.monotonic()
        focus = self._focused_index(app)
        for i, screen in enumerate(self.screens):
            hz = (CONTENT_HZ_FOCUS if i == focus else
                  CONTENT_HZ_IDLE if self._fast else CONTENT_HZ_IDLE_SLOW)
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
                    screen.uploaded()
                    self._last_content[i] = now
                    self.frames_written[i] += 1
                    continue
                if rc == fastblit.NO_FRAME:
                    continue
                if rc == fastblit.ERR_SIZE and gw > 0 and gh > 0:
                    # the frame carries its real size: adopt it, take the
                    # next one
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

        # follow: the screens stay put until the head turns past the
        # threshold, then ease after it
        rot = app.head_rot()
        head_yaw = head_yaw_deg(rot)
        if self._cfg("follow"):
            threshold = self._cfg("follow_threshold")
            delta = head_yaw - self.follow_yaw
            if abs(delta) > threshold:
                target = head_yaw - math.copysign(threshold, delta)
                self.follow_yaw += (target - self.follow_yaw) * follow_alpha(dt)

        # ease the carousel rather than snapping: an instant large jump of
        # everything in view makes people ill
        if abs(self.carousel_target - self.carousel) > 0.01:
            self.carousel += (self.carousel_target - self.carousel) \
                * min(1.0, dt * 7.0)
        else:
            self.carousel = self.carousel_target

    def _report_layout(self):
        """Print where Mutter actually put the monitors.

        The 3D order has the mirror in the centre; the pointer crosses
        monitors in the desktop's logical order. If they disagree, a window
        dragged off the centre screen arrives on the wrong side.
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
            # vehicle compensation only composes cleanly once pitch and roll
            # are stripped, which is why turning it on forces yaw-only
            vehicle_yaw = 0.0
            if (self._cfg("vehicle_mode") and self.vehicle.available
                    and not self.vehicle.calibrating):
                vehicle_yaw = VEHICLE_YAW_SIGN * self.vehicle.integrator.yaw_deg
            rot = yaw_only(rot, offset_deg=-vehicle_yaw)
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
        g = app.keys
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
