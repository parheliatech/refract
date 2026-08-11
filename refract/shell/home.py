"""Refract home: the root launcher scene.

Tiles ONLY. No settings, no toggles, ever -- configuration lives in the HUD
(DEVELOPMENT_PLAN.md invariant 3 -- a flat root where every toggle competes
for attention is the thing this product exists to avoid). Tiles are built
from refract.shell.registry, so a new sub-experience is a registration, not
a layout change.

The projection/hit-test helpers at module level are deliberately pure numpy:
they are the only part of pointer navigation that can be tested without a
window, and pointer aiming is exactly the sort of thing that looks fine in a
screenshot while being subtly wrong.
"""

import math
import time

import numpy as np

from refract.core import displaymode, hardware
from refract.core.render import (Overlay, Scene, WorldScreen, dot_image,
                                 panel_image, wordmark_image)
from refract.shell.registry import REGISTRY

TILE_DISTANCE = 1.35        # metres; matches Desk's comfortable reading depth
TILE_WIDTH = 0.32           # metres
TILE_PX = (560, 420)        # 4:3 -- taller than 16:9 reads better as a tile
TILE_GAP_DEG = 3.0

WORDMARK_PX = (768, 176)
WORDMARK_WIDTH = 0.44
WORDMARK_Y = 0.30           # metres above eye level

CURSOR_R = 0.013            # NDC


def translate(x, y, z):
    m = np.eye(4, dtype="f4")
    m[0, 3], m[1, 3], m[2, 3] = x, y, z
    return m


def tile_yaws(count, distance=TILE_DISTANCE, width=TILE_WIDTH,
              gap_deg=TILE_GAP_DEG):
    """Evenly spaced yaws about straight-ahead, same arc logic Desk uses for
    monitors: each tile subtends its own angular width, plus a gap."""
    step = 2.0 * math.atan2(width * 0.5, distance) + math.radians(gap_deg)
    return [(i - (count - 1) / 2.0) * step for i in range(count)]


def tile_center(yaw, distance=TILE_DISTANCE):
    """World position of a tile centre -- matches screen_mesh() at t=0.5."""
    return (math.sin(yaw) * distance, 0.0, -math.cos(yaw) * distance)


def project_ndc(mvp, point):
    """World point -> (ndc_x, ndc_y), or None if behind the eye.

    The w<=0 rejection is the whole reason this is a function: a point behind
    the viewer projects to a perfectly plausible NDC coordinate, so a tile
    behind your head would otherwise be clickable.
    """
    clip = np.asarray(mvp, dtype="f8") @ np.array(
        [point[0], point[1], point[2], 1.0], dtype="f8")
    if clip[3] <= 1e-6:
        return None
    return float(clip[0] / clip[3]), float(clip[1] / clip[3])


def tile_bounds_ndc(mvp, yaw, distance=TILE_DISTANCE, width=TILE_WIDTH,
                    height=None):
    """(x0, y0, x1, y1) NDC bounds of a tile, or None if not fully in front.

    All FOUR corners are projected. Measuring a half-width from one edge --
    the obvious shortcut -- is biased off-axis: a rectilinear projection
    stretches distance from the view axis non-linearly, so the left and right
    halves of the same tile project to different widths, and the hit box ends
    up offset from the tile the wearer sees. The selftest pins this.
    """
    height = height if height is not None else width * TILE_PX[1] / TILE_PX[0]
    c = tile_center(yaw, distance)
    rx, rz = math.cos(yaw) * width * 0.5, math.sin(yaw) * width * 0.5
    hy = height * 0.5
    xs, ys = [], []
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            p = project_ndc(mvp, (c[0] + sx * rx, c[1] + sy * hy,
                                  c[2] + sx * rz))
            if p is None:
                return None
            xs.append(p[0])
            ys.append(p[1])
    return min(xs), min(ys), max(xs), max(ys)


def pick_tile(bounds, pointer):
    """Index of the tile under the pointer, or None. Nearest centre wins if
    two hit boxes ever overlap."""
    best, best_d = None, None
    px, py = pointer
    for i, b in enumerate(bounds):
        if b is None:
            continue
        x0, y0, x1, y1 = b
        if not (x0 <= px <= x1 and y0 <= py <= y1):
            continue
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        d = (px - cx) ** 2 + (py - cy) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


class StatusProbe:
    """Glasses presence and SBS state for the status bar.

    Cached hard: find_pid() walks sysfs and the display state is a D-Bus
    round trip. Neither belongs in a 60 Hz render loop -- the status bar is
    the classic place where a per-frame syscall hides.
    """

    TTL = 5.0

    def __init__(self):
        self.t = -1e9
        self.glasses = False
        self.sbs = False

    def get(self):
        now = time.time()
        if now - self.t < self.TTL:
            return self.glasses, self.sbs
        self.t = now
        try:
            self.glasses = hardware.find_pid() is not None
        except Exception:
            self.glasses = False
        try:
            _, _, monitors, _, _ = displaymode.get_state()
            conn = displaymode.find_glasses(monitors)
            self.sbs = False
            for spec, modes, _props in monitors:
                if spec[0] == conn:
                    cur = next((m for m in modes
                                if m[6].get("is-current")), None)
                    self.sbs = bool(cur and cur[1] >= 3840)
        except Exception:
            self.sbs = False
        return self.glasses, self.sbs


