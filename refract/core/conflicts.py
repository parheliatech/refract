"""Whatever else is holding the glasses: find it, stop it, or remove it.

USB access to the VITURE glasses is EXCLUSIVE. One process holds the
interface and every other one fails -- and the failure is not a tidy "device
busy": the vendor SDK's init() simply returns false, and two SDK clients
racing for the same device have been seen to segfault inside the driver's IMU
thread. So this runs BEFORE Refract touches the hardware, rather than being
error handling afterwards.

The usual squatter is Breezy Desktop -- its GNOME shell extension, plus the
XRLinuxDriver `xrDriver` process underneath it. Those two are recognised by
name here, because knowing what something IS is what makes "uninstall it" a
sentence we can offer. Anything else is still caught, just anonymously, by
looking at who has the glasses' device nodes open.

Two things are worth knowing before changing any of this:

- `xr_driver_cli --disable` is NOT enough. It stops the driver PROCESSING;
  the process keeps the USB interface claimed. The process itself has to go.
- Stopping xrDriver while Breezy's extension is enabled is temporary -- the
  extension starts it again. That is why disabling the extension is part of
  stopping, and why the extension is dealt with first.

Uninstalling runs the vendors' OWN uninstall scripts. We do not delete
another project's files ourselves: they know what they installed, and
Breezy's uninstaller removes XRLinuxDriver as part of its job, so it goes
first and the driver is re-checked rather than removed blind. The one thing
we do handle first is libglasses.so -- it lives inside XRLinuxDriver's
install and Refract's hardware controls fall back to it, so it is copied
somewhere Refract owns before the uninstaller deletes it.
"""

import glob
import os
import shutil
import signal
import subprocess
import sys
import time

VITURE_VID = "35ca"


def _xdg(var, default):
    return os.environ.get(var) or os.path.expanduser(default)


# The vendor scripts honour these, so we have to look where they actually
# installed rather than where the defaults say.
BIN_DIR = _xdg("XDG_BIN_HOME", "~/.local/bin")
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share")

GLASSES_SDK_SRC = os.path.join(DATA_DIR, "xr_driver", "lib")
GLASSES_SDK_DEST = os.path.join(DATA_DIR, "refract", "sdk")

MODES = ("ask", "stop", "uninstall", "ignore")


# --------------------------------------------------------------- plumbing

