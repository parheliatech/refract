"""The Heads-Up Navigator: quick-switch, container settings, Global Settings.

Owned by App, NOT a scene. That is the whole point -- it draws over whatever
is running, and the scene behind keeps updating and rendering; it only loses
input focus. Backgrounding a game or a desktop session just to change a
setting is exactly the failure this product exists to avoid.

Reachability rule (DEVELOPMENT_PLAN.md): every settings surface is at most
two steps from anywhere. Toggle the HUD (1) and the active container's
settings are already on screen; Global Settings is one Enter away (2).

Head-locked, screen-space. The world-locked variant is deliberately NOT
built: which one feels right is a wearer question, and the plan says decide
it by wearing rather than by reasoning at the desk.
"""

import math
import time

import numpy as np

from refract.core import settings as S
from refract.core.render import Overlay, _font
from refract.shell.globalsettings import global_schema
from refract.shell.registry import REGISTRY

HUD_PX = (1152, 864)
# 4:3 in pixels and in NDC-against-the-eye-viewport, so nothing is stretched
HUD_RECT = (-0.60, -0.80, 0.60, 0.80)

# Measured on a head: NEITHER modifier combo reaches the app -- GNOME
# swallows both before a fullscreen GLFW window sees them. So the primary
# way in is a triple head bob (refract.core.gesture), and the keyboard
# fallback is a BARE key, which the compositor has no reason to grab.
# The combos stay bound in case a different session lets them through.
DEFAULT_HUD_KEYS = ["h", "ctrl+super+r", "ctrl+alt+r"]

MOD_NAMES = {"ctrl": "MOD_CONTROL", "shift": "MOD_SHIFT",
             "alt": "MOD_ALT", "super": "MOD_SUPER"}

BG = (9, 11, 16, 238)
EDGE = (120, 200, 235, 205)
TEXT = (232, 238, 248, 255)
MUTED = (138, 148, 165, 255)
SEL_BG = (30, 40, 54, 255)


