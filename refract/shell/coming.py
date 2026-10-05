"""Placeholder scene for registered-but-unported sub-experiences.

It keeps the launch/return path real and testable for tiles whose scene does
not exist yet.
"""

from refract.core.render import Scene, WorldScreen, panel_image


class ComingSoonScene(Scene):
    def __init__(self, entry):
        self.entry = entry
        self.name = entry.name
        self.title = "Refract " + entry.title
        self.panel = None

    def enter(self, app):
        self.panel = WorldScreen(app, (720, 460), width_m=0.62,
                                 distance=1.3, curve=0.0, alpha=True)
        self.panel.write(panel_image(
            "Refract " + self.entry.title, (self.entry.subtitle,
                                            "ships in phase %d"
                                            % self.entry.phase),
            720, 460, focused=True, accent=self.entry.accent))

    def update(self, app, dt):
        app.status.set_lines(["Refract %s -- not built yet (phase %d)"
                              % (self.entry.title, self.entry.phase),
                              "Esc  back to Refract home"])

    def render_eye(self, app, eye):
        self.panel.render(app.mvp(eye))
