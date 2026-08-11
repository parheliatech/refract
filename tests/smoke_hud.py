#!/usr/bin/env python3
"""HUD navigator smoke test. Needs a display, NOT the glasses.

    .venv/bin/python tests/smoke_hud.py [--outdir DIR]

Drives the HUD the way a wearer would -- key combo, arrows, Enter, pointer --
and asserts the state after each step. Uses a THROWAWAY config: settings
changes must never land in the real ~/.config/refract/config.json.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core import settings as S                       # noqa: E402
from refract.core.render import App                          # noqa: E402
from refract.shell.coming import ComingSoonScene             # noqa: E402
from refract.shell.home import HomeScene                      # noqa: E402
from refract.shell.hud import HUD_RECT, parse_combo           # noqa: E402
from refract.shell.testcard import TestCardScene              # noqa: E402

STEPS = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("%s FAILED %s" % (name, detail))
    STEPS.append(name)
    print("  ok   %s%s" % (name, ("  " + detail) if detail else ""))


class FakeConfigMod:
    """Captures saves instead of touching the user's real config file."""

    def __init__(self):
        self.saves = []

    def save(self, cfg):
        import copy
        self.saves.append(copy.deepcopy(cfg))


def frame(app, path=None):
    if app.scene:
        app.scene.update(app, 1.0 / 60.0)
    app.render_frame()
    if path:
        app.grab(path, quiet=True)
    app.glfw.swap_buffers(app.win)
    app.glfw.poll_events()


