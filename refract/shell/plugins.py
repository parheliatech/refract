"""Drop-in sub-experiences.

A sub-experience no longer has to be a commit to refract/shell/registry.py.
Any folder placed inside the refract package directory -- next to desk/,
play/, three60/ -- that carries an `experience.toml` (or `experience.json`)
manifest is discovered at startup, added to REGISTRY, and shows up in the
HUD quick-switch row and on the home screen exactly like a built-in.

    refract/
      stars/
        experience.toml
        scene.py

    # experience.toml
    title    = "Stars"
    subtitle = "A quiet planetarium"
    scene    = "scene:StarsScene"   # "<module>:<attr>" relative to the
                                    # plugin folder; attr is a Scene subclass
                                    # or any zero-argument callable that
                                    # returns one. ":attr" defaults to Scene.
    accent   = [120, 180, 255]      # optional RGB tile hue

Extra search roots can be listed in $REFRACT_PLUGIN_PATH (os.pathsep-
separated) for plugins kept outside the package tree. Each folder found
there is imported as the package `refract_plugins.<folder>` -- NOT by
putting the root on sys.path, which would let a folder named `json` or
`numpy` replace the real module for the whole process. Inside a plugin,
import its own files relatively (`from . import util`).

Discovery never lets one bad plugin take the shell down: a manifest that
will not parse, an import that raises, a missing 'scene' key, an id that
collides with a built-in -- each is reported to stdout and skipped, and the
rest of the shell comes up. Set REFRACT_PLUGIN_DEBUG=1 for tracebacks.

The scene module is imported LAZILY, only when the tile is launched -- a
plugin that needs GStreamer or a GPU model must not pay that cost just to
draw a launcher tile, the same rule the built-in Desk factory follows.
"""

import importlib
import json
import os
import sys
import traceback

try:
    import tomllib                       # stdlib, 3.11+
except ModuleNotFoundError:              # pragma: no cover
    tomllib = None

MANIFEST_NAMES = ("experience.toml", "experience.json")


def _pkg_dir():
    import refract
    return os.path.dirname(os.path.abspath(refract.__file__))


EXTERNAL_PKG = "refract_plugins"


def _roots():
    """(directory, import_prefix) pairs: the package dir first, then any
    $REFRACT_PLUGIN_PATH entries, whose folders import under EXTERNAL_PKG."""
    roots = [(_pkg_dir(), "refract.")]
    for extra in os.environ.get("REFRACT_PLUGIN_PATH", "").split(os.pathsep):
        extra = extra.strip()
        if extra and os.path.isdir(extra):
            roots.append((os.path.abspath(extra), EXTERNAL_PKG + "."))
    return roots


def _register_external(import_name, folder_path):
    """Make `refract_plugins.<folder>` importable as a package rooted at
    folder_path, whether or not it has an __init__.py. Nothing goes on
    sys.path, so no plugin folder can shadow another module."""
    import importlib.machinery
    import importlib.util
    import types
    if EXTERNAL_PKG not in sys.modules:
        parent = types.ModuleType(EXTERNAL_PKG)
        parent.__path__ = []
        sys.modules[EXTERNAL_PKG] = parent
    init = os.path.join(folder_path, "__init__.py")
    if os.path.isfile(init):
        spec = importlib.util.spec_from_file_location(
            import_name, init, submodule_search_locations=[folder_path])
    else:
        spec = importlib.machinery.ModuleSpec(import_name, None,
                                              is_package=True)
        spec.submodule_search_locations = [folder_path]
    mod = importlib.util.module_from_spec(spec)
    sys.modules[import_name] = mod
    if spec.loader is not None:
        spec.loader.exec_module(mod)
    return mod


def _load_manifest(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.endswith(".toml"):
        if tomllib is None:                              # pragma: no cover
            raise RuntimeError("no tomllib in this Python; "
                               "use experience.json")
        return tomllib.loads(raw.decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _make_factory(import_name, spec, folder_path=None):
    """spec is '<module>:<attr>'. Returns a zero-arg factory that imports
    the module only when called. folder_path is given for external plugins,
    whose package has to be registered by hand (see _register_external)."""
    mod_rel, _, attr = spec.partition(":")
    attr = attr or "Scene"
    mod_name = "%s.%s" % (import_name, mod_rel) if mod_rel else import_name

    def factory():
        if folder_path is not None and import_name not in sys.modules:
            _register_external(import_name, folder_path)
        mod = importlib.import_module(mod_name)
        obj = getattr(mod, attr)
        return obj() if callable(obj) else obj

    return factory


def discover(claimed=()):
    """Every plugin sub-experience on the search roots, sorted by id.

    claimed: ids already taken by a built-in -- a folder reusing one is
    reported and skipped rather than silently shadowing it.
    """
    from refract.shell.registry import ACCENT_DEFAULT, SubExperience

    importlib.invalidate_caches()        # a freshly dropped-in folder
    seen = set(claimed)
    found = []
    for root, prefix in _roots():
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for folder in entries:
            if folder.startswith((".", "_")):
                continue
            fdir = os.path.join(root, folder)
            manifest = next(
                (os.path.join(fdir, n) for n in MANIFEST_NAMES
                 if os.path.isfile(os.path.join(fdir, n))), None)
            if manifest is None:                 # not a plugin folder
                continue
            try:
                m = _load_manifest(manifest)
                name = str(m.get("name") or folder)
                if name in seen:
                    raise ValueError("id %r is already taken -- "
                                     "rename the folder" % name)
                if not m.get("scene"):
                    raise ValueError("manifest has no 'scene' key")
                accent = tuple(m.get("accent") or ACCENT_DEFAULT)
                entry = SubExperience(
                    name=name,
                    title=str(m.get("title") or name.title()),
                    subtitle=str(m.get("subtitle") or ""),
                    accent=accent,
                    available=bool(m.get("available", True)),
                    implemented=True,
                    phase=int(m.get("phase", 0)),
                    scene_factory=_make_factory(
                        prefix + folder, str(m["scene"]),
                        fdir if prefix == EXTERNAL_PKG + "." else None),
                )
            except Exception as e:                       # noqa: BLE001
                print("  plugin: skipped %s/ -- %s" % (folder, e),
                      flush=True)
                if os.environ.get("REFRACT_PLUGIN_DEBUG"):
                    traceback.print_exc()
                continue
            seen.add(name)
            found.append(entry)
            print("  plugin: %s (%s)" % (name, entry.title), flush=True)
    return found
