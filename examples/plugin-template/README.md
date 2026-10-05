# Refract plugin template

A drop-in sub-experience. Two files:

| file | what it is |
|---|---|
| `experience.toml` | the manifest Refract scans for — title, subtitle, accent, and which class is the scene |
| `scene.py` | the scene itself, an `refract.core.render.Scene` subclass |

## Install

```
cp -r examples/plugin-template refract/hello
python -m refract
```

"Hello" now appears in the HUD quick-switch row and on the home screen, and
`python -m refract --scene hello` boots straight into it. Removing the folder
removes the tile — there is nothing else to unregister.

Folders outside the package tree work too: put them anywhere and point
`REFRACT_PLUGIN_PATH` at the containing directory (`os.pathsep`-separated for
more than one). Each such folder is imported as the package
`refract_plugins.<folder>`, so to split a plugin across several files, import
them relatively (`from . import util`), not by the folder's name.

## The manifest

```toml
title    = "Hello"                    # tile / chip label
subtitle = "Example drop-in plugin"   # one line under it
scene    = "scene:HelloScene"         # "<module>:<attr>" relative to this folder
accent   = [120, 180, 255]            # optional RGB tile hue, 0-255
# name      = "hello"                  # id override (default: folder name)
# available = true                     # false => greyed-out tile
# phase     = 0                        # shown only when available = false
```

`scene` is imported **lazily**, only when the tile is launched — a plugin
that pulls in GStreamer or a GPU model does not pay that cost just to draw a
launcher tile.

## Rules that still apply

- The home screen stays a plain list of tiles. Settings belong in the HUD:
  return a `settings_schema()` from your scene (see
  `refract/desk/scene.py`) and they render into the HUD panel automatically.
- Scenes run in-process (architecture decision A1). A crash in your scene
  takes the shell down with it — for now that is accepted, not guarded.
- A folder whose id collides with a built-in (`desk`, `three60`, `play`,
  `tak`) is ignored with a warning; rename it.
