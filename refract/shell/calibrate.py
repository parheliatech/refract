"""Guided axis calibration, prompted ON THE GLASSES.

The prompts must be where the eyes are: a terminal sequence is useless to
someone wearing a headset, and that lesson was paid for twice already (see
i3d/DESIGN.md). The solve itself lives in core.head.CalibrationSession --
this is only its face.

Directional HOLDS, never oscillations: swinging back and forth makes the
measured axis point whichever way you happened to be moving when sampled, so
its sign is luck.
"""

from refract.core.head import CalibrationSession
from refract.core.render import Scene, WorldScreen, panel_image


class CalibrationScene(Scene):
    name = "calibrate"
    title = "Calibration"

    def __init__(self):
        self.session = None
        self.panel = None
        self.lines = None
        self.finished = False

    def enter(self, app):
        self.panel = WorldScreen(app, (860, 420), width_m=0.72,
                                 distance=1.25, curve=0.0, alpha=True)
        if app.head:
            self.session = CalibrationSession(app.head)
            self._show("Calibration", ("hold still", "starting..."))
        else:
            self.finished = True
            self._show("Calibration", ("no IMU running", "Esc returns"))

    def _show(self, title, lines):
        key = (title, tuple(lines))
        if key != self.lines:
            self.panel.write(panel_image(title, lines, 860, 420,
                                         focused=True))
            self.lines = key

    def update(self, app, dt):
        if self.finished or not self.session:
            app.status.set_lines(["calibration finished",
                                  "Esc  back"])
            return
        prompt = self.session.update()
        if prompt is not None:
            self._show(prompt[0], (prompt[1],))
            app.status.set_lines(prompt)
            return
        # session just completed
        self.finished = True
        basis, mirror, report = self.session.result
        print("\n  calibration:", flush=True)
        for line in report:
            print("    %s" % line, flush=True)
        if self.session.apply():
            app.config.setdefault("global", {})["imu"] = app.head.settings()
            app.config_dirty = True
            app.save_config()
            print("    applied and saved", flush=True)
            self._show("CALIBRATED", ("axes solved and saved",
                                      "Esc  back"))
        else:
            self._show("CALIBRATION FAILED", ("not enough movement",
                                              "Esc  back, try again"))

    def render_eye(self, app, eye):
        self.panel.render(app.mvp(eye))
