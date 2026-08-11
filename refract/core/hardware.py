"""Hardware device control: brightness, volume, electrochromic film, duty
cycle, SBS dimension switch -- the controls the XR driver never exposes.

Extracted from viture-hw.py (which remains the standalone CLI). Binds the
newer VITURE SDK (libglasses.so, xr_device_provider_* API) that ships with
XRLinuxDriver.

The rules that make this reliable, all paid for:

- USB access is EXCLUSIVE. `xr_driver_cli --disable` is NOT enough -- the
  xrDriver process keeps the interface claimed. The service must be stopped
  (driver_paused() handles the stop/restore dance). Two SDK clients racing
  for the device segfault in VitureDeviceProvider::ImuReadThread.
- The glasses RE-ENUMERATE on a dimension change: the readback right after
  switch_dimension() often returns -3 (timeout) even though the switch took.
  Trust rc=0 and verify via the display mode later, not the readback.
- libglasses leaves a USB thread running that never joins -- the owning
  process must eventually os._exit() rather than a normal interpreter exit.
  The shell already does (the public SDK's deinit() hangs too).
"""

import contextlib
import ctypes
import glob
import os
import subprocess
import time

SDK = os.path.expanduser("~/.local/share/xr_driver/lib/libglasses.so")
SDK_LIBDIR = os.path.dirname(SDK)
VITURE_VID = "35ca"
SERVICE = "xr-driver"

# Pro XR ranges, from viture_protocol.h Callback::ID docs
RANGES = {"brightness": (0, 6), "volume": (0, 8), "size": (0, 4)}
SIZE_NAMES = {0: "SMALL", 1: "MEDIUM", 2: "LARGE", 3: "EXTRA", 4: "ULTRA"}
DUTY_NAMES = {98: "H", 42: "M", 30: "L"}
DEV_TYPE = {0: "VITURE_GEN1", 1: "VITURE_GEN2", 2: "VITURE_CARINA"}

MODE_2D = 0x31          # 1920x1080
MODE_SBS = 0x32         # 3840x1080 side-by-side


def find_pid():
    """Product ID of the attached VITURE device, from sysfs."""
    for p in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(p, "idVendor")) as f:
                if f.read().strip() != VITURE_VID:
                    continue
            with open(os.path.join(p, "idProduct")) as f:
                pid = int(f.read().strip(), 16)
            if pid != 0x1102:          # skip the microphone interface
                return pid
        except OSError:
            continue
    return None


