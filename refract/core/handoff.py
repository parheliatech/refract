"""Display Handoff -- getting out of the way, and coming back.

PARK gives the machine back: the capture sessions stop, the desktop's monitor
layout is restored, the glasses drop out of side-by-side to an ordinary 2D
display, and our window gets out of the way. RESUME puts it all back.

Built from a scene's own exit() and enter() rather than a second teardown
path. It also parks automatically when the glasses are unplugged or their
display drops out (poll_device), and restores 2D at exit (leave_2d_on_exit).
"""

import shutil
import subprocess
import time


def park(app, reason=""):
    """Hand the desktop back. Safe to call twice."""
    if app.parked:
        return True
    t0 = time.time()
    scene = app.scene
    if scene:
        try:
            scene.park(app)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: scene park failed: %s" % e, flush=True)

    # SBS off LAST: the glasses re-enumerate on a dimension change, which
    # kills any mirror stream still running.
    if app.sbs_ours and app.head:
        try:
            app.head.set_sbs(False)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: sbs off failed: %s" % e, flush=True)

    # The window is NOT minimized: under Wayland an app cannot un-minimize
    # itself, so resume() would come back to a hidden window. It stays
    # where it is -- fullscreen on the glasses' display, out of the
    # laptop's way -- showing black, and rendering stops while parked.
    try:
        app.blank_window()
    except Exception:                                     # noqa: BLE001
        pass

    app.parked = True
    print("  handoff: parked in %.1fs%s"
          % (time.time() - t0, (" (%s)" % reason) if reason else ""),
          flush=True)
    return True


def resume(app):
    """Take it back. Safe to call twice."""
    if not app.parked:
        return True
    t0 = time.time()
    if app.sbs_ours and app.head:
        try:
            app.head.set_sbs(True)
            from refract.core import displaymode
            # wait for the mode to actually appear: the switch is asynchronous
            # and the readback right after it times out even on success
            app.sbs_ok = displaymode.wait_for_mode(app.monitor, tries=16,
                                                   delay=0.25)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: sbs on failed: %s" % e, flush=True)

    # a lost output may have moved our window onto the laptop panel
    try:
        app.reassert_output()
    except Exception as e:                                # noqa: BLE001
        print("  handoff: reassert output failed: %s" % e, flush=True)

    scene = app.scene
    if scene:
        try:
            scene.unpark(app)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: scene unpark failed: %s" % e, flush=True)

    app.parked = False
    app.recenter()
    print("  handoff: resumed in %.1fs" % (time.time() - t0), flush=True)
    return True


def toggle(app):
    return resume(app) if app.parked else park(app)


# seconds between checks that the glasses are still attached
DEVICE_POLL = 2.0

# How long a loss has to hold before it is trusted: reseating a USB-C cable
# flaps absent/present for a couple of seconds, and parking on every blip
# tears Desk down repeatedly. A recovery is trusted immediately.
CONFIRM_UNPLUG = 3.0


def _output_healthy(app):
    """Is the glasses VIDEO output still present and in the side-by-side mode
    the renderer is built around?

    USB presence is not enough: a brief DP dropout makes the compositor move
    our window onto the laptop panel while the USB side stays put.

    Conservative: any error querying the compositor counts as healthy. Only
    "the connector is gone" or "it came back but not in SBS" is unhealthy,
    and that is still debounced by CONFIRM_UNPLUG.
    """
    if app.windowed or not app.head:
        return True
    from refract.core import displaymode
    try:
        _, _, monitors, _, _ = displaymode.get_state()
    except Exception:                                     # noqa: BLE001
        return True
    conn = displaymode.find_glasses(monitors)
    if not conn:
        return False
    if app.parked or not getattr(app, "sbs_ok", True):
        # Parked means 2D on purpose. And if side-by-side could not be
        # reached at all (the glasses refused it), 2D is the state we are in,
        # not a fault -- treating it as one parks, fails to resume into SBS,
        # and parks again, every few seconds.
        return True
    return displaymode.current_mode(monitors, conn) == displaymode.SBS_MODE


def _die_with_parent():
    """preexec_fn: have the kernel SIGTERM this child when its parent dies.

    The normal shutdown path closes the dialog itself; this covers a crash
    or SIGKILL, which would otherwise leave it orphaned. Linux only.
    """
    try:
        import ctypes
        import signal
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
    except Exception:                                     # noqa: BLE001
        pass


