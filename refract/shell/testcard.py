"""Phase 1 gate scene: a spinning test card driven entirely through
refract.core. If this renders correctly in stereo on the glasses and windowed
on the laptop, the core runtime holds.

The card is labelled (per the i3d lesson: a plausible image can still be
wrong -- yaw inverted, eyes swapped -- and you cannot tell from inside the
headset without labels)."""

import math

import numpy as np

from refract.core import settings as S
from refract.core.render import Scene, WorldScreen, panel_image, rot_y


def card_image(w=1024, h=576):
    from PIL import Image, ImageDraw, ImageFont
    from refract.core.render import FONT_PATH
    img = Image.new("RGBA", (w, h), (12, 14, 20, 255))
    d = ImageDraw.Draw(img)
    try:
        big = ImageFont.truetype(FONT_PATH, 96)
        small = ImageFont.truetype(FONT_PATH, 32)
    except OSError:
        big = small = ImageFont.load_default()
    # border + centre cross: clipping or warping shows up immediately
    d.rectangle([4, 4, w - 5, h - 5], outline=(90, 110, 140, 255), width=3)
    d.line([w // 2, 0, w // 2, h], fill=(60, 70, 90, 255), width=1)
    d.line([0, h // 2, w, h // 2], fill=(60, 70, 90, 255), width=1)
    # labelled corners: TL must be top-left in the headset, or the v-flip /
    # orientation story has regressed
    for tag, xy, anchor in (("TL", (16, 12), "la"), ("TR", (w - 16, 12), "ra"),
                            ("BL", (16, h - 12), "ld"),
                            ("BR", (w - 16, h - 12), "rd")):
        d.text(xy, tag, font=small, anchor=anchor, fill=(140, 200, 255, 255))
    # a hue ramp: banding or channel swaps are visible at a glance
    for i in range(w - 160):
        t = i / float(w - 160)
        d.line([80 + i, h - 90, 80 + i, h - 60],
               fill=(int(255 * t), int(120 + 80 * math.sin(t * 6.28)),
                     int(255 * (1 - t)), 255))
    d.text((w // 2, h // 2 - 40), "REFRACT", font=big, anchor="mm",
           fill=(255, 255, 255, 255))
    d.text((w // 2, h // 2 + 40), "core runtime test card", font=small,
           anchor="mm", fill=(200, 210, 225, 255))
    return np.asarray(img, dtype=np.uint8)


class TestCardScene(Scene):
    name = "testcard"
    title = "Test Card"

    # oscillate rather than spin: a full rotation goes edge-on (invisible)
    # and a card that orbits the world origin leaves the frustum entirely --
    # which is exactly how the first version failed
    SWAY_DEG = 25.0
    SWAY_HZ = 0.1

    DISTANCE = 1.4

    def __init__(self):
        self.t = 0.0
        self.angle = 0.0
        self.screen = None
        self.panel = None
        self.distance = self.DISTANCE
        self.sway_deg = self.SWAY_DEG
        self.show_panel = True

    # Until Desk lands (phase 4) this is the only scene with real container
    # settings, so it doubles as the proof that the HUD renders a schema and
    # applies changes live.
    def settings_schema(self):
        def set_distance(app, value):
            self.distance = float(value)
            if self.screen:
                self.screen.set_geometry(width_m=1.3, distance=self.distance,
                                         curve=0.0)

        def set_sway(app, value):
            self.sway_deg = float(value)

        def set_panel(app, value):
            self.show_panel = bool(value)

        return [
            S.Setting("Card distance", S.FLOAT, key="distance",
                      section="testcard", default=self.DISTANCE, lo=0.6,
                      hi=3.0, step=0.1, unit=" m", on_change=set_distance),
            S.Setting("Sway", S.FLOAT, key="sway", section="testcard",
                      default=self.SWAY_DEG, lo=0.0, hi=60.0, step=5.0,
                      unit=" deg", on_change=set_sway),
            S.Setting("Side panel", S.BOOL, key="panel", section="testcard",
                      default=True, on_change=set_panel),
        ]

    def enter(self, app):
        self.screen = WorldScreen(app, (1024, 576), width_m=1.3,
                                  distance=self.DISTANCE, curve=0.0)
        self.screen.write(card_image())
        # a glass panel floating beside the card: exercises the alpha path
        # and shows the toolkit primitive in stereo
        self.panel = WorldScreen(app, (640, 360), width_m=0.55,
                                 distance=1.3, yaw=0.85, curve=0.0,
                                 alpha=True)
        self.panel.write(panel_image("Refract", ("scene stack live",
                                                 "Esc exits"), focused=True))
        # a restored config must TAKE EFFECT, not merely be displayed
        S.apply_all(app, self.settings_schema())

    def update(self, app, dt):
        self.t += dt
        self.angle = math.radians(self.sway_deg) * math.sin(
            2.0 * math.pi * self.SWAY_HZ * self.t)
        app.status.set_lines(
            ["test card  fov %.0f  ipd %.3f" % (app.fov, app.ipd),
             "R recentre   Esc quit"])

    def render_eye(self, app, eye):
        # pivot about the card's own centre: rotating about the world origin
        # would orbit it around the viewer and out of view
        c = np.eye(4, dtype="f4")
        c[2, 3] = -self.distance
        c_inv = np.eye(4, dtype="f4")
        c_inv[2, 3] = self.distance
        model = c @ rot_y(self.angle) @ c_inv
        self.screen.render(app.mvp(eye, model=model))
        if self.show_panel:
            self.panel.render(app.mvp(eye))