def driver_running():
    """True if the xrDriver PROCESS is alive.

    NOTE: `xr_driver_cli --disable` only stops the driver *processing* -- the
    process keeps the USB interface claimed. USB access is exclusive, so the
    service must actually be stopped to talk to the glasses.
    """
    try:
        return subprocess.run(["pgrep", "-x", "xrDriver"],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def driver_set(run):
    subprocess.run(["systemctl", "--user", "start" if run else "stop", SERVICE],
                   capture_output=True, text=True, timeout=25)
    time.sleep(2.0)


@contextlib.contextmanager
def driver_paused():
    """Stop xr-driver for exclusive USB access; restore it on the way out
    (only if it was running). Yields False if it could not be stopped."""
    was = driver_running()
    if was:
        driver_set(False)
        if driver_running():
            yield False
            return
    try:
        yield True
    finally:
        if was:
            driver_set(True)


def preload_sdk_deps():
    """libglasses.so NEEDs OpenCV 4.2 sonames that ship next to it in
    SDK_LIBDIR but are not on the loader search path -- and this host has no
    system OpenCV 4.2, so the dlopen fails with 'libopencv_highgui.so.4.2:
    cannot open shared object file'.

    Setting LD_LIBRARY_PATH from here is too late (the loader reads it at
    process start). Instead dlopen the bundled libs with RTLD_GLOBAL first:
    once loaded, they satisfy libglasses' NEEDED entries by soname. Several
    passes because they depend on each other (core <- imgproc <- highgui ...).
    """
    pending = sorted(glob.glob(os.path.join(SDK_LIBDIR, "libopencv_*.so.4.2")))
    for _ in range(len(pending)):
        stuck = [p for p in pending if not _try_load(p)]
        if not stuck or len(stuck) == len(pending):
            return
        pending = stuck


def _try_load(path):
    try:
        ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        return True
    except OSError:
        return False


class Glasses:
    """Direct device handle. The caller is responsible for the driver dance
    (use driver_paused()) -- initialize() fails if xrDriver holds the USB."""

    # 0..4-ish; higher is noisier. 1 keeps errors, drops the [I] flood.
    QUIET = 1

    def __init__(self, pid=None, log_level=QUIET):
        pid = pid if pid is not None else find_pid()
        if pid is None:
            raise RuntimeError("No VITURE device found on USB.")
        if not os.path.exists(SDK):
            raise RuntimeError("SDK not found: %s (install XRLinuxDriver)"
                               % SDK)
        preload_sdk_deps()
        self.lib = ctypes.CDLL(SDK)
        L = self.lib
        L.xr_device_provider_create.argtypes = [ctypes.c_int]
        L.xr_device_provider_create.restype = ctypes.c_void_p
        L.xr_device_provider_initialize.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_char_p]
        L.xr_device_provider_get_film_mode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)]
        L.xr_device_provider_set_film_mode.argtypes = [ctypes.c_void_p,
                                                       ctypes.c_float]
        L.xr_device_provider_get_market_name.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        L.xr_device_provider_get_glasses_version.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        for fn in ("get_brightness_level", "get_volume_level",
                   "get_display_size", "get_display_distance",
                   "get_duty_cycle", "get_display_mode", "get_device_type",
                   "start", "stop", "shutdown", "destroy"):
            getattr(L, "xr_device_provider_" + fn).argtypes = [ctypes.c_void_p]
        for fn in ("set_brightness_level", "set_volume_level",
                   "set_display_size", "set_display_distance",
                   "set_duty_cycle", "set_display_mode"):
            getattr(L, "xr_device_provider_" + fn).argtypes = [ctypes.c_void_p,
                                                               ctypes.c_int]
        L.xr_device_provider_switch_dimension.argtypes = [ctypes.c_void_p,
                                                          ctypes.c_bool]
        # libglasses prints a wall of [I][libglasses] chatter on every
        # command. It has its own knob for this -- no need for the fd
        # redirect the plan assumed. Set it BEFORE create/initialize so the
        # start-up spam is caught too.
        try:
            L.xr_device_provider_set_log_level.argtypes = [ctypes.c_int]
            L.xr_device_provider_set_log_level(int(log_level))
        except Exception:                                 # noqa: BLE001
            pass
        self.pid = pid
        self.h = L.xr_device_provider_create(pid)
        if not self.h:
            raise RuntimeError("xr_device_provider_create(0x%04x) returned "
                               "NULL" % pid)
        r = L.xr_device_provider_initialize(self.h, None)
        if r != 0:
            raise RuntimeError("initialize() failed: %d (is the driver still "
                               "holding the device?)" % r)
        L.xr_device_provider_start(self.h)

    def market_name(self):
        buf = ctypes.create_string_buffer(64)
        n = ctypes.c_int(64)
        self.lib.xr_device_provider_get_market_name(self.pid, buf,
                                                    ctypes.byref(n))
        return buf.value.decode(errors="replace")

    def version(self):
        buf = ctypes.create_string_buffer(128)
        n = ctypes.c_int(128)
        self.lib.xr_device_provider_get_glasses_version(self.h, buf,
                                                        ctypes.byref(n))
        return buf.value.decode(errors="replace")

    def film(self):
        v = ctypes.c_float(0)
        self.lib.xr_device_provider_get_film_mode(self.h, ctypes.byref(v))
        return v.value

    def set_film(self, value):
        return self.lib.xr_device_provider_set_film_mode(
            self.h, ctypes.c_float(float(value)))

    def switch_dimension(self, sbs):
        """rc=0 means it took, even if the next readback times out (-3):
        the glasses re-enumerate on a mode change."""
        return self.lib.xr_device_provider_switch_dimension(
            self.h, ctypes.c_bool(bool(sbs)))

    def g(self, name):
        return getattr(self.lib, "xr_device_provider_get_" + name)(self.h)

    def s(self, name, val):
        return getattr(self.lib, "xr_device_provider_set_" + name)(self.h, val)

    def info(self):
        dt = self.g("device_type")
        return {
            "market_name": self.market_name(),
            "product_id": self.pid,
            "device_type": DEV_TYPE.get(dt, "unknown (%d)" % dt),
            "firmware": self.version(),
            "brightness": self.g("brightness_level"),
            "volume": self.g("volume_level"),
            "duty_cycle": self.g("duty_cycle"),
            "display_mode": self.g("display_mode"),
            "film": self.film(),
        }

    def close(self):
        try:
            self.lib.xr_device_provider_stop(self.h)
            self.lib.xr_device_provider_shutdown(self.h)
            self.lib.xr_device_provider_destroy(self.h)
        except Exception:
            pass
