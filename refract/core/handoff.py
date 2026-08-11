"""Display Handoff -- getting out of the way, and coming back.

The single biggest complaint about Breezy Desktop: taking the glasses off, or
wanting to glance at the laptop screen, meant reconfiguring displays by hand.
So Refract treats it as a feature with its own controls rather than as a side
effect of other display code.

PARK gives the machine back: the capture sessions stop, the desktop's monitor
layout is restored, the glasses drop out of side-by-side to an ordinary 2D
display, and our window gets out of the way. RESUME puts it all back.

Deliberately built out of paths that already work -- a scene's own exit() and
enter() -- rather than a second, subtly different teardown. The quirks this
has to survive are all documented and all real: the glasses re-enumerate on a
dimension change, USB access is exclusive, and Mutter's display config is
applied with the TEMPORARY method so a crash cannot outlive the session.
"""

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

    try:
        app.glfw.iconify_window(app.win)
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
    try:
        app.glfw.restore_window(app.win)
        app.glfw.focus_window(app.win)
    except Exception:                                     # noqa: BLE001
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


def poll_device(app, now, present_fn=None):
    """Park when the glasses are unplugged, resume when they come back.

    Wear detection turned out to be impossible on this hardware -- there is
    no `get_wear_status` in the Linux libglasses, and putting the glasses on
    and off produces no MCU events at all (verified with every event logged,
    three times over). So the one thing we CAN detect automatically is the
    cable: unplugging is itself a handoff, and without this the shell would
    sit rendering into a display that is not there.

    Only auto-resumes if WE parked for this reason -- a deliberate park
    should survive a replug.
    """
    if now - app._device_t < DEVICE_POLL:
        return None
    app._device_t = now
    if present_fn is None:
        from refract.core import hardware
        def present_fn():                                 # noqa: E306
            return hardware.find_pid() is not None
    try:
        present = bool(present_fn())
    except Exception:                                     # noqa: BLE001
        return None
    if present == app._device_present:
        return None
    app._device_present = present

    if not present:
        # Only claim the unplug caused it if we were actually running. If the
        # wearer had already parked deliberately, the cable is incidental and
        # a later replug must NOT undo their choice.
        was_parked = app.parked
        print("  glasses unplugged -> parking", flush=True)
        park(app, "unplugged")
        app._parked_by_unplug = not was_parked
        return "unplugged"
    if app._parked_by_unplug:
        print("  glasses back -> resuming", flush=True)
        app._parked_by_unplug = False
        resume(app)
        return "replugged"
    return "present"