def parse_combo(glfw, combo):
    """'ctrl+super+r' -> (mods_bitmask, glfw key). None if unparseable."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, None
    for p in parts:
        if p in MOD_NAMES:
            mods |= getattr(glfw, MOD_NAMES[p], 0)
        else:
            key = getattr(glfw, "KEY_" + p.upper(), None)
    return None if key is None else (mods, key)


class Row:
    """One navigable line. `parts` are click targets in image coordinates,
    filled in by the renderer so the pointer and the keyboard agree on what
    is where."""

    def __init__(self, kind, label="", setting=None, target=None,
                 entries=None):
        self.kind = kind          # chips | setting | link
        self.label = label
        self.setting = setting
        self.target = target
        self.entries = entries or []


class Hud:
    def __init__(self, app):
        self.app = app
        self.open = False
        self.page = "root"
        self.row = 0
        self.chip = 0
        self.overlay = Overlay(app, HUD_PX)
        self.focused = False
        self._gaze_ref = None
        self._dwell_row = None
        self._dwell_since = 0.0
        self._dwell_done = False
        self._confirm_quit = None
        self._now = 0.0
        self._sel_rect = None
        self.rows = []
        self.rects = []           # (row, part, index, x0, y0, x1, y1) 0..1
        self._key = None
        combos = app.config.get("global", {}).get("hud_key",
                                                  DEFAULT_HUD_KEYS)
        if isinstance(combos, str):
            combos = [combos]
        self.combos = [c for c in (parse_combo(app.glfw, c) for c in combos)
                       if c]
        self.combo_names = combos

    # -- state ------------------------------------------------------------

    def matches(self, key, mods):
        return any(key == k and mods == m for m, k in self.combos)

    def toggle(self):
        self.close() if self.open else self.show()

    def show(self):
        # The head bob reaches us through the IMU, which needs no window
        # focus -- but the KEYS afterwards do, and the focused window is
        # whatever the wearer last used on the laptop. Take focus if we can;
        # if the compositor refuses, head pointing drives the HUD instead.
        try:
            self.focused = self.app.grab_focus()
        except Exception as e:                            # noqa: BLE001
            print("  hud: focus grab failed: %s" % e, flush=True)
            self.focused = False
        print("  hud: open (keyboard %s)"
              % ("ours" if self.focused else "DENIED -- head pointing"),
              flush=True)
        self.open = True
        self._confirm_quit = None
        self._gaze_ref = None
        self._dwell_row = None
        self._dwell_since = 0.0
        self._dwell_done = False
        self.page = "root"
        self.row = 0
        self.chip = 0
        self._key = None

    def close(self):
        self.open = False
        self.overlay.hide()
        if self.focused:
            # the pointer is parked on the glasses output, where our own
            # window hides it; hand it back to the laptop panel
            self.app.release_pointer()
            self.focused = False
        # settings apply live, so closing is just the natural moment to put
        # the file on disk instead of writing it on every arrow keypress
        self.app.save_config()

    # Head pointing, used when the compositor refuses us the keyboard.
    # Degrees of head movement per row, and how long to hold before a row
    # fires. Dwell has to be long enough that merely reading a row does not
    # trigger it, short enough not to feel stuck.
    GAZE_ROW_DEG = 7.0
    GAZE_CHIP_DEG = 9.0
    DWELL = 1.1

    def update_gaze(self, app, dt, now):
        """Select by looking, activate by dwelling. No keyboard involved."""
        if not self.open or self.focused or not app.head or not self.rows:
            return
        from refract.desk.scene import head_yaw_deg
        rot = app.head_rot()
        fwd = rot @ np.array([0.0, 0.0, -1.0], dtype="f4")
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[1])))))
        yaw = head_yaw_deg(rot)
        if self._gaze_ref is None:
            self._gaze_ref = (pitch, yaw)
            return
        dpitch = pitch - self._gaze_ref[0]
        dyaw = yaw - self._gaze_ref[1]

        # looking DOWN moves down the list
        row = int(round(-dpitch / self.GAZE_ROW_DEG))
        row = max(0, min(len(self.rows) - 1, row))
        if row != self.row:
            self.row = row
            self._dwell_row = None
        current = self.rows[self.row]
        if current.kind == "chips" and current.entries:
            chip = int(round(dyaw / self.GAZE_CHIP_DEG))
            self.chip = max(0, min(len(current.entries) - 1, chip))

        if self._dwell_row != self.row:
            self._dwell_row = self.row
            self._dwell_since = now
            self._dwell_done = False
        elif not self._dwell_done and now - self._dwell_since >= self.DWELL:
            self._dwell_done = True       # one shot; look away and back to
            self._activate(current)       # fire the same row again

    def dwell_fraction(self, now):
        """0..1 progress toward firing, for drawing a dwell indicator."""
        if self.focused or self._dwell_row is None or self._dwell_done:
            return 0.0
        return max(0.0, min(1.0, (now - self._dwell_since) / self.DWELL))

    CONFIRM_TTL = 5.0

    def _build_rows(self):
        if self._confirm_quit and \
                time.time() - self._confirm_quit > self.CONFIRM_TTL:
            self._confirm_quit = None
        rows = []
        if self.page == "root":
            rows.append(Row("chips", "Switch to", entries=REGISTRY))
            scene = self.app.scene
            schema = scene.settings_schema() if scene else []
            for s in schema:
                rows.append(Row("setting", s.label, setting=s))
            rows.append(Row("link", "Global Settings", target="global"))
        else:
            for s in global_schema(self.app):
                rows.append(Row("setting", s.label, setting=s))
            rows.append(Row("link", "Back", target="root"))
        # Quit lives on EVERY page: it is the one action you must be able to
        # reach from wherever you are, and until now leaving meant finding a
        # keyboard the compositor might not even give us.
        rows.append(Row("quit", "Really quit? choose again to confirm"
                        if self._confirm_quit else "Quit Refract"))
        self.rows = rows
        self.row = max(0, min(len(rows) - 1, self.row))
        return rows

    def _state_key(self):
        # the dwell bar advances continuously, so the image must be rebuilt
        # while dwelling -- quantised so it is not every single frame
        dwell = round(self.dwell_fraction(self._now) * 12)
        """Everything that changes the picture. The PIL render is far too
        expensive to repeat per frame."""
        scene = self.app.scene
        vals = tuple(S.format_value(self.app, r.setting)
                     for r in self.rows if r.setting)
        return (self.page, self.row, self.chip,
                scene.title if scene else "", vals, dwell, self.focused,
                tuple(r.label for r in self.rows))

    def _sanity(self):
        """Every row must be reachable: if the selection cannot be scrolled
        into view the list is unusable, which is how the overflow bug hid."""
        return len(self.rows)

    # -- input ------------------------------------------------------------

    def on_key(self, key, mods):
        g = self.app.glfw
        if key == g.KEY_ESCAPE:
            if self.page != "root":
                self.page, self.row = "root", 0
            else:
                self.close()
            return True
        if key == g.KEY_UP:
            self.row = max(0, self.row - 1)
            return True
        if key == g.KEY_DOWN:
            self.row = min(len(self.rows) - 1, self.row + 1)
            return True
        row = self.rows[self.row] if self.rows else None
        if row is None:
            return True
        if key in (g.KEY_LEFT, g.KEY_RIGHT):
            step = 1 if key == g.KEY_RIGHT else -1
            if row.kind == "chips":
                self.chip = max(0, min(len(row.entries) - 1,
                                       self.chip + step))
            elif row.kind == "setting":
                S.adjust(self.app, row.setting, step)
            return True
        if key in (g.KEY_ENTER, g.KEY_KP_ENTER, g.KEY_SPACE):
            self._activate(row)
            return True
        return True       # the HUD swallows everything while it is open

    def _activate(self, row):
        if row.kind == "quit":
            # Two steps, deliberately. With dwell activation just LOOKING at
            # a row for a moment fires it, and quitting by accident mid-work
            # would be the worst thing the HUD could do.
            if self._confirm_quit:
                print("  hud: quit confirmed", flush=True)
                self.close()
                self.app.quit = True
            else:
                self._confirm_quit = time.time()
            return
        self._confirm_quit = None
        if row.kind == "chips":
            entry = row.entries[self.chip]
            if not entry.available:
                return
            self.close()
            self.app.switch(entry.make_scene())
        elif row.kind == "link":
            self.page = row.target
            self.row = 0
        elif row.kind == "setting":
            S.activate(self.app, row.setting)

    def _hit(self, ndc):
        x0, y0, x1, y1 = HUD_RECT
        fx = (ndc[0] - x0) / (x1 - x0)
        fy = 1.0 - (ndc[1] - y0) / (y1 - y0)      # image y runs downward
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            return None
        for rect in self.rects:
            if rect[3] <= fx <= rect[5] and rect[4] <= fy <= rect[6]:
                return rect
        return None

    def on_cursor(self, x, y):
        hit = self._hit((x, y))
        if hit:
            self.row = hit[0]
            if hit[1] == "chip":
                self.chip = hit[2]
        return True

    def on_mouse(self, button, action):
        g = self.app.glfw
        if button != g.MOUSE_BUTTON_LEFT or action != g.PRESS:
            return True
        hit = self._hit(self.app.cursor_ndc)
        if not hit:
            self.close()          # clicking outside dismisses, per the spec
            return True
        self.row = hit[0]
        row = self.rows[self.row]
        if hit[1] == "chip":
            self.chip = hit[2]
            self._activate(row)
        elif hit[1] in ("dec", "inc"):
            S.adjust(self.app, row.setting, -1 if hit[1] == "dec" else 1)
        else:
            self._activate(row)
        return True

    # -- drawing ----------------------------------------------------------

    def render_eye(self, app, eye):
        if not self.open:
            return
        self._now = time.time()
        self._build_rows()
        key = self._state_key()
        if key != self._key:
            img, self.rects = self._image()
            self.overlay.set_image(img, key=key)
            self._key = key
        self.overlay.visible = True
        self.overlay.draw(HUD_RECT)

    def _image(self):
        from PIL import Image, ImageDraw
        w, h = HUD_PX
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rects = []
        self._sel_rect = None
        d.rounded_rectangle([2, 2, w - 3, h - 3], radius=26, fill=BG,
                            outline=EDGE, width=3)
        # the same light-catching corner the panels use
        d.arc([6, 6, 66, 66], 180, 270, fill=(235, 248, 255, 220), width=3)
        d.line([36, 6, w * 0.5, 6], fill=(235, 248, 255, 130), width=2)

        scene = self.app.scene
        d.text((44, 46), "REFRACT", font=_font(34), anchor="lm", fill=TEXT)
        d.text((w - 44, 46), "in: %s" % (scene.title if scene else "-"),
               font=_font(26), anchor="rm", fill=MUTED)
        d.line([44, 82, w - 44, 82], fill=(70, 84, 104, 255), width=2)

        y = 112
        if self.page == "global":
            d.text((48, y + 14), "GLOBAL SETTINGS", font=_font(22),
                   anchor="lm", fill=MUTED)
            y += 36

        # Scroll, because the row list outgrew the panel: Desk alone has
        # eleven settings, and rows past the bottom were drawing over the
        # footer and could not be reached at all. Keep the chips row pinned
        # and slide the rest so the selection is always on screen.
        pinned = [r for r in self.rows if r.kind == "chips"]
        scrollable = [r for r in self.rows if r.kind != "chips"]
        first_scroll = len(pinned)
        room = (h - 74 - 20) - (y + (150 if pinned else 0))
        per_page = max(1, int(room // 72))
        sel = max(0, self.row - first_scroll)
        start = max(0, min(len(scrollable) - per_page,
                           sel - per_page // 2)) if \
            len(scrollable) > per_page else 0
        visible = pinned + scrollable[start:start + per_page]

        for row in visible:
            i = self.rows.index(row)
            sel = (i == self.row)
            if row.kind == "chips":
                d.text((48, y + 14), row.label.upper(), font=_font(22),
                       anchor="lm", fill=MUTED)
                y += 36
                y = self._draw_chips(d, row, i, sel, y, rects)
                # the container-settings heading only makes sense on root
                title = ("%s settings" % scene.title) if scene else "settings"
                d.text((48, y + 18), title.upper(), font=_font(22),
                       anchor="lm", fill=MUTED)
                y += 44
                if not any(r.kind == "setting" for r in self.rows):
                    d.text((72, y + 20), "this container has no settings",
                           font=_font(26), anchor="lm", fill=MUTED)
                    y += 56
            elif row.kind == "setting":
                y = self._draw_setting(d, row, i, sel, y, rects)
            else:
                y = self._draw_link(d, row, i, sel, y, rects)

        # centred, clear of the value arrows on the right of each row
        if start > 0:
            d.text((w // 2, y - 34), "^ more", font=_font(20), anchor="mm",
                   fill=MUTED)
        if start + per_page < len(scrollable):
            d.text((w // 2, h - 92), "v more", font=_font(20), anchor="mm",
                   fill=MUTED)

        d.line([44, h - 74, w - 44, h - 74], fill=(70, 84, 104, 255), width=2)
        if self.focused:
            hint = "%s close     up/down select     left/right adjust" \
                % self.combo_names[0]
        else:
            hint = "LOOK to select      HOLD to choose      3 nods to close"
        d.text((w // 2, h - 44), hint, font=_font(24), anchor="mm",
               fill=MUTED)

        # dwell indicator: a bar filling under the selected row tells you
        # something IS happening and how long is left
        frac = self.dwell_fraction(self._now)
        if frac > 0.0 and self._sel_rect:
            x0, y0, x1, y1 = self._sel_rect
            d.rounded_rectangle([x0, y1 - 6, x0 + (x1 - x0) * frac, y1 - 2],
                                radius=2, fill=EDGE)
        return np.asarray(img, dtype=np.uint8), rects

    def _draw_chips(self, d, row, index, sel, y, rects):
        w = HUD_PX[0]
        n = max(1, len(row.entries))
        pad, left = 14, 48
        cw = (w - 2 * left - pad * (n - 1)) / n
        ch = 78
        for j, entry in enumerate(row.entries):
            x = left + j * (cw + pad)
            on = sel and j == self.chip
            accent = entry.accent if entry.available else (110, 118, 132)
            d.rounded_rectangle([x, y, x + cw, y + ch], radius=14,
                                fill=SEL_BG if on else (16, 20, 28, 220),
                                outline=accent + ((255,) if on else (120,)),
                                width=3 if on else 2)
            d.text((x + cw / 2, y + ch * 0.40), entry.title, font=_font(30),
                   anchor="mm",
                   fill=TEXT if entry.available else (150, 157, 170, 255))
            d.text((x + cw / 2, y + ch * 0.74),
                   "ready" if entry.available else "phase %d" % entry.phase,
                   font=_font(19), anchor="mm", fill=accent + (255,))
            rects.append((index, "chip", j, x / w, y / HUD_PX[1],
                          (x + cw) / w, (y + ch) / HUD_PX[1]))
        return y + ch + 26

    def _draw_setting(self, d, row, index, sel, y, rects):
        w, h = HUD_PX
        s = row.setting
        rh = 62
        left, right = 48, w - 48
        if sel:
            d.rounded_rectangle([left, y, right, y + rh], radius=12,
                                fill=SEL_BG, outline=EDGE, width=2)
            self._sel_rect = (left, y, right, y + rh)
        label_fill = TEXT if s.available else MUTED
        d.text((left + 24, y + rh / 2), s.label, font=_font(28), anchor="lm",
               fill=label_fill)
        value = S.format_value(self.app, s)
        if s.adjustable:
            # A FIXED-WIDTH value field with the arrows outside it. The first
            # version centred the value between the arrows and drew it last,
            # so a wide value ("25.00 deg") painted straight over them and
            # the row looked like it had no controls at all.
            field = 200
            vx = right - 70 - field / 2
            for part, cx in (("dec", vx - field / 2 - 30),
                             ("inc", right - 30)):
                d.text((cx, y + rh / 2), "<" if part == "dec" else ">",
                       font=_font(30), anchor="mm",
                       fill=EDGE if sel else MUTED)
                rects.append((index, part, 0, (cx - 26) / w, y / h,
                              (cx + 26) / w, (y + rh) / h))
            d.text((vx, y + rh / 2), value, font=_font(27),
                   anchor="mm", fill=label_fill)
        else:
            d.text((right - 24, y + rh / 2), value, font=_font(25),
                   anchor="rm", fill=MUTED)
        rects.append((index, "main", 0, left / w, y / h, right / w,
                      (y + rh) / h))
        return y + rh + 10

    def _draw_link(self, d, row, index, sel, y, rects):
        w, h = HUD_PX
        rh = 62
        left, right = 48, w - 48
        if sel:
            d.rounded_rectangle([left, y, right, y + rh], radius=12,
                                fill=SEL_BG, outline=EDGE, width=2)
            self._sel_rect = (left, y, right, y + rh)
        quit_row = row.kind == "quit"
        warn = (255, 140, 140, 255)
        if quit_row and self._confirm_quit:
            d.rounded_rectangle([left, y, right, y + rh], radius=12,
                                outline=warn, width=3)
        d.text((left + 24, y + rh / 2), row.label, font=_font(28),
               anchor="lm", fill=warn if quit_row else TEXT)
        glyph = "x" if quit_row else (">" if row.target == "global" else "<")
        d.text((right - 24, y + rh / 2), glyph, font=_font(28), anchor="rm",
               fill=warn if quit_row else EDGE)
        rects.append((index, "main", 0, left / w, y / h, right / w,
                      (y + rh) / h))
        return y + rh + 10