def _ask_quit_on_unplug(app, reason="unplugged"):
    """Put up a non-blocking dialog asking whether to quit, after the glasses
    connection dropped and parked us.

    Losing the glasses is usually accidental, so quitting outright would be
    wrong, but sitting parked forever is no better. So it asks, on the
    laptop screen; the glasses coming back answers "no" by itself.

    zenity is spawned, not awaited -- the render loop cannot block on it.
    poll_device() polls the handle and the shutdown path closes it.
    """
    if app._unplug_dialog is not None or not shutil.which("zenity"):
        return
    lead = ("The glasses display dropped out" if reason == "display-lost"
            else "The glasses were unplugged")
    try:
        app._unplug_dialog = subprocess.Popen(
            ["zenity", "--question", "--title=Refract",
             "--text=%s and the desktop has been handed back to the "
             "laptop.\n\nQuit Refract?" % lead,
             "--ok-label=Quit", "--cancel-label=Keep it running"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, preexec_fn=_die_with_parent)
    except OSError:
        app._unplug_dialog = None


def _dismiss_unplug_dialog(app):
    """Make the "quit?" dialog go away -- the glasses came back, so the
    question answers itself."""
    dlg = app._unplug_dialog
    if dlg is None:
        return
    app._unplug_dialog = None
    if dlg.poll() is None:
        try:
            dlg.terminate()
        except OSError:
            pass


def _usb_present():
    from refract.core import hardware
    return hardware.find_pid() is not None


def _probe(app, present_fn=_usb_present):
    """(usb_present, output_ok), or None if the USB check itself failed.

    The output is only checked while the USB device is present (otherwise
    the answer is moot).
    """
    try:
        usb = bool(present_fn())
    except Exception:                                     # noqa: BLE001
        return None
    return usb, (True if not usb else _output_healthy(app))


class _Prober:
    """Runs _probe every DEVICE_POLL on its own thread.

    The output check is a D-Bus round trip -- too slow for the render
    thread, where it would drop a frame each time.

    Each result records whether the app was parked when the probe STARTED;
    a probe that straddled a park or resume (which change the display mode
    on purpose) is dropped by take() rather than read as "output lost".
    """

    def __init__(self, app):
        import threading
        self.app = app
        self._lock = threading.Lock()
        self._result = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="refract-device-probe")
        self._thread.start()

    def _run(self):
        while True:
            parked = self.app.parked
            probed = _probe(self.app)
            with self._lock:
                self._result = (parked, probed)
            time.sleep(DEVICE_POLL)

    def take(self):
        """The newest finished probe, once; None if there is none yet or it
        went stale across a park/resume."""
        with self._lock:
            result, self._result = self._result, None
        if result is None:
            return None
        parked, probed = result
        if parked != self.app.parked:
            return None
        return probed


def poll_device(app, now, present_fn=None):
    """Park when the glasses are unplugged, resume when they come back.

    Watches the cable (the USB device vanishes) and the output (the DP
    connector goes away, or comes back out of SBS -- see _output_healthy).
    Wear detection is not possible on this hardware: putting the glasses on
    or off produces no event at all.

    Only auto-resumes if WE parked for this reason -- a deliberate park
    should survive a replug.
    """
    # react to the "quit?" dialog as soon as it is answered
    if app._unplug_dialog is not None:
        rc = app._unplug_dialog.poll()
        if rc is not None:
            app._unplug_dialog = None
            if rc == 0:                    # "Quit" pressed
                print("  unplug dialog: quitting", flush=True)
                app.quit = True

    if present_fn is not None:
        # synchronous, on the caller's clock -- how the tests drive it
        if now - app._device_t < DEVICE_POLL:
            return None
        app._device_t = now
        probed = _probe(app, present_fn)
    else:
        prober = getattr(app, "_device_prober", None)
        if prober is None:
            prober = app._device_prober = _Prober(app)
        probed = prober.take()
    if probed is None:
        return None
    usb, out_ok = probed

    # each side gets its own CONFIRM_UNPLUG debounce
    if usb:
        app._device_pending_absent_since = None
    elif app._device_pending_absent_since is None:
        app._device_pending_absent_since = now
    if out_ok:
        app._output_bad_since = None
    elif app._output_bad_since is None:
        app._output_bad_since = now

    usb_gone = (not usb and now - app._device_pending_absent_since
                >= CONFIRM_UNPLUG)
    out_gone = (not out_ok and now - app._output_bad_since >= CONFIRM_UNPLUG)

    # a loss on either side that has NOT yet held for the debounce -> unsettled
    if (not usb and not usb_gone) or (not out_ok and not out_gone):
        return None

    present = not (usb_gone or out_gone)
    reason = "unplugged" if usb_gone else "display-lost"

    if present == app._device_present:
        return None
    app._device_present = present

    if not present:
        # a recovery must not undo a park the wearer made deliberately
        was_parked = app.parked
        print("  glasses %s -> parking"
              % ("unplugged" if reason == "unplugged" else "display lost"),
              flush=True)
        park(app, reason)
        app._parked_by_unplug = not was_parked
        if app._parked_by_unplug:
            _ask_quit_on_unplug(app, reason)
        return reason
    if app._parked_by_unplug:
        print("  glasses back -> resuming", flush=True)
        app._parked_by_unplug = False
        _dismiss_unplug_dialog(app)
        resume(app)
        return "replugged"
    return "present"


def leave_2d_on_exit(app):
    """At shutdown, put the glasses back to an ordinary 2D display, whoever
    switched them to side-by-side (global.keep_sbs opts out).

    Asks the display what mode it is ACTUALLY in: switching an already-2D
    panel waits out a re-enumeration timeout and makes quitting feel hung.
    """
    leave_2d = not app.config.setdefault("global", {}).get(
        "keep_sbs", False)
    if leave_2d and app.head:
        try:
            from refract.core import displaymode
            if displaymode.is_sbs(app.monitor):
                if not app.head.v:
                    # no IMU connection: set_sbs() would silently do
                    # nothing, so say the panel stays in SBS
                    print("  glasses     : IMU never initialized -- "
                          "cannot switch back to 2D; panel stays in "
                          "side-by-side until switched by hand",
                          flush=True)
                else:
                    rc = app.head.set_sbs(False)
                    if rc == 0:
                        print("  glasses     : back to 2D",
                              flush=True)
                    else:
                        print("  glasses     : switch-to-2D failed "
                              "(rc=%s) -- panel may still be in "
                              "side-by-side" % rc, flush=True)
            else:
                print("  glasses     : already 2D", flush=True)
        except Exception as e:                    # noqa: BLE001
            print("  glasses: could not restore 2D: %s" % e,
                  flush=True)

