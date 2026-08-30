"""Display Handoff -- getting out of the way, and coming back.

The single biggest complaint about XR desktop tools generally: taking the
glasses off, or wanting to glance at the laptop screen, means reconfiguring
displays by hand. So Refract treats it as a feature with its own controls
rather than as a side effect of other display code.

PARK gives the machine back: the capture sessions stop, the desktop's monitor
layout is restored, the glasses drop out of side-by-side to an ordinary 2D
display, and our window gets out of the way. RESUME puts it all back.

Deliberately built out of paths that already work -- a scene's own exit() and
enter() -- rather than a second, subtly different teardown. The quirks this
has to survive are all documented and all real: the glasses re-enumerate on a
dimension change, USB access is exclusive, and Mutter's display config is
applied with the TEMPORARY method so a crash cannot outlive the session.
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

    # SBS off LAST: the glasses re-enumerate on a dimension change, and doing
    # it while captures are still running invites the mirror-stream death
    # documented in the plan.
    if app.sbs_ours and app.head:
        try:
            app.head.set_sbs(False)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: sbs off failed: %s" % e, flush=True)

    # Deliberate handoff (reason=="") wants the window truly out of the way
    # -- the wearer is actively switching to laptop apps and will focus
    # whatever they click next themselves. Anything ACCIDENTAL (an unplug, a
    # lost output) is different: GLFW's focus_window() is a DOCUMENTED no-op
    # under Wayland ("Wayland has no concept of client-controlled focus"), so
    # once iconified there is no reliable programmatic way back -- confirmed
    # on a real unplug/replug, where the window stayed hidden behind the
    # desktop until the wearer manually clicked the taskbar icon. Skip the
    # iconify for those: leave the window mapped, so resume() has nothing to
    # reclaim and nothing Wayland can refuse.
    app._parked_iconified = reason == ""
    if app._parked_iconified:
        try:
            app.glfw.iconify_window(app.win)
        except Exception:                                 # noqa: BLE001
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
    if app._parked_iconified:
        try:
            app.glfw.restore_window(app.win)
            app.glfw.focus_window(app.win)
        except Exception:                                 # noqa: BLE001
            pass

    if app.sbs_ours and app.head:
        try:
            app.head.set_sbs(True)
            from refract.core import displaymode
            # wait for the mode to actually appear: the switch is asynchronous
            # and the readback right after it times out even on success
            displaymode.wait_for_mode(app.monitor, tries=16, delay=0.25)
        except Exception as e:                            # noqa: BLE001
            print("  handoff: sbs on failed: %s" % e, flush=True)

    # Whatever the compositor did with our window while the output was gone
    # (a lost-output park migrates it onto the laptop panel), put it back on
    # the glasses connector. A no-op on a clean replug where it never moved.
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


# How often to ask whether the glasses are still attached. Cheap (a sysfs
# read) but not free, and unplugging is not a thing that needs millisecond
# latency.
DEVICE_POLL = 2.0

# How long an ABSENT reading has to hold before it is trusted as a real
# unplug. Reseating a USB-C cable by hand is not one clean transition -- it
# reads absent/present several times over a couple of seconds before it is
# fully seated, and reacting to every blip tore Desk down and rebuilt it
# repeatedly (observed: three separate parks and a spawned "quit?" dialog
# from a single manual replug), sometimes corrupting the monitor layout on
# the way ("Logical monitors not adjacent"). A PRESENT reading is trusted
# immediately, with no debounce -- there is nothing to lose by resuming
# promptly, and a replug should feel instant, not delayed on principle.
CONFIRM_UNPLUG = 3.0


def _output_healthy(app):
    """Is the glasses VIDEO output still present and in the side-by-side mode
    the renderer is built around?

    Presence of the USB device is not enough. A brief DP dropout -- a
    cable/connector flex, which happens a lot while tapping the temple --
    makes the compositor migrate our fullscreen window onto the laptop panel
    and reshuffle the desktop around the output that vanished, all while the
    USB side stays put. Left alone, the shell keeps rendering a squeezed
    side-by-side image onto the laptop with no keyboard and no way back.

    Conservative on purpose: any error querying the compositor returns True
    (don't cry wolf and tear down a working session over a transient D-Bus
    or xrandr hiccup). Only a clear "the connector is gone" or "it came back
    but not in SBS" counts as unhealthy, and even that is debounced by
    CONFIRM_UNPLUG before it is acted on.
    """
    if app.windowed or not app.head:
        return True
    from refract.core import displaymode
    try:
        conn = displaymode.glasses_connector()
    except Exception:                                     # noqa: BLE001
        return True
    if not conn:
        return False
    if app.parked:
        # While parked we drop the panel to 2D on purpose, so "still in SBS"
        # is the wrong question -- recovery just means the connector is
        # enumerated again; resume() restores the mode itself.
        return True
    try:
        return displaymode.is_sbs(conn)
    except Exception:                                     # noqa: BLE001
        return True


def _ask_quit_on_unplug(app, reason="unplugged"):
    """Put up a non-blocking dialog asking whether to quit, after the glasses
    connection dropped and parked us.

    Losing the glasses is usually accidental -- a cable pulled loose or a
    connector flexed out for a moment, not a deliberate "I'm done" -- and
    used to just sit parked forever with no way back except the keyboard or a
    plug the wearer may not reach for a while. Quitting outright would be
    worse: the whole point of auto-park is that a momentary cable wiggle
    should not blow away the session. So instead this asks, on the laptop
    screen (the glasses are gone, so it cannot ask there), and the glasses
    coming back (see _dismiss_unplug_dialog) answers "no" for free.

    zenity is spawned, not awaited -- the render loop cannot block on it. Its
    Popen handle is polled a few times a second from poll_device and once
    more in the shutdown path, so a dialog never outlives the process it
    belongs to.
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
            stdin=subprocess.DEVNULL)
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


