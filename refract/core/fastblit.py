"""Optional C fast path for getting captured frames into GL textures.

Desk's cost is not rendering, it is moving pixels. Pulling three 1080p
streams through PyGObject costs ~19.7 ms/frame before a single texel is
uploaded, because `GstMapInfo.data` is handed back as a Python `bytes` --
an 8 MB allocate-and-copy per stream per frame that exists only to satisfy
the binding. `csrc/refract_blit.c` maps the buffer and passes the pointer
straight to glTexSubImage2D, so the only copy left is the one into the GPU.

Everything here degrades. If the library was never built, or GStreamer's
runtime is missing, or a symbol has moved, `available()` is False and the
caller keeps using `ScreenCapture.latest()`. Nothing in the shell requires
the fast path to exist -- it is a speed-up, not a dependency.

Set REFRACT_NO_FASTBLIT=1 to force the pure-Python path (used to A/B the
frame rate, and the first thing to try if frames ever look wrong).
"""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

SO_NAME = "librefract_blit.so"
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SRC = _REPO / "csrc" / "refract_blit.c"

# Return codes from refract_blit(); negative is a problem.
OK = 1
NO_FRAME = 0
ERR_INIT = -1
ERR_MAP = -2
ERR_SIZE = -3
ERR_CAPS = -4
ERR_PBO = -5

_lib = None
_load_error = None
_loaded = False
_validated = {}          # sink pointer -> True, so the GType check runs once


def so_path():
    """Where the built library lives: next to this file, so it travels with
    the package and does not depend on the repo layout at run time."""
    return _HERE / SO_NAME


def build(quiet=True):
    """Compile the fast path. Returns (ok, message).

    Called by install.sh. Kept in Python rather than a shell script so the
    one place that knows the compiler flags is the one place that loads the
    result.
    """
    if not _SRC.exists():
        return False, "source missing: %s" % _SRC
    cc = os.environ.get("CC", "cc")
    cmd = [cc, "-O2", "-fPIC", "-shared", "-Wall",
           "-o", str(so_path()), str(_SRC), "-lGL"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, "no C compiler (%s); the pure-Python path still works" % cc
    except subprocess.TimeoutExpired:
        return False, "compiler timed out"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "compile failed").strip()
    if not quiet:
        print("built %s" % so_path())
    return True, str(so_path())


def _load():
    global _lib, _load_error, _loaded
    if _loaded:
        return _lib
    _loaded = True

    if os.environ.get("REFRACT_NO_FASTBLIT"):
        _load_error = "disabled by REFRACT_NO_FASTBLIT"
        return None
    path = so_path()
    if not path.exists():
        _load_error = "not built (run install.sh, or python -m " \
                      "refract.core.fastblit --build)"
        return None
    try:
        lib = ctypes.CDLL(str(path))
        lib.refract_blit_init.restype = ctypes.c_int
        lib.refract_blit_init.argtypes = []
        lib.refract_blit.restype = ctypes.c_int
        lib.refract_blit.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        lib.refract_gtype_name.restype = ctypes.c_char_p
        lib.refract_gtype_name.argtypes = [ctypes.c_void_p]
    except OSError as e:
        _load_error = str(e)
        return None

    # Resolving GStreamer's symbols is what actually decides whether the fast
    # path works; a library that loads but cannot find them is no use.
    if not lib.refract_blit_init():
        _load_error = "GStreamer runtime symbols not found"
        return None

    _lib = lib
    return _lib


def available():
    return _load() is not None


def why_unavailable():
    _load()
    return _load_error


def _pointer(sink):
    """The underlying GstAppSink* behind a PyGObject wrapper.

    PyGObject exposes it two ways and they agree; the capsule is the
    documented one, `hash()` is the fallback for older versions.
    """
    cap = getattr(sink, "__gpointer__", None)
    if cap is not None:
        try:
            get = ctypes.pythonapi.PyCapsule_GetPointer
            get.restype = ctypes.c_void_p
            get.argtypes = [ctypes.py_object, ctypes.c_char_p]
            name = ctypes.pythonapi.PyCapsule_GetName
            name.restype = ctypes.c_char_p
            name.argtypes = [ctypes.py_object]
            ptr = get(cap, name(cap))
            if ptr:
                return int(ptr)
        except Exception:                                     # noqa: BLE001
            pass
    try:
        return int(hash(sink))
    except Exception:                                         # noqa: BLE001
        return 0


def _valid_sink(lib, ptr):
    """Confirm the pointer really is a GstAppSink before C dereferences it.

    A wrong pointer here would be a segfault inside GStreamer with a
    meaningless backtrace, so it is worth one GType read the first time each
    sink is seen.
    """
    if ptr in _validated:
        return _validated[ptr]
    name = lib.refract_gtype_name(ctypes.c_void_p(ptr))
    ok = bool(name) and name.decode("ascii", "replace").endswith("AppSink")
    _validated[ptr] = ok
    return ok


def blit(sink, texture_glo, w, h):
    """Upload the newest frame from `sink` into GL texture `texture_glo`.

    Returns (rc, width, height). rc is OK / NO_FRAME / one of the ERR_*
    codes; on ERR_SIZE the width and height are the frame's real ones, so
    the caller can resize the texture and try again next frame.

    Must be called with the GL context current -- it issues GL calls on the
    calling thread, which is the render thread.
    """
    lib = _load()
    if lib is None:
        return ERR_INIT, 0, 0
    ptr = _pointer(sink)
    if not ptr or not _valid_sink(lib, ptr):
        return ERR_INIT, 0, 0
    fw = ctypes.c_int(0)
    fh = ctypes.c_int(0)
    rc = lib.refract_blit(ctypes.c_void_p(ptr), ctypes.c_uint(texture_glo),
                          ctypes.c_int(w), ctypes.c_int(h),
                          ctypes.byref(fw), ctypes.byref(fh))
    return rc, fw.value, fh.value


def _main(argv):
    if "--build" in argv:
        ok, msg = build(quiet=False)
        if not ok:
            print("fastblit: %s" % msg, file=sys.stderr)
            return 1
        # Reload state so --build --check in one go reports the new library.
        global _loaded, _lib, _load_error
        _loaded, _lib, _load_error = False, None, None
    if available():
        print("fastblit: available (%s)" % so_path())
        return 0
    print("fastblit: unavailable -- %s" % why_unavailable())
    return 0 if "--build" not in argv else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
