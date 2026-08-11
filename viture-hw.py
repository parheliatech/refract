#!/usr/bin/env python3
"""
viture-hw -- hardware device control for VITURE XR glasses on Linux x86_64.

Brightness, volume, electrochromic film, display size/distance and duty cycle:
the controls Breezy Desktop does NOT expose. Binds the newer VITURE SDK
(libglasses.so, xr_device_provider_* API) that ships with XRLinuxDriver.

    ./viture-hw.py info
    ./viture-hw.py brightness [0-6]
    ./viture-hw.py volume     [0-8]
    ./viture-hw.py film       [0.0-1.0]     electrochromic dimming
    ./viture-hw.py size       [0-4]         SMALL MEDIUM LARGE EXTRA ULTRA
    ./viture-hw.py distance   [int]
    ./viture-hw.py duty       [30|42|98]    L / M / H

IMPORTANT -- USB access is EXCLUSIVE. The XR driver holds the glasses while
enabled, so this tool briefly disables it, does the work, then re-enables it.
Use --no-driver-toggle to skip that if the driver is already off.
"""

import ctypes
import os
import subprocess
import sys
import time

SDK = os.path.expanduser("~/.local/share/xr_driver/lib/libglasses.so")
SDK_LIBDIR = os.path.dirname(SDK)
CLI = os.path.expanduser("~/.local/bin/xr_driver_cli")
VITURE_VID = "35ca"

# Pro XR ranges, from viture_protocol.h Callback::ID docs
RANGES = {"brightness": (0, 6), "volume": (0, 8), "size": (0, 4)}
SIZE_NAMES = {0: "SMALL", 1: "MEDIUM", 2: "LARGE", 3: "EXTRA", 4: "ULTRA"}
DUTY_NAMES = {98: "H", 42: "M", 30: "L"}
DEV_TYPE = {0: "VITURE_GEN1", 1: "VITURE_GEN2", 2: "VITURE_CARINA"}


def find_pid():
    """Product ID of the attached VITURE device, from sysfs."""
    import glob
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


SERVICE = "xr-driver"


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
    import glob
    pending = sorted(glob.glob(os.path.join(SDK_LIBDIR, "libopencv_*.so.4.2")))
    for _ in range(len(pending)):
        stuck = [p for p in pending
                 if not _try_load(p)]
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
    def __init__(self, pid):
        preload_sdk_deps()
        self.lib = ctypes.CDLL(SDK)
        L = self.lib
        L.xr_device_provider_create.argtypes = [ctypes.c_int]
        L.xr_device_provider_create.restype = ctypes.c_void_p
        L.xr_device_provider_initialize.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.xr_device_provider_get_film_mode.argtypes = [ctypes.c_void_p,
                                                       ctypes.POINTER(ctypes.c_float)]
        L.xr_device_provider_set_film_mode.argtypes = [ctypes.c_void_p, ctypes.c_float]
        L.xr_device_provider_get_market_name.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        L.xr_device_provider_get_glasses_version.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        for fn in ("get_brightness_level", "get_volume_level", "get_display_size",
                   "get_display_distance", "get_duty_cycle", "get_display_mode",
                   "get_device_type", "start", "stop", "shutdown", "destroy"):
            getattr(L, "xr_device_provider_" + fn).argtypes = [ctypes.c_void_p]
        for fn in ("set_brightness_level", "set_volume_level", "set_display_size",
                   "set_display_distance", "set_duty_cycle", "set_display_mode"):
            getattr(L, "xr_device_provider_" + fn).argtypes = [ctypes.c_void_p,
                                                               ctypes.c_int]
        L.xr_device_provider_switch_dimension.argtypes = [ctypes.c_void_p,
                                                          ctypes.c_bool]
        self.pid = pid
        self.h = L.xr_device_provider_create(pid)
        if not self.h:
            sys.exit("xr_device_provider_create(0x%04x) returned NULL" % pid)
        r = L.xr_device_provider_initialize(self.h, None)
        if r != 0:
            sys.exit("initialize() failed: %d (is the driver still holding the device?)" % r)
        L.xr_device_provider_start(self.h)

    def market_name(self):
        buf = ctypes.create_string_buffer(64)
        n = ctypes.c_int(64)
        self.lib.xr_device_provider_get_market_name(self.pid, buf, ctypes.byref(n))
        return buf.value.decode(errors="replace")

    def version(self):
        buf = ctypes.create_string_buffer(128)
        n = ctypes.c_int(128)
        self.lib.xr_device_provider_get_glasses_version(self.h, buf, ctypes.byref(n))
        return buf.value.decode(errors="replace")

    def film(self):
        v = ctypes.c_float(0)
        self.lib.xr_device_provider_get_film_mode(self.h, ctypes.byref(v))
        return v.value

    def g(self, name):
        return getattr(self.lib, "xr_device_provider_get_" + name)(self.h)

    def s(self, name, val):
        return getattr(self.lib, "xr_device_provider_set_" + name)(self.h, val)

    def close(self):
        try:
            self.lib.xr_device_provider_stop(self.h)
            self.lib.xr_device_provider_shutdown(self.h)
            self.lib.xr_device_provider_destroy(self.h)
        except Exception:
            pass


