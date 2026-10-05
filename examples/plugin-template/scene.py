"""A minimal Refract plugin scene.

Everything a sub-experience needs is on refract.core.render.Scene: enter /
exit for setup and teardown, update for per-frame logic and the status bar,
render_eye to draw (once per eye, viewport already set), and the on_* hooks
for input. Esc pops back to the launcher for free -- don't handle it here.
"""

from refract.core.render import Scene, WorldScreen, panel_image


class HelloScene(Scene):
    name = "hello"
    title = "Refract Hello"

    def enter(self, app):
        # One world-locked panel, 62 cm wide, 1.3 m out. panel_image builds a
        # PIL image, so build it here -- never per frame.
        self.panel = WorldScreen(app, (720, 460), width_m=0.62, distance=1.3,
                                 curve=0.0, alpha=True)
        self.panel.write(panel_image(
            "Hello from a plugin",
            ("This folder was dropped into refract/.",
             "Esc  goes back to the launcher"),
            720, 460, focused=True, accent=(120, 180, 255)))

    def update(self, app, dt):
        app.status.set_lines(["Refract Hello -- an example drop-in plugin",
                              "Esc  back to Refract home"])

    def render_eye(self, app, eye):
        self.panel.render(app.mvp(eye))
