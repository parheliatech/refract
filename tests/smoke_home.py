#!/usr/bin/env python3
"""Home-screen navigation smoke test. Needs a display, NOT the glasses.

    .venv/bin/python tests/smoke_home.py [--outdir DIR]

Drives the shell through keyboard and pointer navigation frame by frame and
asserts the state after each step, writing a PNG per step so the visual
result can be checked too. A capture of the home screen alone proves only
that it draws; this proves you can move around it.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.render import App                          # noqa: E402
from refract.shell.coming import ComingSoonScene             # noqa: E402
from refract.shell.home import HomeScene, tile_bounds_ndc    # noqa: E402

STEPS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("%s FAILED %s" % (name, detail))
    STEPS.append(name)
    print("  ok   %s%s" % (name, ("  " + detail) if detail else ""))


def frame(app, path=None):
    """Advance one frame; optionally save it."""
    if app.scene:
        app.scene.update(app, 1.0 / 60.0)
    app.render_frame()
    if path:
        app.grab(path, quiet=True)
    app.glfw.swap_buffers(app.win)
    app.glfw.poll_events()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="/tmp/refract-smoke")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    out = lambda n: os.path.join(a.outdir, n)          # noqa: E731

    # throwaway config: a test must never write the user's real settings
    app = App(head=None, windowed=True, size_win=(1920, 540), fov=46.0,
              config={"global": {}})
    home = HomeScene()
    app.push(home)
    frame(app, out("01-home.png"))
    g = app.glfw

    check("boots into home", app.scene is home)
    check("home focuses the first tile", home.focus == 0)
    check("tiles come from the registry", len(home.tiles) == 4,
          "titles: " + ", ".join(t["entry"].title for t in home.tiles))

    # -- keyboard ---------------------------------------------------------
    home.on_key(app, g.KEY_RIGHT, 0, g.PRESS, 0)
    frame(app, out("02-focus-360.png"))
    check("right arrow moves focus", home.focus == 1,
          "-> " + home.tiles[home.focus]["entry"].title)

    home.on_key(app, g.KEY_LEFT, 0, g.PRESS, 0)
    home.on_key(app, g.KEY_LEFT, 0, g.PRESS, 0)
    frame(app)
    check("focus clamps at the left end", home.focus == 0)

    for _ in range(6):
        home.on_key(app, g.KEY_RIGHT, 0, g.PRESS, 0)
    frame(app, out("03-focus-tak.png"))
    check("focus clamps at the right end", home.focus == 3,
          "-> " + home.tiles[home.focus]["entry"].title)

    # -- unavailable tiles say so, in the headset -------------------------
    home.on_key(app, g.KEY_ENTER, 0, g.PRESS, 0)
    frame(app, out("04-unavailable.png"))
    check("launching an unavailable tile does not switch scene",
          app.scene is home)
    check("unavailable tile explains itself on the status bar",
          home.message and "phase 8" in home.message, repr(home.message))

    # -- pointer ----------------------------------------------------------
    bounds = home._bounds(app)
    cx = (bounds[1][0] + bounds[1][2]) * 0.5
    cy = (bounds[1][1] + bounds[1][3]) * 0.5
    app.cursor_ndc = (cx, cy)
    home.on_cursor(app, cx, cy)
    frame(app, out("05-hover-360.png"))
    check("pointer hover moves focus", home.focus == 1,
          "-> " + home.tiles[home.focus]["entry"].title)

    app.cursor_ndc = (0.0, -0.95)                 # below every tile
    home.on_cursor(app, 0.0, -0.95)
    frame(app)
    check("pointer off the tiles keeps the last focus", home.focus == 1)

    # -- launch and return ------------------------------------------------
    app.cursor_ndc = (cx, cy)
    home.on_mouse(app, g.MOUSE_BUTTON_LEFT, g.PRESS, 0)
    frame(app, out("06-launched.png"))
    check("clicking an available tile launches it",
          isinstance(app.scene, ComingSoonScene))
    check("launched scene is the one clicked", app.scene.name == "three60",
          app.scene.title)
    check("home is still on the stack underneath", len(app.scenes) == 2)

    app._on_key(app.win, g.KEY_ESCAPE, 0, g.PRESS, 0)
    frame(app, out("07-back-home.png"))
    check("Esc returns to home", app.scene is home)
    check("returning home keeps the focus", home.focus == 1)

    # tiles must still be live after the round trip -- a scene that fails to
    # re-enter cleanly shows up here and nowhere else
    b2 = home._bounds(app)
    check("home still projects its tiles after returning",
          all(x is not None for x in b2))

    app._on_key(app.win, g.KEY_ESCAPE, 0, g.PRESS, 0)
    check("Esc at home quits", app.quit is True)

    while app.scenes:
        app.scenes.pop().exit(app)
    app.glfw.terminate()
    print("\n  %d checks passed;  frames in %s" % (len(STEPS), a.outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
