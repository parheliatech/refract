"""GTK 4 window prototype for the GLFW -> GTK port (DEVELOPMENT_PLAN.md):
can a Gtk.GLArea host moderngl fullscreen on the glasses, and STAY there
while Desk-style virtual monitors come and go?

Timeline (seconds after the window is shown):
   5  create two virtual monitors (ScreenCapture, as Desk does)
  12  remove them
  18  quit
Run from the repo root:  .venv/bin/python tools/gtk-window-proto.py
Switches the glasses to side-by-side and back. Exits hard (vendor SDK
teardown hangs).
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, ".")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk          # noqa: E402

from refract.core import displaymode                # noqa: E402
from refract.core.head import Head                  # noqa: E402

LOG_T0 = time.time()


def log(msg):
    print("  [%5.1fs] %s" % (time.time() - LOG_T0, msg), flush=True)


class EglLoader:
    """moderngl loader via the RUNTIME libEGL.so.1 (GTK on Wayland is EGL)."""

    def __init__(self):
        egl = ctypes.CDLL("libEGL.so.1")
        self._gpa = egl.eglGetProcAddress
        self._gpa.restype = ctypes.c_void_p
        self._gpa.argtypes = [ctypes.c_char_p]

    def load_opengl_function(self, name):
        return self._gpa(name.encode()) or 0

    def __enter__(self):
        pass

    def __exit__(self, *a):
        pass

    def release(self):
        pass


def main():
    # 1. glasses into side-by-side, as Refract does at boot
    head = Head()
    head.start(rate_hz=240)
    if head.error:
        log("IMU error: %s" % head.error)
        os._exit(1)
    conn = displaymode.glasses_connector()
    rc = head.set_sbs(True)
    ok = displaymode.wait_for_mode(conn)
    log("glasses %s: set_sbs rc=%s, side-by-side %s" % (conn, rc, "on" if ok else "FAILED"))

    Gtk.init()
    disp = Gdk.Display.get_default()
    ms = disp.get_monitors()
    mon = next(ms.get_item(i) for i in range(ms.get_n_items())
               if ms.get_item(i).get_connector() == conn)
    geo = mon.get_geometry()
    log("GDK monitor %s: logical %dx%d scale %s" % (conn, geo.width, geo.height, mon.get_scale()))

    state = {"ctx": None, "frames": 0, "fbsize": None, "t_first": None,
             "quit": False, "cap": None}

    area = Gtk.GLArea()
    area.set_allowed_apis(Gdk.GLAPI.GL)
    area.set_required_version(3, 3)
    area.set_has_depth_buffer(True)

    def on_realize(a):
        a.make_current()
        if a.get_error():
            log("GLArea error: %s" % a.get_error())
            return
        import moderngl
        moderngl.init_context(EglLoader())
        state["ctx"] = moderngl.get_context()
        log("GL %s via %s" % (state["ctx"].version_code,
                              state["ctx"].info["GL_RENDERER"]))

    def on_render(a, glctx):
        ctx = state["ctx"]
        if ctx is None:
            return False
        fbo = ctx.detect_framebuffer()
        fbo.use()
        w, h = fbo.size
        state["fbsize"] = (w, h)
        t = time.time()
        # left eye reddish, right eye bluish, pulsing -- visibly alive
        p = 0.5 + 0.5 * __import__("math").sin(t * 3.0)
        for eye, rgb in ((0, (0.6 * p, 0.05, 0.05)), (1, (0.05, 0.05, 0.6 * p))):
            ctx.viewport = (eye * w // 2, 0, w // 2, h)
            ctx.scissor = (eye * w // 2, 0, w // 2, h)
            ctx.clear(*rgb, 1.0)
        ctx.scissor = None
        state["frames"] += 1
        if state["t_first"] is None:
            state["t_first"] = t
        return True

    area.connect("realize", on_realize)
    area.connect("render", on_render)

    def tick(widget, clock):
        widget.queue_render()
        return True
    area.add_tick_callback(tick)

    win = Gtk.Window(title="refract-gtk-proto")
    win.set_child(area)
    win.fullscreen_on_monitor(mon)
    win.present()

    # Re-place on the glasses whenever the monitor set changes, once the
    # new layout has had time to settle.
    pending = {"id": None}

    def replace():
        pending["id"] = None
        ms2 = disp.get_monitors()
        m2 = next((ms2.get_item(i) for i in range(ms2.get_n_items())
                   if ms2.get_item(i).get_connector() == conn), None)
        if m2 is None:
            log("re-place: %s not present" % conn)
            return False
        win.fullscreen_on_monitor(m2)
        log("re-place: fullscreen_on_monitor(%s) re-issued (fb was %s)" % (conn, state["fbsize"]))
        return False

    def on_monitors_changed(model, pos, removed, added):
        log("monitors changed (-%d +%d)" % (removed, added))
        if pending["id"]:
            GLib.source_remove(pending["id"])
        pending["id"] = GLib.timeout_add(700, replace)
    ms.connect("items-changed", on_monitors_changed)
    t_shown = time.time()
    log("window presented, fullscreen_on_monitor(%s)" % conn)

    def fps_report():
        if state["t_first"]:
            el = time.time() - state["t_first"]
            log("frames %d, ~%.0f fps, framebuffer %s%s" % (state["frames"], state["frames"] / el, state["fbsize"], "  <- ON GLASSES" if state["fbsize"] == (3840, 1080) else "  <- NOT on glasses"))
        return not state["quit"]
    GLib.timeout_add(1500, fps_report)

    def add_virtuals():
        from refract.core.vdisplay import ScreenCapture
        cap = ScreenCapture([("virtual", (1920, 1080)), ("virtual", (1920, 1080))], capture=True)
        cap.start()
        state["cap"] = cap
        log("virtual monitors requested")
        return False

    def drop_virtuals():
        if state["cap"]:
            state["cap"].stop()
            state["cap"] = None
            log("virtual monitors removed")
        return False

    def finish():
        state["quit"] = True
        return False

    GLib.timeout_add(5000, add_virtuals)
    GLib.timeout_add(12000, drop_virtuals)
    GLib.timeout_add(18000, finish)

    ctx = GLib.MainContext.default()
    while not state["quit"]:
        ctx.iteration(True)

    fps_report()
    win.destroy()
    while ctx.pending():
        ctx.iteration(False)
    rc = head.set_sbs(False)
    log("glasses back to 2D: rc=%s" % rc)
    time.sleep(2.0)
    head.stop()
    sys.stdout.flush()
    os._exit(0)


main()