def cmd_info(g):
    dt = g.g("device_type")
    print("  market name      : %s" % g.market_name())
    print("  product id       : 0x%04x" % g.pid)
    print("  device type      : %s" % DEV_TYPE.get(dt, "unknown (%d)" % dt))
    print("  firmware         : %s" % g.version())
    print("  brightness       : %s   (range 0-6)" % g.g("brightness_level"))
    print("  volume           : %s   (range 0-8)" % g.g("volume_level"))
    sz = g.g("display_size")
    print("  display size     : %s" % (
        "%s %s" % (sz, SIZE_NAMES.get(sz, "")) if sz >= 0
        else "unsupported on this model (rc=%d)" % sz))
    dd = g.g("display_distance")
    print("  display distance : %s" % (
        dd if dd >= 0 else "unsupported on this model (rc=%d)" % dd))
    dc = g.g("duty_cycle")
    print("  duty cycle       : %s   %s" % (dc, DUTY_NAMES.get(dc, "")))
    print("  display mode     : 0x%02x" % g.g("display_mode"))
    print("  electrochromic   : %.2f" % g.film())


def main():
    args = sys.argv[1:]
    toggle = "--no-driver-toggle" not in args
    args = [a for a in args if a != "--no-driver-toggle"]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not os.path.exists(SDK):
        sys.exit("SDK not found: %s\n(install XRLinuxDriver first)" % SDK)

    pid = find_pid()
    if pid is None:
        sys.exit("No VITURE device found on USB. Plug the glasses in.")

    cmd, val = args[0], (args[1] if len(args) > 1 else None)

    was_enabled = False
    if toggle and driver_running():
        print("  [stopping xr-driver.service for exclusive USB access]",
              flush=True)
        was_enabled = True
        driver_set(False)
        if driver_running():
            sys.exit("  could not stop xr-driver.service; "
                     "run: systemctl --user stop xr-driver")

    g = None
    rc = 0
    try:
        g = Glasses(pid)
        print()
        if cmd == "info":
            cmd_info(g)
        elif cmd == "3d":
            if val is None:
                m = g.g("display_mode")
                print("  display mode: 0x%02x  (%s)" % (
                    m, "3D / side-by-side" if m == 0x32 else
                       "2D" if m == 0x31 else "other"))
            else:
                want = val.lower() in ("on", "1", "true")
                before = g.g("display_mode")
                r = g.lib.xr_device_provider_switch_dimension(g.h,
                                                              ctypes.c_bool(want))
                time.sleep(1.0)
                after = g.g("display_mode")
                print("  switch_dimension(%s) -> rc=%d %s" % (
                    want, r, "OK" if r == 0 else "FAILED"))
                # the glasses re-enumerate on a mode change, so the readback
                # right after often times out (-3) even though the switch took.
                fmt = (lambda m: "0x%02x" % m if m >= 0
                       else "readback timed out (rc=%d, device re-enumerating)" % m)
                print("  display mode: %s -> %s" % (fmt(before), fmt(after)))
                print("  (0x31 = 2D 1920x1080, 0x32 = 3D SBS 3840x1080)")
                print("  verify with:  xrandr | grep -A1 '^DP-1'")
        elif cmd == "film":
            if val is None:
                print("  electrochromic film: %.2f" % g.film())
            else:
                r = g.lib.xr_device_provider_set_film_mode(g.h, ctypes.c_float(float(val)))
                print("  set film -> %d   now %.2f" % (r, g.film()))
        elif cmd in ("brightness", "volume", "size", "distance", "duty"):
            key = {"brightness": "brightness_level", "volume": "volume_level",
                   "size": "display_size", "distance": "display_distance",
                   "duty": "duty_cycle"}[cmd]
            if val is None:
                cur = g.g(key)
                extra = SIZE_NAMES.get(cur, "") if cmd == "size" else \
                        DUTY_NAMES.get(cur, "") if cmd == "duty" else ""
                print("  %s: %s %s" % (cmd, cur, extra))
            else:
                v = int(val)
                if cmd in RANGES:
                    lo, hi = RANGES[cmd]
                    if not lo <= v <= hi:
                        sys.exit("  %s must be %d-%d" % (cmd, lo, hi))
                r = g.s(key, v)
                time.sleep(0.3)
                print("  set %s=%d -> rc=%d   now %s" % (cmd, v, r, g.g(key)))
        else:
            sys.exit("unknown command: %s (try --help)" % cmd)
        print()
    except SystemExit as e:
        # os._exit() below skips Python's normal exit path, which would
        # otherwise print this message -- surface it here or failures are
        # completely silent.
        if e.code:
            print("  ERROR: %s" % e.code, file=sys.stderr, flush=True)
        rc = 1 if e.code else 0
    except BaseException:
        # same reason: os._exit() skips the interpreter's traceback printing,
        # so an ordinary exception would vanish without a trace.
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        if g:
            g.close()
        if was_enabled:
            print("  [restarting xr-driver.service]", flush=True)
            driver_set(True)
        sys.stdout.flush()
        sys.stderr.flush()
        # hard exit: the SDK leaves a USB thread running that never joins
        os._exit(rc)


if __name__ == "__main__":
    main()
