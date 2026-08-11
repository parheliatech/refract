"""The sub-experience registry.

The launcher, and later the HUD's quick-switch row, are BUILT FROM THIS LIST.
Adding a sub-experience later must be a registration here plus a scene module
-- never a layout redesign, and never a new tile hardcoded into the home
screen. That is what keeps the root shallow (see DEVELOPMENT_PLAN.md
invariant 3).

`available` means "selectable from the launcher now". `implemented` means the
real scene exists; until it does, the factory returns a ComingSoonScene so the
tile -> launch -> Esc -> home path is exercisable from Phase 2 onward.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from refract.core.render import PARHELIA_AMBER


@dataclass
class SubExperience:
    name: str                       # config section / internal id
    title: str                      # shown on the tile
    subtitle: str                   # one line, what it is for
    accent: tuple                   # Parhelia signal hue, per-experience
    available: bool                 # selectable from the launcher
    implemented: bool               # real scene, not a placeholder
    phase: int                      # plan phase that delivers it
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


REGISTRY = [
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


def by_name(name):
    return next((e for e in REGISTRY if e.name == name), None)