def poll_device(app, now, present_fn=None):
    """Park when the glasses are unplugged, resume when they come back.

    Wear detection turned out to be impossible on this hardware -- there is
    no `get_wear_status` in the Linux libglasses, and putting the glasses on
    and off produces no MCU events at all (verified with every event logged,
    three times over). So the two things we CAN detect automatically are the
    cable (the USB device vanishes) and the output (the DP connector goes
    away, or the compositor migrates our window off it after a momentary
    dropout -- see _output_healthy). Either leaves the shell rendering into a
    display the wearer is not looking through, with no keyboard to recover.

    Only auto-resumes if WE parked for this reason -- a deliberate park
    should survive a replug.
    """
    # Cheap regardless of the throttle below: react to the wearer's answer
    # in the "quit?" dialog as soon as it closes, not just on the next
    # presence-poll tick.
    if app._unplug_dialog is not None:
        rc = app._unplug_dialog.poll()
        if rc is not None:
            app._unplug_dialog = None
            if rc == 0:                    # "Quit" pressed
                print("  unplug dialog: quitting", flush=True)
                app.quit = True

    if now - app._device_t < DEVICE_POLL:
        return None
    app._device_t = now
    if present_fn is None:
        from refract.core import hardware
        def present_fn():                                 # noqa: E306
            return hardware.find_pid() is not None
    try:
        usb = bool(present_fn())
    except Exception:                                     # noqa: BLE001
        return None

    # The glasses count as "here" only when the USB device is present AND --
    # for a real session that owns a glasses output -- that output is still
    # usable. The output check is skipped only when the USB side is already
    # gone (moot). _output_healthy handles the parked case itself (connector
    # merely needs to be enumerated -- we dropped SBS on purpose) and returns
    # True for a headless / windowed app, so this is inert unless there is
    # really a glasses output to lose.
    out_ok = True if not usb else _output_healthy(app)

    # Each side gets its own CONFIRM_UNPLUG debounce -- a reseat and a mode
    # re-enumeration both flap absent/present for a second or two, and only
    # a sustained loss should tear the session down.
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
        # Only claim the loss caused it if we were actually running. If the
        # wearer had already parked deliberately, a cable/output blip is
        # incidental and a later recovery must NOT undo their choice.
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