def _run(cmd, timeout=20, stdio=False):
    """subprocess.run that never raises.

    stdio=True inherits the terminal: the vendor uninstallers shell out to
    sudo for the udev rules and need somewhere to ask for a password.
    """
    try:
        return subprocess.run(
            cmd, timeout=timeout, text=True,
            capture_output=not stdio,
            stdin=None if stdio else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(p):
    return p is not None and p.returncode == 0


def _out(p):
    return (p.stdout or "") if p is not None else ""


def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def _exe(path):
    return path if os.access(path, os.X_OK) else None


def _comm(pid):
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        return ""


def _cmdline(pid, limit=60):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read().decode(errors="replace")
    except OSError:
        return ""
    line = " ".join(p for p in raw.split("\0") if p)
    return line if len(line) <= limit else line[:limit - 1] + "…"


def interactive():
    """True if there is a terminal to ask a question on. Refract is normally
    launched from the app grid, where there is not."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


# ----------------------------------------------------------- who is there

def pids_named(name):
    """PIDs whose comm is exactly `name` (pgrep -x)."""
    p = _run(["pgrep", "-x", name], timeout=10)
    if not _ok(p):
        return []
    return [int(v) for v in _out(p).split() if v.isdigit()]


def pids_matching(pattern):
    """PIDs whose full command line contains `pattern` (pgrep -f).

    Ours are filtered out: this process may well have the pattern on its own
    command line, and killing ourselves to free the glasses would be a poor
    trade.
    """
    p = _run(["pgrep", "-f", pattern], timeout=10)
    if not _ok(p):
        return []
    mine = {os.getpid(), os.getppid()}
    return [int(v) for v in _out(p).split()
            if v.isdigit() and int(v) not in mine]


def kill_pids(pids, log=print, grace=3.0):
    """SIGTERM, wait, then SIGKILL what is left. Returns the survivors.

    Polite first because xrDriver releases the USB interface on the way out
    if it is given the chance; a SIGKILL leaves the kernel to clean up, which
    it does, but only after the device has been reset.
    """
    pids = [p for p in pids if p > 1 and p != os.getpid()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        pids = [p for p in pids if _alive(p)]
        if not pids:
            return []
        time.sleep(0.2)
    for pid in pids:
        log("    %d did not stop, killing it" % pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.5)
    return [p for p in pids if _alive(p)]


def _alive(pid):
    return os.path.exists("/proc/%d" % pid)


def unit_exists(unit):
    p = _run(["systemctl", "--user", "list-unit-files", unit + ".service"])
    return _ok(p) and (unit + ".service") in _out(p)


def unit_active(unit):
    return (_out(_run(["systemctl", "--user", "is-active", unit])).strip()
            == "active")


def extension_enabled(info_text):
    """Parse `gnome-extensions info`.

    Only "Enabled: Yes" matters: an installed-but-disabled extension is not
    holding anything and is not worth interrupting a launch over.
    """
    for line in info_text.splitlines():
        if line.strip().startswith("Enabled:"):
            return line.split(":", 1)[1].strip().lower().startswith("y")
    return False


def hid_is_viture(uevent_text):
    """True if a hidraw's uevent describes the VITURE glasses.

    HID_ID is "BUS:VVVVVVVV:PPPPPPPP", upper case and zero padded --
    0003:000035CA:0000101D for the Pro XR. Parsed into fields rather than
    substring-matched on "35CA", which would also hit an unrelated device
    whose product id happened to contain those digits.
    """
    for line in uevent_text.splitlines():
        if not line.startswith("HID_ID="):
            continue
        parts = line.split("=", 1)[1].strip().split(":")
        if len(parts) != 3:
            return False
        try:
            return int(parts[1], 16) == int(VITURE_VID, 16)
        except ValueError:
            return False
    return False


def glasses_nodes():
    """Device nodes belonging to the glasses -- the hidraw interfaces and the
    usbfs node for the device itself. Empty when nothing is plugged in."""
    nodes = []
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sysdir, "device", "uevent")) as f:
                text = f.read()
        except OSError:
            continue
        if hid_is_viture(text):
            nodes.append("/dev/" + os.path.basename(sysdir))
    for sysdir in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(sysdir, "idVendor")) as f:
                if f.read().strip().lower() != VITURE_VID:
                    continue
            with open(os.path.join(sysdir, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(sysdir, "devnum")) as f:
                dev = int(f.read())
        except (OSError, ValueError):
            continue
        nodes.append("/dev/bus/usb/%03d/%03d" % (bus, dev))
    return nodes


def node_holders(nodes, exclude=()):
    """[(pid, comm, node)] for the processes holding one of `nodes` open.

    Reads /proc directly instead of shelling out to lsof or fuser: neither is
    installed by default on Ubuntu, and a missing tool would silently turn
    this check into "nothing is holding the device", which is the one answer
    it must never give wrongly. Other users' processes are invisible here --
    acceptable, because the drivers that matter run as the logged-in user.
    """
    if not nodes:
        return []
    want = set(nodes)
    skip = set(exclude) | {os.getpid()}
    out = []
    for entry in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(entry))
        except ValueError:
            continue
        if pid in skip:
            continue
        try:
            fds = os.listdir(os.path.join(entry, "fd"))
        except OSError:
            continue                  # another user's, or it just exited
        for fd in fds:
            try:
                target = os.readlink(os.path.join(entry, "fd", fd))
            except OSError:
                continue
            if target in want:
                out.append((pid, _comm(pid), target))
                break
    return out


def still_held(exclude=()):
    """Who has the device open right now -- the check after stopping things,
    and the only one that is ground truth rather than inference."""
    return node_holders(glasses_nodes(), exclude=exclude)


# ------------------------------------------------------------- the things

class Conflict:
    """One thing standing between Refract and the glasses.

    `active` lines mean it is holding the device now, or is about to (an
    enabled extension that starts a driver counts). `dormant` lines mean it
    is merely on disk: harmless this session, back after the next login.
    Only `active` is worth interrupting a launch for.
    """

    key = "?"
    name = "?"
    why = ""

    def __init__(self):
        self.active = []
        self.dormant = []
        self.detect()

    @property
    def present(self):
        return bool(self.active or self.dormant)

    def refresh(self):
        """A freshly detected copy -- what is true after something else ran
        (Breezy's uninstaller takes XRLinuxDriver with it)."""
        return type(self)()

    def detect(self):
        pass

    def removable(self):
        return False

    def stop(self, log=print):
        return True

    def uninstall(self, log=print):
        log("  %s: nothing here knows how to uninstall it" % self.name)
        return False


class BreezyGnome(Conflict):
    """Breezy Desktop's GNOME half: the shell extension and its UI app.

    Handled before XRLinuxDriver because it is what brings the driver back.
    """

    key = "breezy-gnome"
    name = "Breezy Desktop (GNOME)"
    why = ("the shell extension that drives the glasses; while it is enabled "
           "it restarts XRLinuxDriver after anything stops it")

    EXT = "breezydesktop@xronlinux.com"

    def detect(self):
        info = _out(_run(["gnome-extensions", "info", self.EXT]))
        self.enabled = extension_enabled(info)
        installed = "Name:" in info or os.path.isdir(os.path.join(
            DATA_DIR, "gnome-shell", "extensions", self.EXT))
        if self.enabled:
            self.active.append("the %s extension is ENABLED" % self.EXT)
        elif installed:
            self.dormant.append("the %s extension is installed, disabled"
                                % self.EXT)
        self.app_pids = pids_matching(os.path.join(BIN_DIR, "breezydesktop"))
        if self.app_pids:
            self.active.append("the Breezy Desktop app is running (pid %s)"
                               % ", ".join(str(p) for p in self.app_pids))
        files = [p for p in (os.path.join(BIN_DIR, "breezydesktop"),
                             os.path.join(BIN_DIR, "virtualdisplay"),
                             os.path.join(DATA_DIR, "breezydesktop"))
                 if os.path.exists(p)]
        if files:
            self.dormant.append("installed: %s"
                                % ", ".join(_short(p) for p in files))
        self.uninstaller = _exe(os.path.join(BIN_DIR,
                                             "breezy_gnome_uninstall"))

    def removable(self):
        return bool(self.uninstaller)

    def stop(self, log=print):
        ok = True
        if self.enabled:
            log("  disabling the %s extension" % self.EXT)
            log("    this one is a saved setting, not just a running "
                "process -- put it back with:")
            log("      gnome-extensions enable %s" % self.EXT)
            ok = _ok(_run(["gnome-extensions", "disable", self.EXT])) and ok
        if self.app_pids:
            log("  stopping the Breezy Desktop app")
            ok = not kill_pids(self.app_pids, log) and ok
        return ok

    def uninstall(self, log=print):
        if not self.uninstaller:
            log("  %s: no uninstaller at %s"
                % (self.name, _short(os.path.join(BIN_DIR,
                                                  "breezy_gnome_uninstall"))))
            return False
        preserve_glasses_sdk(log)
        self.stop(log)
        log("  running %s" % _short(self.uninstaller))
        log("    it removes XRLinuxDriver too, needs sudo for the udev "
            "rules, and reports the uninstall to the vendor's analytics "
            "-- that is their script, not ours")
        p = _run([self.uninstaller], timeout=300, stdio=interactive())
        if p is None or p.returncode != 0:
            log("  the uninstaller failed%s"
                % ("" if p is None else " (rc=%d)" % p.returncode))
            for line in _out(p).splitlines()[-6:]:
                log("    %s" % line)
            return False
        log("  Breezy Desktop removed")
        return True


class XRLinuxDriver(Conflict):
    """The device driver under Breezy Desktop. This is the one that actually
    claims the USB interface."""

    key = "xr-driver"
    name = "XRLinuxDriver"
    why = ("the driver underneath Breezy Desktop; it claims the glasses' USB "
           "interface exclusively")

    UNIT = "xr-driver"
    PROC = "xrDriver"

    def detect(self):
        self.pids = pids_named(self.PROC)
        if self.pids:
            self.active.append("%s is running (pid %s)"
                               % (self.PROC,
                                  ", ".join(str(p) for p in self.pids)))
        self.unit = unit_exists(self.UNIT)
        if self.unit and unit_active(self.UNIT):
            self.active.append("the %s user service is active" % self.UNIT)
        elif self.unit:
            self.dormant.append("the %s user service is installed" % self.UNIT)
        files = [p for p in (os.path.join(BIN_DIR, "xrDriver"),
                             os.path.join(BIN_DIR, "xr_driver_cli"),
                             os.path.join(DATA_DIR, "xr_driver"))
                 if os.path.exists(p)]
        if files:
            self.dormant.append("installed: %s"
                                % ", ".join(_short(p) for p in files))
        self.uninstaller = _exe(os.path.join(BIN_DIR, "xr_driver_uninstall"))

    def removable(self):
        return bool(self.uninstaller)

    def stop(self, log=print):
        # NOT `xr_driver_cli --disable`: that stops the driver processing and
        # leaves the process holding the interface, which looks like success
        # and is not. Stop the unit, then make sure the process is gone.
        if self.unit and unit_active(self.UNIT):
            log("  stopping the %s user service" % self.UNIT)
            _run(["systemctl", "--user", "stop", self.UNIT], timeout=45)
            time.sleep(1.0)
        left = pids_named(self.PROC)
        if left:
            log("  stopping %s (pid %s)"
                % (self.PROC, ", ".join(str(p) for p in left)))
            left = kill_pids(left, log)
        return not left

    def uninstall(self, log=print):
        if not self.uninstaller:
            log("  %s: no uninstaller at %s"
                % (self.name, _short(os.path.join(BIN_DIR,
                                                  "xr_driver_uninstall"))))
            return False
        preserve_glasses_sdk(log)
        self.stop(log)
        log("  running %s" % _short(self.uninstaller))
        log("    it needs sudo for the udev rules, and reports the "
            "uninstall to the vendor's analytics -- that is their script, "
            "not ours")
        p = _run([self.uninstaller], timeout=300, stdio=interactive())
        if p is None or p.returncode != 0:
            log("  the uninstaller failed%s"
                % ("" if p is None else " (rc=%d)" % p.returncode))
            for line in _out(p).splitlines()[-6:]:
                log("    %s" % line)
            return False
        log("  XRLinuxDriver removed")
        return True


class ForeignHolder(Conflict):
    """Something we do not recognise with the device open.

    Named by whatever /proc says, which is often just "python3" -- a Refract
    that crashed without clearing its pid file looks exactly like this. There
    is no uninstaller for an unknown thing, only a stop.
    """

    key = "holder"
    why = "it has one of the glasses' device nodes open"

    def __init__(self, pid, comm, node):
        self.pid, self.comm, self.node = pid, comm, node
        self.name = "%s (pid %d)" % (comm or "unknown", pid)
        super().__init__()

    def refresh(self):
        return ForeignHolder(self.pid, self.comm, self.node)

    def detect(self):
        self.active.append("holds %s open" % self.node)
        cmd = _cmdline(self.pid)
        if cmd:
            self.active.append(cmd)

    def stop(self, log=print):
        log("  stopping %s" % self.name)
        return not kill_pids([self.pid], log)


# --------------------------------------------------------------- the scan

def scan(exclude_pids=()):
    """Everything in the way, in the order it has to be dealt with.

    Breezy's extension first: it is what restarts XRLinuxDriver, so stopping
    the driver before disabling the extension only loses the race. Unknown
    holders last, and never twice -- the pids the named drivers already
    account for are excluded so xrDriver is not also reported as a stranger.
    """
    found = [c for c in (BreezyGnome(), XRLinuxDriver()) if c.present]
    known = set(exclude_pids)
    for c in found:
        known.update(getattr(c, "pids", ()) or ())
        known.update(getattr(c, "app_pids", ()) or ())
    for pid, comm, node in node_holders(glasses_nodes(), exclude=known):
        found.append(ForeignHolder(pid, comm, node))
    return found


def active(found):
    return [c for c in found if c.active]


def report(found, log=print):
    """What was found, wrapped to a terminal width nobody has to widen.

    `*` is something happening now, `-` something merely installed. The
    difference is the whole decision: only `*` has to be dealt with before
    Refract can start.
    """
    import textwrap
    for c in found:
        log("    %s" % c.name)
        for line in textwrap.wrap(c.why, 66):
            log("      %s" % line)
        for mark, lines in (("*", c.active), ("-", c.dormant)):
            for line in lines:
                wrapped = textwrap.wrap(line, 64)
                log("      %s %s" % (mark, wrapped[0] if wrapped else line))
                for cont in wrapped[1:]:
                    log("        %s" % cont)


def preserve_glasses_sdk(log=print):
    """Copy libglasses.so out of XRLinuxDriver before removing it.

    Brightness, volume, the electrochromic film and the SBS dimension switch
    go through libglasses.so, which is VITURE's and ships only inside
    XRLinuxDriver's installer -- uninstalling that driver takes the library
    with it and those controls go quiet. So it is copied somewhere Refract
    owns first. tools/import-glasses-sdk.sh is the same copy by hand. Head
    tracking needs none of it: that runs on the public SDK vendored in sdk/.
    """
    if not os.path.exists(os.path.join(GLASSES_SDK_SRC, "libglasses.so")):
        return False
    if os.path.exists(os.path.join(GLASSES_SDK_DEST, "libglasses.so")):
        return True                                    # already ours
    try:
        shutil.copytree(GLASSES_SDK_SRC, GLASSES_SDK_DEST, dirs_exist_ok=True)
    except OSError as e:
        log("  could not keep a copy of libglasses.so: %s" % e)
        log("  brightness, volume, film and the dimension switch will stop "
            "working once the driver is gone")
        return False
    log("  kept libglasses.so: copied %s -> %s"
        % (_short(GLASSES_SDK_SRC), _short(GLASSES_SDK_DEST)))
    return True


def stop_all(found, log=print):
    ok = True
    for c in found:
        if not c.stop(log):
            log("  could not stop %s" % c.name)
            ok = False
    return ok


def uninstall_all(found, log=print):
    """Stop everything, then run the vendors' uninstallers.

    Each one is re-detected immediately before it runs: Breezy's uninstaller
    removes XRLinuxDriver as part of its own job, so by the time we get to
    the driver there may be nothing left to remove.
    """
    stop_all(found, log)
    removable = [c for c in found if c.removable()]
    if not removable:
        log("  nothing found here ships an uninstaller -- stopped only")
        return False
    ok = True
    for c in removable:
        fresh = c.refresh()
        if not fresh.present:
            log("  %s: already gone" % c.name)
            continue
        if not fresh.uninstall(log):
            ok = False
    left = glob.glob("/usr/lib/udev/rules.d/70-*-xr.rules") + \
        glob.glob("/etc/udev/rules.d/70-*-xr.rules")
    if left:
        log("  the driver's udev rules are still there (removing them needs "
            "root):")
        log("      sudo rm %s" % " ".join(left))
        log("  they are harmless -- they only widen access to the glasses")
    return ok


# ---------------------------------------------------------------- the ask

PROMPT = [
    ("s", "stop",      "stop them for now -- reversible, they come back "
                       "at the next login"),
    ("u", "uninstall", "stop them AND uninstall them, using their own "
                       "uninstallers"),
    ("c", "continue",  "leave them alone and start anyway -- the IMU will "
                       "probably fail"),
    ("q", "quit",      "quit and change nothing"),
]


def ask_terminal(log=print):
    for key, _name, text in PROMPT:
        log("    [%s] %s" % (key, text))
    log("")
    while True:
        try:
            reply = input("  choice [s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log("")
            return "quit"
        if not reply:
            return "stop"
        for key, name, _text in PROMPT:
            if reply == key or reply == name:
                return name
        log("  s, u, c or q.")


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ask_desktop(found):
    """No terminal -- Refract is normally launched from the app grid -- so
    ask with a GNOME dialog instead. None if there is no zenity to ask with.
    """
    if not shutil.which("zenity"):
        return None
    lines = ["Something else is holding the VITURE glasses.", "",
             "USB access to them is exclusive, so Refract cannot read the "
             "IMU or switch to side-by-side while this is running:", ""]
    for c in active(found):
        lines.append("• %s" % c.name)
        for line in c.active:
            lines.append("    %s" % line)
    p = _run(["zenity", "--question", "--title=Refract", "--width=560",
              "--text=" + _escape("\n".join(lines)),
              "--ok-label=Stop them",
              "--cancel-label=Quit",
              "--extra-button=Stop and uninstall",
              "--extra-button=Start anyway"], timeout=600)
    if p is None:
        return None
    pressed = _out(p).strip()
    if p.returncode == 0:
        return "stop"
    if pressed.startswith("Stop and uninstall"):
        return "uninstall"
    if pressed.startswith("Start anyway"):
        return "continue"
    return "quit"


def check(mode="ask", log=print):
    """The start-up check. True to carry on, False to quit.

    mode is one of MODES: ask (default), stop, uninstall, ignore.
    """
    if mode == "ignore":
        return True
    found = scan()
    if not found:
        return True
    live = active(found)
    if not live:
        # Installed but not running. Not a conflict today; say so in one
        # line and get out of the way rather than interrupting a launch.
        log("  conflicts    : %s installed but not running"
            % ", ".join(c.name for c in found))
        log("                 remove for good: python -m "
            "refract.core.conflicts --uninstall")
        return True

    log("")
    log("  ! Something else is holding the glasses.")
    log("    USB access is exclusive: while this is running Refract cannot")
    log("    read the IMU or switch the display to side-by-side.")
    log("")
    report(found, log)
    log("")

    choice = mode if mode in ("stop", "uninstall") else None
    if choice is None:
        choice = ask_terminal(log) if interactive() else ask_desktop(found)
    if choice is None:
        # Launched with no terminal and no zenity: there is no way to ask,
        # and starting into a guaranteed failure helps nobody. Take the
        # reversible option and say loudly what was done.
        log("  no terminal and no zenity to ask with -- stopping them, "
            "which is reversible")
        choice = "stop"

    if choice == "quit":
        log("  quitting; nothing was changed")
        return False
    if choice == "continue":
        log("  starting anyway -- expect the IMU to fail")
        return True
    if choice == "uninstall":
        uninstall_all(found, log)
    else:
        stop_all(found, log)

    left = still_held()
    if left:
        log("  still held by: %s"
            % ", ".join("%s (pid %d)" % (c or "unknown", p)
                        for p, c, _n in left))
    else:
        log("  glasses       : free")
    return True


# -------------------------------------------------------------------- cli

def main(argv=None):
    """python -m refract.core.conflicts [--stop | --uninstall]"""
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m refract.core.conflicts",
        description="Find (and optionally remove) other XR drivers holding "
                    "the VITURE glasses.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--stop", action="store_true",
                   help="stop them now; reversible")
    g.add_argument("--uninstall", action="store_true",
                   help="stop them and run their own uninstallers")
    a = ap.parse_args(argv)

    found = scan()
    if not found:
        print("  nothing else is holding the glasses")
        nodes = glasses_nodes()
        if not nodes:
            print("  (no VITURE device on USB right now, so only installs "
                  "were checked)")
        return 0
    print("  found:")
    report(found)
    if a.uninstall:
        print("")
        uninstall_all(found)
    elif a.stop:
        print("")
        stop_all(found)
    else:
        print("")
        print("  --stop       stop them for now")
        print("  --uninstall  stop them and remove them for good")
        return 1 if active(found) else 0
    left = still_held()
    if left:
        print("  still held by: %s"
              % ", ".join("%s (pid %d)" % (c or "unknown", p)
                          for p, c, _n in left))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
