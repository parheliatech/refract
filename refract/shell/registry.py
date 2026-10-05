"""The sub-experience registry.

The launcher, and the HUD's quick-switch row, are BUILT FROM THIS LIST.
Adding a sub-experience is either a registration in `BUILTIN` below plus a
scene module, OR -- for anything out-of-tree -- a drop-in plugin: a folder
with an `experience.toml` manifest placed inside the refract package
directory (see refract/shell/plugins.py). Either way it must never be a
layout redesign, and never a new tile hardcoded into the home screen. That
is what keeps the root shallow (see DEVELOPMENT_PLAN.md invariant 3).

`available` means "selectable from the launcher now". `implemented` means the
real scene exists; until it does, the factory returns a ComingSoonScene, so
the tile -> launch -> Esc -> home path works for every tile.
Discovered plugins are always available and implemented -- a placeholder
plugin makes no sense.

REGISTRY is rebuilt in place by reload(): built-ins first, in the order
below, then discovered plugins sorted by id. It is mutated, not reassigned,
so `from refract.shell.registry import REGISTRY` elsewhere keeps working.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from refract.core.render import PARHELIA_AMBER

# A plugin that names no accent gets this: a neutral slate, deliberately not
# one of the Parhelia signal hues below, which are each spoken for.
ACCENT_DEFAULT = (150, 160, 178)


@dataclass
class SubExperience:
    name: str                       # config section / internal id
    title: str                      # shown on the tile
    subtitle: str = ""              # one line, what it is for
    accent: tuple = ACCENT_DEFAULT  # Parhelia signal hue, per-experience
    available: bool = True          # selectable from the launcher
    implemented: bool = True        # real scene, not a placeholder
    phase: int = 0                  # plan phase that delivers it (0 = plugin)
    scene_factory: Optional[Callable] = None

    def make_scene(self):
        if self.scene_factory is not None:
            return self.scene_factory()
        from refract.shell.coming import ComingSoonScene
        return ComingSoonScene(self)


# Brighter than the raw Parhelia hues: these are read through tinted optics
# on a black field, where the print-weight values go muddy.
ACCENT_DESK = (46, 190, 205)          # teal   #0a7985
ACCENT_360 = PARHELIA_AMBER           # amber  #ffac11
ACCENT_PLAY = (232, 60, 140)          # magenta #db1675
ACCENT_TAK = (96, 226, 96)            # radar green


def _desk_scene():
    # imported on demand: the Desk scene pulls in GStreamer/PipeWire, which
    # nothing else in the shell needs just to draw a launcher tile
    from refract.desk.scene import DeskScene
    return DeskScene()


BUILTIN = [
    SubExperience(
        name="desk", title="Desk", subtitle="Virtual monitors",
        accent=ACCENT_DESK, available=True, implemented=True, phase=4,
        scene_factory=_desk_scene),
    SubExperience(
        name="three60", title="360", subtitle="Immersive video",
        accent=ACCENT_360, available=True, implemented=False, phase=6),
    SubExperience(
        name="play", title="Play", subtitle="Games and 3D films",
        accent=ACCENT_PLAY, available=False, implemented=False, phase=7),
    SubExperience(
        name="tak", title="TAK", subtitle="Tactical awareness",
        accent=ACCENT_TAK, available=False, implemented=False, phase=8),
]

# Populated by reload() at import time. Kept as one list object for its
# whole life -- see the module docstring.
REGISTRY = []


def reload():
    """Rebuild REGISTRY: built-ins, then whatever plugins.discover() finds.

    A folder that reuses a built-in id is reported and ignored rather than
    shadowing it. Discovery failing as a whole must not stop the shell
    booting -- worst case you get the built-ins and a line on stdout.
    """
    from refract.shell import plugins
    REGISTRY[:] = list(BUILTIN)
    try:
        REGISTRY.extend(
            plugins.discover(claimed=[e.name for e in BUILTIN]))
    except Exception as e:                                # noqa: BLE001
        print("  plugin: discovery failed -- %s" % e, flush=True)


def by_name(name):
    return next((e for e in REGISTRY if e.name == name), None)


reload()