class HomeScene(Scene):
    name = "home"
    title = "Refract"

    def __init__(self, entries=None):
        self.entries = entries if entries is not None else REGISTRY
        self.tiles = []
        self.focus = 0
        self.wordmark = None
        self.cursor = None
        self.probe = StatusProbe()
        self.message = None
        self.message_until = 0.0

    # -- lifecycle --------------------------------------------------------

    def enter(self, app):
        yaws = tile_yaws(len(self.entries))
        self.tiles = []
        for entry, yaw in zip(self.entries, yaws):
            screen = WorldScreen(app, TILE_PX, width_m=TILE_WIDTH,
                                 distance=TILE_DISTANCE, yaw=yaw, curve=0.0,
                                 alpha=True)
            sub = (entry.subtitle if entry.available
                   else "%s  (phase %d)" % (entry.subtitle, entry.phase))
            # both states rendered once, here -- panel_image builds a PIL
            # image, which is far too expensive to do per frame
            normal = panel_image("Refract " + entry.title, (sub,),
                                 *TILE_PX, focused=False, accent=entry.accent,
                                 available=entry.available)
            focused = panel_image("Refract " + entry.title, (sub,),
                                  *TILE_PX, focused=True, accent=entry.accent,
                                  available=entry.available)
            screen.write(normal)
            self.tiles.append({"entry": entry, "yaw": yaw, "screen": screen,
                               "normal": normal, "focused": focused})

        self.wordmark = WorldScreen(app, WORDMARK_PX,
                                    width_m=WORDMARK_WIDTH,
                                    distance=TILE_DISTANCE, curve=0.0,
                                    alpha=True)
        self.wordmark.write(wordmark_image(*WORDMARK_PX, sub="XR SHELL"))

        self.cursor = Overlay(app, (48, 48))
        self.cursor.set_image(dot_image(), key="dot")

        self.focus = min(self.focus, len(self.tiles) - 1)
        self._apply_focus(self.focus, force=True)

    def _apply_focus(self, index, force=False):
        if not self.tiles:
            return
        index = max(0, min(len(self.tiles) - 1, index))
        if index == self.focus and not force:
            return
        self.tiles[self.focus]["screen"].write(self.tiles[self.focus]["normal"])
        self.focus = index
        self.tiles[index]["screen"].write(self.tiles[index]["focused"])

    def _bounds(self, app):
        mvp = app.mvp(0)
        return [tile_bounds_ndc(mvp, t["yaw"]) for t in self.tiles]

    def _launch(self, app, index):
        entry = self.tiles[index]["entry"]
        if not entry.available:
            self._say("Refract %s ships in phase %d" % (entry.title,
                                                        entry.phase))
            return
        app.push(entry.make_scene())

    def _say(self, text, secs=3.0):
        self.message = text
        self.message_until = time.time() + secs

    # -- frame ------------------------------------------------------------

    def update(self, app, dt):
        glasses, sbs = self.probe.get()
        line1 = "%s   glasses %s   SBS %s" % (
            time.strftime("%H:%M"), "ok" if glasses else "--",
            "on" if sbs else "off")
        if self.message and time.time() < self.message_until:
            line2 = self.message
        else:
            self.message = None
            line2 = "<- ->  select      Enter  launch      Esc  quit"
        app.status.set_lines([line1, line2])

    def render_eye(self, app, eye):
        self.wordmark.render(app.mvp(eye, model=translate(0.0, WORDMARK_Y,
                                                          0.0)))
        for tile in self.tiles:
            tile["screen"].render(app.mvp(eye))
        x, y = app.cursor_ndc
        ry = CURSOR_R * (app.eye_w / float(app.eye_h))
        self.cursor.draw((x - CURSOR_R, y - ry, x + CURSOR_R, y + ry))

    # -- input ------------------------------------------------------------

    def on_key(self, app, key, scancode, action, mods):
        glfw = app.glfw
        if key == glfw.KEY_LEFT:
            self._apply_focus(self.focus - 1)
            return True
        if key == glfw.KEY_RIGHT:
            self._apply_focus(self.focus + 1)
            return True
        if key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER, glfw.KEY_SPACE):
            self._launch(app, self.focus)
            return True
        return False

    def on_cursor(self, app, x, y):
        hit = pick_tile(self._bounds(app), (x, y))
        if hit is not None:
            self._apply_focus(hit)
        return True

    def on_mouse(self, app, button, action, mods):
        glfw = app.glfw
        if button == glfw.MOUSE_BUTTON_LEFT and action == glfw.PRESS:
            hit = pick_tile(self._bounds(app), app.cursor_ndc)
            if hit is not None:
                self._apply_focus(hit)
                self._launch(app, hit)
            return True
        return False