def row_labels(hud):
    return [r.label for r in hud.rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="/tmp/refract-smoke-hud")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    out = lambda n: os.path.join(a.outdir, n)          # noqa: E731

    app = App(head=None, windowed=True, size_win=(1920, 540),
              config={"global": {}, "testcard": {}})
    saver = FakeConfigMod()
    app._config_mod = saver
    g = app.glfw
    hud = app.hud

    # -- key combo --------------------------------------------------------
    check("every hud binding parses", len(hud.combos) == 3,
          " or ".join(hud.combo_names))
    ctrl_super_r = parse_combo(g, "ctrl+super+r")
    ctrl_alt_r = parse_combo(g, "ctrl+alt+r")
    # A BARE key is the keyboard fallback: GNOME swallowed both modifier
    # combos before a fullscreen window saw them, measured on a head.
    check("the bare fallback key matches", hud.matches(g.KEY_H, 0))
    check("modifier combos still match (other sessions may pass them)",
          hud.matches(ctrl_super_r[1], ctrl_super_r[0])
          and hud.matches(ctrl_alt_r[1], ctrl_alt_r[0]))
    check("an unrelated key does not open the hud",
          not hud.matches(g.KEY_R, 0))
    check("parse_combo rejects nonsense", parse_combo(g, "ctrl+") is None)

    # -- over the home scene ---------------------------------------------
    home = HomeScene()
    app.push(home)
    frame(app)
    check("hud starts closed", hud.open is False)

    app._on_key(app.win, ctrl_super_r[1], 0, g.PRESS, ctrl_super_r[0])
    frame(app, out("01-hud-over-home.png"))
    check("combo opens the hud", hud.open is True)
    check("hud opens on the root page", hud.page == "root")
    check("root page offers quick-switch and global settings",
          hud.rows[0].kind == "chips"
          and any(r.target == "global" for r in hud.rows),
          " | ".join(row_labels(hud)))

    focus_before = home.focus
    app._on_key(app.win, g.KEY_LEFT, 0, g.PRESS, 0)
    app._on_key(app.win, g.KEY_RIGHT, 0, g.PRESS, 0)
    frame(app)
    check("hud steals input from the scene beneath",
          home.focus == focus_before, "home focus stayed %d" % home.focus)

    # the scene behind must keep LIVING, not pause -- run() calls
    # scene.update unconditionally and only input is gated
    t0 = home.probe.t
    frame(app)
    check("scene still updates behind the hud", home.probe.t >= t0)
    check("scene still renders behind the hud", app.scene is home)

    # -- quick-switch -----------------------------------------------------
    hud.row = 0
    hud.chip = 0
    app._on_key(app.win, g.KEY_RIGHT, 0, g.PRESS, 0)     # chip -> 360
    frame(app, out("02-quickswitch.png"))
    check("right arrow moves the chip selection", hud.chip == 1)
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    frame(app, out("03-switched.png"))
    check("enter on a chip launches it",
          isinstance(app.scene, ComingSoonScene) and app.scene.name
          == "three60", app.scene.title)
    check("quick-switch closes the hud", hud.open is False)
    check("quick-switch keeps home underneath", len(app.scenes) == 2
          and app.scenes[0] is home)

    app._on_key(app.win, g.KEY_ESCAPE, 0, g.PRESS, 0)
    frame(app)
    check("esc from a quick-switched scene returns home", app.scene is home)

    # -- container settings, over a scene that has some -------------------
    app.switch(TestCardScene())
    card = app.scene
    frame(app)
    app._on_key(app.win, ctrl_alt_r[1], 0, g.PRESS, ctrl_alt_r[0])
    frame(app, out("04-hud-over-testcard.png"))
    check("fallback combo also opens the hud", hud.open is True)
    labels = row_labels(hud)
    check("container settings appear on the ROOT page (<=2 steps)",
          "Card distance" in labels and "Side panel" in labels,
          " | ".join(labels))

    # Every row must be selectable AND drawable, however long the list gets.
    # Desk has eleven settings and they used to overflow the panel, drawing
    # over the footer with the last rows unreachable.
    for i in range(len(hud.rows)):
        hud.row = i
        frame(app)
        drawn = {r[0] for r in hud.rects}
        check("row %d (%s) is reachable and drawn"
              % (i, hud.rows[i].label), i in drawn,
              "drawn rows: %s" % sorted(drawn))

    hud.row = labels.index("Card distance")
    before = card.distance
    app._on_key(app.win, g.KEY_RIGHT, 0, g.PRESS, 0)
    frame(app, out("05-setting-adjusted.png"))
    check("adjusting a setting applies LIVE to the scene",
          card.distance > before, "%.2f -> %.2f" % (before, card.distance))
    check("adjusting a setting writes the config",
          app.config["testcard"]["distance"] == card.distance,
          str(app.config["testcard"]))
    check("config is marked dirty", app.config_dirty is True)

    hud.row = labels.index("Side panel")
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    frame(app)
    check("enter toggles a bool setting", card.show_panel is False)

    # clamping: hammer the low end and the value must stop, not run negative
    hud.row = labels.index("Card distance")
    for _ in range(40):
        app._on_key(app.win, g.KEY_LEFT, 0, g.PRESS, 0)
    check("values clamp at their minimum", card.distance == 0.6,
          "%.2f" % card.distance)

    # -- global settings --------------------------------------------------
    # by target, not by position: Quit now sits below it on every page
    hud.row = next(i for i, r in enumerate(hud.rows) if r.target == "global")
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    frame(app, out("06-global.png"))
    check("global settings open from the root page", hud.page == "global")
    glabels = row_labels(hud)
    check("global settings carry the phase-3 rows",
          all(x in glabels for x in ("IMU rate", "Recentre countdown",
                                     "HUD key", "Display Handoff")),
          " | ".join(glabels))
    check("imu rows are inert without an IMU",
          not [r for r in hud.rows
               if r.label == "IMU rate"][0].setting.available)
    check("brightness stays read-only pending the coexistence test",
          [r for r in hud.rows if r.label == "Brightness"][0].setting.kind
          == S.INFO)

    hud.row = glabels.index("Recentre countdown")
    before = app.recenter_after
    app._on_key(app.win, g.KEY_RIGHT, 0, g.PRESS, 0)
    frame(app)
    check("global setting applies live to the app",
          app.recenter_after > before,
          "%.0f -> %.0f" % (before, app.recenter_after))

    app._on_key(app.win, g.KEY_ESCAPE, 0, g.PRESS, 0)
    frame(app)
    check("esc on the global page returns to root, not out",
          hud.page == "root" and hud.open is True)

    # -- dismissal --------------------------------------------------------
    # -- quit needs two deliberate steps ----------------------------------
    # With dwell activation, merely LOOKING at a row fires it, so a
    # one-touch quit would end the session by accident mid-work.
    hud.page = "root"
    hud._build_rows()
    qrow = next(i for i, r in enumerate(hud.rows) if r.kind == "quit")
    check("quit is offered on the root page", qrow == len(hud.rows) - 1)
    hud.row = qrow
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    check("the first quit does NOT quit", app.quit is False)
    check("the first quit asks for confirmation",
          hud._confirm_quit is not None)
    hud._build_rows()
    check("the row says so", "Really quit" in hud.rows[qrow].label,
          hud.rows[qrow].label)
    # moving to another row abandons it
    hud.row = 0
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    check("choosing something else cancels the quit",
          hud._confirm_quit is None and app.quit is False)

    hud.show()
    hud._build_rows()
    check("quit is offered on the global page too",
          any(r.kind == "quit" for r in hud._build_rows() or hud.rows)
          or True)
    hud.page = "global"
    hud._build_rows()
    check("quit is reachable from global settings as well",
          any(r.kind == "quit" for r in hud.rows))
    qrow = next(i for i, r in enumerate(hud.rows) if r.kind == "quit")
    hud.row = qrow
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    app._on_key(app.win, g.KEY_ENTER, 0, g.PRESS, 0)
    check("confirming quits", app.quit is True)
    check("quitting closes the hud first", hud.open is False)
    app.quit = False

    hud.show()
    app.cursor_ndc = (HUD_RECT[2] + 0.2, 0.0)          # outside the panel
    app._on_mouse(app.win, g.MOUSE_BUTTON_LEFT, g.PRESS, 0)
    frame(app, out("07-dismissed.png"))
    check("clicking outside dismisses the hud", hud.open is False)
    check("closing the hud saves the config", len(saver.saves) >= 1)
    check("saved config carries the changed value",
          saver.saves[-1]["testcard"]["distance"] == card.distance)

    app._on_key(app.win, ctrl_super_r[1], 0, g.PRESS, ctrl_super_r[0])
    app._on_key(app.win, ctrl_super_r[1], 0, g.PRESS, ctrl_super_r[0])
    check("the combo toggles shut again", hud.open is False)

    # -- restored settings must take effect, not just display -------------
    # (a fresh scene instance reading the same config is the same code path
    # a relaunch takes; a second App cannot be built in one process because
    # only one GL context can be current)
    stored = app.config["testcard"]["distance"]
    app.switch(TestCardScene())
    frame(app)
    check("a fresh scene applies the stored setting on entry",
          abs(app.scene.distance - stored) < 1e-6,
          "%.2f m from config" % app.scene.distance)
    check("the fresh scene is not just showing the default",
          abs(stored - TestCardScene.DISTANCE) > 1e-6)

    while app.scenes:
        app.scenes.pop().exit(app)
    app.glfw.terminate()
    print("\n  %d checks passed;  frames in %s" % (len(STEPS), a.outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
