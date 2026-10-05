"""Stereo renderer, scene stack, and the minimal glass-panel UI toolkit.

The frame is drawn per eye as: active scene -> status overlay -> HUD.
Scenes are in-process modules on a stack (architecture decision A1: one
process owns the glasses; switching modes is a scene swap, no device handoff,
no GL context churn).
"""

import math
import os
import signal
import time

import numpy as np

SCREEN_VERT = """
#version 330 core
in vec3 aPos;
in vec2 aUV;
uniform mat4 uMVP;
out vec2 vUV;
void main() {
    vUV = aUV;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

SCREEN_FRAG = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D sScreen;
uniform float uDim;
uniform float uUseAlpha;
void main() {
    vec4 t = texture(sScreen, vUV);
    // Desktop capture arrives as BGRx -- its alpha channel is meaningless
    // (often 0), so opaque screens must FORCE alpha to 1. Only the UI panels,
    // which are drawn with real alpha, ask for it to be honoured.
    fragColor = vec4(t.rgb * uDim, mix(1.0, t.a, uUseAlpha));
}
"""

OVERLAY_VERT = """
#version 330 core
in vec2 aPos;
out vec2 vUV;
uniform vec4 uRect;
void main() {
    vec2 t = aPos * 0.5 + 0.5;
    vUV = vec2(t.x, 1.0 - t.y);
    gl_Position = vec4(mix(uRect.xy, uRect.zw, t), 0.0, 1.0);
}
"""

OVERLAY_FRAG = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D sHud;
void main() { fragColor = texture(sHud, vUV); }
"""

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Must match the .desktop file basename (refract.desktop) for the shell to
# associate our window with it -- see the app-id hints in App.__init__.
APP_ID = "refract"

from refract.core.config import runtime_dir

RUNTIME_DIR = runtime_dir()
PID_PATH = os.path.join(RUNTIME_DIR, "refract.pid")

# seconds a settings change waits before it is written to disk
CONFIG_AUTOSAVE = 1.0


def already_running():
    """PID of another live Refract, or None.

    Two instances fight over the glasses (USB is exclusive), each create
    their own virtual monitors, and both rearrange the desktop layout.
    """
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().decode(errors="replace").split("\0")
    except OSError:
        return None                     # stale pid file, nobody home
    return pid if is_refract_cmdline(argv) else None


def is_refract_cmdline(argv):
    """Is this argv a running Refract shell (python -m refract ...)? Not
    merely "mentions refract" -- that would match an editor with a Refract
    file open, or refract.ctl."""
    for i, arg in enumerate(argv):
        if arg == "-m" and i + 1 < len(argv) and argv[i + 1] == "refract":
            return True
        if arg.endswith(os.path.join("refract", "__main__.py")):
            return True
    return False


class _GlfwLoader:
    """GL function loader for moderngl that asks GLFW, which already knows
    which GL library its context came from.

    moderngl's default loader dlopens the UNVERSIONED libEGL.so / libGL.so,
    which only exist when the -dev packages are installed.
    """

    def __init__(self, glfw):
        self._glfw = glfw

    def load_opengl_function(self, name):
        return self._glfw.get_proc_address(name) or 0

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass

    def release(self):
        pass


def gl_context(glfw):
    """moderngl context for the GLFW window that is current."""
    import moderngl
    # init_context + get_context, NOT create_context: create_context ignores
    # the default context and falls back to glcontext's library detection.
    moderngl.init_context(_GlfwLoader(glfw))
    ctx = moderngl.get_context()
    if ctx.version_code < 330:
        raise RuntimeError("OpenGL 3.3 needed, got %d" % ctx.version_code)
    return ctx


class _EglLoader:
    """GL function loader through the RUNTIME libEGL.so.1 -- GTK on Wayland
    renders with EGL. (The unversioned libEGL.so moderngl would dlopen only
    exists when the -dev packages are installed.) Falls back to GLX for an
    X11 session."""

    def __init__(self):
        import ctypes
        try:
            lib = ctypes.CDLL("libEGL.so.1")
            fn = lib.eglGetProcAddress
        except (OSError, AttributeError):
            lib = ctypes.CDLL("libGL.so.1")
            fn = lib.glXGetProcAddress
        fn.restype = ctypes.c_void_p
        fn.argtypes = [ctypes.c_char_p]
        self._gpa = fn

    def load_opengl_function(self, name):
        return self._gpa(name.encode()) or 0

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass

    def release(self):
        pass


def gl_context_current():
    """moderngl context for whatever GL context is current (the GtkGLArea's,
    made current by the caller)."""
    import moderngl
    moderngl.init_context(_EglLoader())
    ctx = moderngl.get_context()
    if ctx.version_code < 330:
        raise RuntimeError("OpenGL 3.3 needed, got %d" % ctx.version_code)
    return ctx


def perspective(fovy_deg, aspect, near=0.05, far=100.0):
    f = 1.0 / math.tan(math.radians(fovy_deg) * 0.5)
    m = np.zeros((4, 4), dtype="f4")
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def rot_y(rad):
    m = np.eye(4, dtype="f4")
    m[0, 0] = math.cos(rad)
    m[0, 2] = math.sin(rad)
    m[2, 0] = -math.sin(rad)
    m[2, 2] = math.cos(rad)
    return m


def screen_mesh(centre_yaw, width, height, distance, curve, segments=24):
    """World-space vertices for one screen, laid on a cylinder of radius
    `distance`. curve=0 is flat (a tangent plane); curve=1 follows the cylinder
    exactly, so every part of the screen stays the same distance from the eye.
    """
    half_ang = math.atan2(width * 0.5, distance)
    verts = []
    for j in range(segments + 1):
        t = j / segments
        a = centre_yaw + (t - 0.5) * 2.0 * half_ang
        # curved: ride the arc. flat: project the arc onto the tangent plane.
        if curve > 0.0:
            cx = math.sin(a) * distance
            cz = -math.cos(a) * distance
            fx = math.sin(centre_yaw) * distance + \
                math.cos(centre_yaw) * (t - 0.5) * width
            fz = -math.cos(centre_yaw) * distance + \
                math.sin(centre_yaw) * (t - 0.5) * width
            x = fx + (cx - fx) * curve
            z = fz + (cz - fz) * curve
        else:
            x = math.sin(centre_yaw) * distance + \
                math.cos(centre_yaw) * (t - 0.5) * width
            z = -math.cos(centre_yaw) * distance + \
                math.sin(centre_yaw) * (t - 0.5) * width
        # v=0 at the TOP: captured frames arrive top-row-first.
        verts.append((x, height * 0.5, z, t, 0.0))
        verts.append((x, -height * 0.5, z, t, 1.0))
    return np.array(verts, dtype="f4").reshape(-1)


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def text_panel(lines, w=1024, h=96, font_size=28, bg=(0, 0, 0, 140)):
    """Legible text on a translucent strip -- the workhorse for anything the
    wearer must read. Feedback belongs where the eyes are, never only on a
    terminal the wearer cannot see."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), bg)
    d = ImageDraw.Draw(img)
    f = _font(font_size)
    for i, ln in enumerate(lines[:2]):
        d.text((w // 2, 26 + i * 40), ln, font=f, anchor="mm",
               fill=(255, 255, 255, 255))
    return np.asarray(img, dtype=np.uint8)


# Parhelia palette (from the AeroScan theme, the same design system):
# near-black ground, charcoal-blue base, and the three signal hues.
PARHELIA_GROUND = (17, 17, 17)
PARHELIA_BASE = (54, 69, 79)
PARHELIA_TEAL = (10, 121, 133)
PARHELIA_AMBER = (255, 172, 17)
PARHELIA_MAGENTA = (219, 22, 117)
PARHELIA_RADAR = (57, 255, 20)

ACCENT_DEFAULT = (120, 200, 235)


def _dimmed(rgb):
    """Unavailable chrome: keep the hue's luminance, drop its identity."""
    g = sum(rgb) // 3
    return (int(g * 0.35) + 55, int(g * 0.35) + 60, int(g * 0.35) + 70)


def panel_image(title, lines=(), w=640, h=360, focused=False,
                accent=ACCENT_DEFAULT, available=True):
    """A glass panel: translucent body, light-catching edge, accent chip.

    THE single source of panel/tile chrome -- style here, never per-scene, so
    the Parhelia + refraction treatment stays consistent across the shell,
    the HUD and every sub-experience.
    """
    from PIL import Image, ImageDraw
    a = tuple(int(c) for c in accent)
    if not available:
        a = _dimmed(a)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = max(8, int(min(w, h) * 0.07))
    body = (16, 20, 28, 215) if focused else (11, 13, 18, 170)
    edge = a + (245,) if focused else a + (105,)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=rad, fill=body,
                        outline=edge, width=max(3, int(h * 0.011))
                        if focused else 2)
    # light-catching top-left corner + edge streak: the refraction motif,
    # used as an accent rather than allowed to dominate
    d.arc([3, 3, 3 + rad * 2, 3 + rad * 2], 180, 270,
          fill=(235, 248, 255, 215), width=2)
    d.line([rad, 3, int(w * 0.55), 3],
           fill=(235, 248, 255, 190 if focused else 110), width=2)
    d.text((w // 2, h * 0.34), title, font=_font(int(h * 0.135)), anchor="mm",
           fill=(255, 255, 255, 255) if available else (185, 192, 205, 255))
    # accent chip: the per-sub-experience identity at a glance
    cw, ch = int(w * 0.20), max(3, int(h * 0.020))
    cx, cy = w // 2, int(h * 0.50)
    d.rounded_rectangle([cx - cw // 2, cy, cx + cw // 2, cy + ch],
                        radius=ch // 2, fill=a + (255 if focused else 190,))
    for i, ln in enumerate(lines[:3]):
        d.text((w // 2, h * 0.63 + i * h * 0.115), ln,
               font=_font(int(h * 0.072)), anchor="mm",
               fill=(205, 214, 228, 255) if available
               else (140, 147, 160, 255))
    return np.asarray(img, dtype=np.uint8)


def wordmark_image(w=768, h=176, text="REFRACT", sub=None):
    """The Refract wordmark, with a chromatic-aberration split -- light bent
    through the lens, which is the whole naming conceit. Shared with the
    HUD."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font(int(h * 0.46))
    cx, cy = w // 2, int(h * 0.42)
    off = max(2, int(h * 0.022))
    # split the fringes the way a prism would, then lay white over the top
    d.text((cx - off, cy), text, font=f, anchor="mm", fill=(0, 200, 255, 150))
    d.text((cx + off, cy), text, font=f, anchor="mm", fill=(255, 40, 160, 150))
    d.text((cx, cy), text, font=f, anchor="mm", fill=(255, 255, 255, 255))
    if sub:
        d.text((cx, int(h * 0.80)), sub, font=_font(int(h * 0.15)),
               anchor="mm", fill=(150, 162, 180, 255))
    return np.asarray(img, dtype=np.uint8)


def dot_image(size=48, rgb=(255, 255, 255)):
    """Pointer dot: bright core, dark ring so it stays visible on any
    background (a plain white dot vanishes over a white window)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size * 0.16
    d.ellipse([m, m, size - m, size - m], fill=rgb + (245,),
              outline=(0, 0, 0, 200), width=max(2, size // 16))
    return np.asarray(img, dtype=np.uint8)


class WorldScreen:
    """A textured screen floating in world space (curved or flat): desktop
    monitors, launcher tiles, the test card."""

    def __init__(self, app, size_px, width_m=1.15, distance=1.2,
                 yaw=0.0, curve=1.0, alpha=False, mipmaps=False,
                 bgra=False):
        self.app = app
        self.size_px = size_px
        self.mipmaps = mipmaps
        self.bgra = bgra
        self.tex = None
        self._make_texture()
        self.alpha = alpha
        self.vbo = None
        self.vao = None
        self._geom = None
        self.set_geometry(width_m=width_m, distance=distance, yaw=yaw,
                          curve=curve)

    def resize(self, size_px):
        """Adopt a new source resolution. A mirrored physical output does not
        announce its size until frames flow, and the aspect feeds the mesh --
        so the geometry is re-applied from the stored parameters."""
        size_px = (int(size_px[0]), int(size_px[1]))
        if size_px == tuple(self.size_px):
            return False
        self.tex.release()
        self.size_px = size_px
        self._make_texture()
        if self._geom:
            self.set_geometry(**self._geom)
        return True

    def _make_texture(self):
        mgl = self.app.moderngl
        self.tex = self.app.ctx.texture(tuple(self.size_px), 4)
        # Mipmaps for screens that get minified (Desk's side screens, seen at
        # a steep angle) -- without them the text shimmers as the head moves.
        mip = mgl.LINEAR_MIPMAP_LINEAR if self.mipmaps else mgl.LINEAR
        self.tex.filter = (mip, mgl.LINEAR)
        self.tex.repeat_x = self.tex.repeat_y = False
        try:
            self.tex.anisotropy = 16.0
        except Exception:                                 # noqa: BLE001
            pass
        if self.bgra:
            # screen capture is uploaded as BGRx; swap red and blue when
            # sampling, which is free, instead of on the CPU
            self.tex.swizzle = "BGRA"
        if self.mipmaps:
            self.tex.build_mipmaps()   # complete chain before the first draw

    def uploaded(self):
        """Call after new pixels land in the texture by any route (write()
        or the C fast blit), so the smaller levels match the new frame."""
        if self.mipmaps:
            self.tex.build_mipmaps()

    def set_geometry(self, width_m, distance, yaw=0.0, curve=1.0):
        self._geom = {"width_m": width_m, "distance": distance, "yaw": yaw,
                      "curve": curve}
        aspect = self.size_px[0] / float(self.size_px[1])
        data = screen_mesh(yaw, width_m, width_m / aspect, distance, curve)
        if self.vbo is None:
            self.vbo = self.app.ctx.buffer(data.tobytes())
            self.vao = self.app.ctx.vertex_array(
                self.app.screen_prog, [(self.vbo, "3f 2f", "aPos", "aUV")])
        else:
            self.vbo.orphan(data.nbytes)
            self.vbo.write(data.tobytes())

    def write(self, rgba):
        self.tex.write(rgba)
        self.uploaded()

    def render(self, mvp, dim=1.0):
        mgl = self.app.moderngl
        ctx = self.app.ctx
        self.app.screen_prog["uMVP"].write(np.ascontiguousarray(mvp.T))
        self.app.screen_prog["uDim"].value = dim
        self.app.screen_prog["uUseAlpha"].value = 1.0 if self.alpha else 0.0
        self.tex.use(0)
        if self.alpha:
            ctx.enable(mgl.BLEND)
            # a panel's transparent corners must not write depth, or they
            # punch holes in whatever is drawn behind them afterwards
            ctx.depth_mask = False
        self.vao.render(mgl.TRIANGLE_STRIP)
        if self.alpha:
            ctx.depth_mask = True
            ctx.disable(mgl.BLEND)


class Overlay:
    """A screen-space texture strip (NDC rect), drawn per eye over the
    scene: the status line and the HUD."""

    def __init__(self, app, size=(1024, 96)):
        self.app = app
        self.size = size
        self.tex = app.ctx.texture(size, 4)
        self.tex.filter = (app.moderngl.LINEAR, app.moderngl.LINEAR)
        self._key = None
        self.visible = False
        self.expires = None

    def set_lines(self, lines, ttl=None):
        """Show text. With ttl, it hides itself again after that many
        seconds -- the default for anything a scene wants to say. Persistent
        text floats in the middle of the view forever; feedback should
        appear when something changes and then get out of the way.
        """
        key = tuple(lines)
        if self._key != key:
            self.tex.write(text_panel(lines, *self.size))
            self._key = key
        self.visible = True
        self.expires = (time.time() + ttl) if ttl else None

    def set_image(self, rgba, key=None):
        if key is None or self._key != key:
            self.tex.write(rgba)
            self._key = key
        self.visible = True

    def hide(self):
        self.visible = False

    def draw(self, rect=(-0.5, -0.92, 0.5, -0.72)):
        if self.expires is not None and time.time() >= self.expires:
            self.visible = False
            self.expires = None
        if not self.visible:
            return
        mgl = self.app.moderngl
        ctx = self.app.ctx
        self.tex.use(5)
        ctx.disable(mgl.DEPTH_TEST)
        ctx.enable(mgl.BLEND)
        self.app.overlay_prog["uRect"].value = rect
        self.app.overlay_vao.render(mgl.TRIANGLE_STRIP)
        ctx.disable(mgl.BLEND)
        ctx.enable(mgl.DEPTH_TEST)


class Scene:
    """A sub-experience. In-process by design (A1); misbehaving scenes can
    take the shell down -- accepted until proven to matter."""

    name = "scene"
    title = "Scene"

    def enter(self, app):
        pass

    def exit(self, app):
        pass

    def update(self, app, dt):
        pass

    def render_eye(self, app, eye):
        """Draw this scene for one eye. Viewport is already set; use
        app.mvp(eye, ...) for the camera."""

    def on_key(self, app, key, scancode, action, mods):
        """Return True if consumed; unconsumed keys fall through to the app
        (Esc pops the scene / quits)."""
        return False

    def on_command(self, app, cmd):
        """A single word from refract.ctl. Return True if handled."""
        return False

    def park(self, app):
        """Release everything the desktop needs back (captures, monitors,
        display config). Default: a full exit, which is already correct for
        any scene whose enter() rebuilds from config."""
        self.exit(app)

    def unpark(self, app):
        self.enter(app)

    def on_cursor(self, app, x, y):
        """Pointer moved. (x, y) are NDC in [-1, 1], y up."""
        return False

    def on_mouse(self, app, button, action, mods):
        """Pointer button. app.cursor_ndc holds the current position."""
        return False

    def settings_schema(self):
        """[Setting, ...] rendered into this scene's HUD panel."""
        return []


class App:
    """The persistent Refract process: window, GL context, IMU, scene stack.

    options: fov, ipd, monitor, windowed, size_win, platform, recenter_after.
    """

    def __init__(self, head=None, sim_rot=None, fov=46.0, ipd=0.063,
                 monitor="DP-2", windowed=False, size_win=(1920, 540),
                 platform="any", recenter_after=4.0, title="Refract",
                 config=None, log_axis=False, log_tap=False):
        from refract.core import config as config_mod
        self.config = config_mod.load() if config is None else config
        self.config_dirty = False
        self._config_dirty_since = None   # see the autosave in run()
        self._config_mod = config_mod
        self.head = head
        self.sim_rot = sim_rot
        self.fov = fov
        self.ipd = ipd
        self.monitor = monitor
        self.windowed = windowed          # no glasses output to watch/hold
        self.recenter_after = recenter_after
        self.log_axis = log_axis
        self._axis_t = 0.0
        self.log_tap = log_tap
        self._input = None                # lazy pointer-input session
        self.sbs_ours = False             # did WE switch the glasses to SBS?
        self.sbs_ok = False               # is SBS confirmed working right now?
        self.parked = False               # display handed back to the laptop
        self._device_t = 0.0              # glasses-presence poll
        self._device_present = True
        self._device_pending_absent_since = None  # debounce, see handoff.py
        self._output_bad_since = None      # debounce for a lost glasses OUTPUT
        self._parked_by_unplug = False
        self._unplug_dialog = None        # zenity asking "quit?", or None
        self._frame_rot = None            # per-frame pose, see head_rot()
        self._reassert_at = None          # see reassert_output_soon()
        self.scenes = []
        self.quit = False
        self.t0 = None
        self.frames = 0

        # -- window: GTK 4 --------------------------------------------------
        # Not GLFW: Mutter moves a fullscreen window onto the laptop panel
        # whenever the monitor set changes (Desk adds two virtual monitors),
        # and only GTK can reliably put it back (see reassert_output()).
        if platform in ("x11", "wayland"):
            os.environ["GDK_BACKEND"] = platform
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GLib, Gtk
        # the Wayland app id must equal the .desktop file's basename, or
        # GNOME shows a generic icon and calls the window "unknown"
        GLib.set_prgname(APP_ID)
        if not Gtk.init_check():
            raise RuntimeError("GTK could not open a display")
        from refract.core import keys
        self.Gtk, self.Gdk, self.GLib = Gtk, Gdk, GLib
        self.keys = keys
        self.display = Gdk.Display.get_default()
        self._main = GLib.MainContext.default()
        self._render_job = None
        self._rendered = False
        self._fbo = None
        self.fb_w = self.fb_h = 0

        import moderngl
        self.moderngl = moderngl
        self.ctx = None
        self._gl_error = None
        self.area = Gtk.GLArea()
        self.area.set_allowed_apis(Gdk.GLAPI.GL)
        self.area.set_required_version(3, 3)
        self.area.set_has_depth_buffer(True)
        self.area.set_auto_render(False)
        self.area.set_focusable(True)
        self.area.connect("realize", self._on_realize)
        self.area.connect("render", self._on_render)

        self.win = Gtk.Window(title=title)
        self.win.set_child(self.area)
        self.win.connect("close-request", self._on_close_request)
        keyc = Gtk.EventControllerKey()
        keyc.connect("key-pressed", self._gtk_key)
        self.win.add_controller(keyc)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._gtk_motion)
        self.area.add_controller(motion)
        click = Gtk.GestureClick()
        click.set_button(0)                       # every button
        click.connect("pressed", self._gtk_click, keys.PRESS)
        click.connect("released", self._gtk_click, keys.RELEASE)
        self.area.add_controller(click)

        if windowed:
            self.win.set_default_size(*size_win)
        else:
            mon = self._gdk_monitor(monitor)
            if mon is not None:
                self.win.fullscreen_on_monitor(mon)
            else:
                self.win.fullscreen()
        self.display.get_monitors().connect("items-changed",
                                            self._on_monitors_changed)
        self.win.present()

        # Wait for the GL context, then one real frame for its size.
        deadline = time.time() + 10.0
        while self.ctx is None and self._gl_error is None \
                and time.time() < deadline:
            self._main.iteration(True)
        if self.ctx is None:
            raise RuntimeError("no OpenGL context: %s"
                               % (self._gl_error or "timed out"))
        if not self.render_frame(timeout=5.0) or not self.fb_w:
            raise RuntimeError("the window never drew a frame")
        self._gl()

        self.ctx.enable(moderngl.DEPTH_TEST)
        self.screen_prog = self.ctx.program(vertex_shader=SCREEN_VERT,
                                            fragment_shader=SCREEN_FRAG)
        self.screen_prog["sScreen"].value = 0
        self.screen_prog["uDim"].value = 1.0
        self.screen_prog["uUseAlpha"].value = 0.0

        self.overlay_prog = self.ctx.program(vertex_shader=OVERLAY_VERT,
                                             fragment_shader=OVERLAY_FRAG)
        quad = self.ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1],
                                        dtype="f4").tobytes())
        self.overlay_vao = self.ctx.simple_vertex_array(self.overlay_prog,
                                                        quad, "aPos")
        self.overlay_prog["sHud"].value = 5
        self.status = Overlay(self)

        self.proj = perspective(self.fov, self.eye_w / float(self.eye_h))

        # imported here, not at module scope: the HUD lives in refract.shell,
        # which imports this module
        from refract.shell.hud import Hud
        self.hud = Hud(self)

        # Temple taps and the triple nod: detectors fed from the IMU thread,
        # acted on once per frame in run(). See refract.core.headinput.
        from refract.core.headinput import HeadInput
        self.input = HeadInput(self.config, log_tap=log_tap)
        if self.head:
            self.head.on_sample = self.input.on_sample

        self.cursor_ndc = (0.0, 0.0)

        # Recenter over a signal as well as a key: a window launched in the
        # background never gets keyboard input.   pkill -USR1 -f refract
        # SIGTERM/SIGHUP ask the loop to stop rather than killing outright,
        # so the cleanup in run() still restores the panel, the monitor
        # layout and 2D mode.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: setattr(self, "quit", True))
        signal.signal(signal.SIGUSR1, lambda *_: self.recenter())
        signal.signal(signal.SIGUSR2, lambda *_: self.command("follow"))
        try:
            # O_NOFOLLOW so a symlink planted in our place is an error
            # rather than a redirect, and 0600 because nobody else needs it
            fd = os.open(PID_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        from refract.core.control import ControlSocket
        self._ctl = ControlSocket()

    # -- GTK window, GL context, input ----------------------------------------

    def _gdk_monitor(self, connector):
        ms = self.display.get_monitors()
        for i in range(ms.get_n_items()):
            m = ms.get_item(i)
            if m.get_connector() == connector:
                return m
        return None

    def _on_realize(self, area):
        area.make_current()
        err = area.get_error()
        if err is not None:
            self._gl_error = err.message
            return
        try:
            self.ctx = gl_context_current()
        except Exception as e:                            # noqa: BLE001
            self._gl_error = str(e)

    def _gl(self):
        """Make our GL context current. GTK renders with its own context,
        so do this before ANY moderngl call made outside the render signal
        (input handlers, control commands, scene updates)."""
        if self.ctx is not None:
            self.area.make_current()

    def _on_render(self, area, glcontext):
        """GtkGLArea's render signal: the only place our frame may be drawn
        -- the area's framebuffer is only bound for us here."""
        if self.ctx is None:
            return False
        self._fbo = self.ctx.detect_framebuffer()
        w, h = self._fbo.size
        if (w, h) != (self.fb_w, self.fb_h):
            self.fb_w, self.fb_h = w, h
            self.eye_w, self.eye_h = w // 2, h
            self.proj = perspective(self.fov, self.eye_w / float(max(1, h)))
        job, self._render_job = self._render_job, None
        if job is not None:
            job()
        else:
            self._fbo.use()
            self.ctx.clear(0.02, 0.02, 0.03, 1.0)
        self._rendered = True
        return True

    def _pump(self):
        while self._main.pending():
            self._main.iteration(False)

    def _on_close_request(self, win):
        self.quit = True
        return True                       # we destroy it ourselves in run()

    def _gtk_key(self, ctrl, keyval, keycode, state):
        k = self.keys
        code = k.from_gdk(self.display, keyval, keycode)
        if code == k.KEY_UNKNOWN:
            return False
        self._gl()
        self._on_key(self.win, code, keycode, k.PRESS, k.mods_from_gdk(state))
        return True

    def _gtk_motion(self, ctrl, x, y):
        self._gl()
        self._on_cursor(self.win, x, y)

    def _gtk_click(self, gesture, n_press, x, y, action):
        # GDK numbers buttons 1/2/3 = left/middle/right
        button = {1: self.keys.MOUSE_BUTTON_LEFT,
                  2: self.keys.MOUSE_BUTTON_MIDDLE,
                  3: self.keys.MOUSE_BUTTON_RIGHT}.get(
                      gesture.get_current_button(), -1)
        self._gl()
        self._on_cursor(self.win, x, y)
        self._on_mouse(self.win, button, action, 0)

    def _on_monitors_changed(self, model, position, removed, added):
        # Mutter re-places fullscreen windows whenever the monitor set
        # changes; put ours back on the glasses once the change has landed
        if not self.windowed and not self.parked:
            self.reassert_output_soon()

    def blank_window(self):
        """Put up one black frame -- what a parked window shows."""
        def black():
            self._fbo.use()
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._render_job = None
        self._rendered = False
        if self.ctx is not None:
            self._render_job = black
            self.area.queue_render()
            deadline = time.time() + 0.3
            while not self._rendered and time.time() < deadline:
                if self._main.pending():
                    self._main.iteration(False)
                else:
                    time.sleep(0.001)
            self._render_job = None
            self._gl()

    def close(self):
        """Destroy the window (run() does this itself on exit)."""
        try:
            self.win.destroy()
            self._pump()
        except Exception:                                 # noqa: BLE001
            pass

    # -- scene stack ------------------------------------------------------

    def push(self, scene):
        self.scenes.append(scene)
        scene.enter(self)

    def pop(self):
        if self.scenes:
            self.scenes.pop().exit(self)
        if not self.scenes:
            self.quit = True

    def switch(self, scene, keep_root=True):
        """Replace the running sub-experience, keeping the root (home) scene
        underneath -- quick-switching from Desk to 360 must still leave Esc
        meaning "back to home", not "quit"."""
        floor = 1 if (keep_root and self.scenes) else 0
        while len(self.scenes) > floor:
            self.scenes.pop().exit(self)
        self.push(scene)

    def launch(self, entry, replace=False):
        """Start a sub-experience from its registry entry, surviving a
        broken one. True if it is now running.

        Scenes run in-process and plugins are arbitrary code, so a factory
        or enter() that raises must not take the shell down: the
        half-started scene is removed again (its exit() gets a chance to
        release whatever enter() took), the failure is shown in the glasses,
        and the traceback goes to the log.

        replace=True swaps out the running sub-experience (the HUD's quick
        switch); False stacks it on top (the launcher).
        """
        import traceback
        try:
            scene = entry.make_scene()
        except Exception as e:                            # noqa: BLE001
            return self._launch_failed(entry, e, traceback.format_exc())
        if replace:
            floor = 1 if self.scenes else 0
            while len(self.scenes) > floor:
                self.scenes.pop().exit(self)
        self.scenes.append(scene)
        try:
            scene.enter(self)
        except Exception as e:                            # noqa: BLE001
            self.scenes.remove(scene)
            try:
                scene.exit(self)
            except Exception:                             # noqa: BLE001
                pass
            return self._launch_failed(entry, e, traceback.format_exc())
        return True

    def _launch_failed(self, entry, err, tb):
        print("  launch: %s failed to start -- %s: %s\n%s"
              % (entry.name, type(err).__name__, err, tb), flush=True)
        self.status.set_lines(["%s failed to start" % entry.title,
                               ("%s: %s" % (type(err).__name__, err))[:70]],
                              ttl=6.0)
        if not self.scenes:
            self.quit = True
        return False

    def save_config(self):
        if not self.config_dirty:
            return
        try:
            self._config_mod.save(self.config)
            self.config_dirty = False
        except OSError as e:
            print("  config save failed: %s" % e, flush=True)

    @property
    def scene(self):
        return self.scenes[-1] if self.scenes else None

    # -- camera -----------------------------------------------------------

    def recenter(self):
        if self.head:
            self.head.recenter()
            print("  recentered", flush=True)

    def grab_focus(self):
        """Take the keyboard by clicking our own window.

        A fullscreen window on the glasses output does not hold focus while
        the wearer works on the laptop panel, and the compositor may refuse
        a present(). GNOME does focus on click, and we can inject one
        through the RemoteDesktop session.

        Returns True if we ended up focused.
        """
        if self.win.is_active():
            return True
        self.win.present()                # polite first; sometimes enough
        self._pump()
        if self.win.is_active():
            return True
        try:
            inp = self._ensure_input()
            if inp and inp.ready():
                w, h = inp.frame_sizes()[0] or (3840, 1080)
                inp.move_pointer(0, w * 0.5, h * 0.5)
                inp.click()
                for _ in range(10):
                    self._pump()
                    time.sleep(0.01)
        except Exception as e:                            # noqa: BLE001
            print("  focus: click failed: %s" % e, flush=True)
        return bool(self.win.is_active())

    def reassert_output(self):
        """Put our window back, fullscreen, on the glasses connector.

        Mutter moves a fullscreen window onto the laptop panel when the
        monitor set changes (Desk's virtual monitors, a DP dropout).
        GTK's fullscreen_on_monitor() re-issued for the glasses brings it
        back.
        """
        if self.windowed:
            return False
        try:
            from refract.core import displaymode
            conn = displaymode.glasses_connector() or self.monitor
        except Exception:                                 # noqa: BLE001
            conn = self.monitor
        mon = self._gdk_monitor(conn)
        if mon is None:
            return False
        self.monitor = conn                  # the connector may have renamed
        self.win.fullscreen_on_monitor(mon)
        return True

    # Mutter applies a monitor-layout change asynchronously and moves a
    # fullscreen window while it does; re-placing the window straight away
    # gets undone, so wait for the layout to settle.
    REASSERT_AFTER = 0.7

    def reassert_output_soon(self):
        """reassert_output() once a layout change has had time to land."""
        self._reassert_at = time.time() + self.REASSERT_AFTER

    def release_pointer(self):
        """Put the pointer back on the laptop panel. Left on the glasses
        output it is invisible (our window covers it) and effectively lost."""
        try:
            inp = self._input
            if inp and inp.ready() and len(inp.specs) > 1:
                size = inp.frame_sizes()[1] or (1920, 1080)
                inp.move_pointer(1, size[0] * 0.5, size[1] * 0.5)
        except Exception:                                 # noqa: BLE001
            pass

    def _ensure_input(self):
        """A capture session used only for pointer input: the glasses output
        first (to click ourselves into focus), the laptop panel second (to
        hand the pointer back). fakesink -- we want the addressing, not the
        pixels."""
        if self._input is not None:
            return self._input
        from refract.core import displaymode
        from refract.core.vdisplay import ScreenCapture
        try:
            glasses = displaymode.glasses_connector() or self.monitor
            others = [o["connector"] for o in displaymode.list_outputs()
                      if o["connector"] != glasses]
            specs = [("monitor", glasses)]
            if others:
                specs.append(("monitor", others[0]))
            self._input = ScreenCapture(specs, capture=False)
            self._input.start()
            self._input.pump(2.0)
        except Exception as e:                            # noqa: BLE001
            print("  focus: input session failed: %s" % e, flush=True)
            self._input = False                          # do not retry
        return self._input or None

    def command(self, cmd):
        """Dispatch one control word: shell-wide first, then the scene.
        True if something understood it."""
        cmd = (cmd or "").strip().lower()
        if not cmd:
            return False
        if cmd == "recenter":
            self.recenter()
        elif cmd == "save":
            self.config_dirty = True
            self.save_config()
        elif cmd == "hud":
            self.hud.toggle()
        elif cmd == "quit":
            self.quit = True
        elif cmd == "home":
            while len(self.scenes) > 1:      # back to the root (home) scene
                self.scenes.pop().exit(self)
        elif cmd in ("park", "resume", "handoff"):
            from refract.core import handoff
            {"park": handoff.park, "resume": handoff.resume,
             "handoff": handoff.toggle}[cmd](self)
        elif not (self.scene and self.scene.on_command(self, cmd)):
            print("  ctl: unknown command %r" % cmd, flush=True)
            return False
        print("  ctl: %s" % cmd, flush=True)
        return True

    def poll_control(self):
        """Run every command waiting on the control socket (each sender
        gets an answer -- see refract.core.control)."""
        self._ctl.poll(self.command)

    def head_rot(self):
        """The head rotation for the frame being drawn.

        Inside render_frame() this is one snapshot shared by both eyes and
        every overlay; reading the live IMU per call would let a sample land
        between the eyes and give them different poses.
        """
        if self._frame_rot is not None:
            return self._frame_rot
        if self.sim_rot is not None:
            return self.sim_rot
        if self.head:
            return self.head.matrix()
        return np.eye(3, dtype="f4")

    def mvp(self, eye, world=None, model=None, rot=None):
        """proj @ view(eye) @ world @ model.

        For a camera at world position p = R * (offset,0,0), the view matrix
        translation is -R^T * p, which collapses to just -(offset,0,0).
        Rotating it again would swing the eye separation around as the head
        turns.
        """
        rot = self.head_rot() if rot is None else rot
        offset = (-0.5 + eye) * self.ipd     # left eye sits to the left
        view = np.eye(4, dtype="f4")
        view[:3, :3] = rot.T
        view[:3, 3] = np.array([-offset, 0.0, 0.0], dtype="f4")
        m = self.proj @ view
        if world is not None:
            m = m @ world
        if model is not None:
            m = m @ model
        return m

    # -- input ------------------------------------------------------------

    def _on_key(self, w_, key, sc, action, mods):
        k = self.keys
        if action not in (k.PRESS, k.REPEAT):
            return
        # the HUD combo is checked BEFORE the scene, so a sub-experience can
        # never bind over the one key that gets you out of it
        if self.hud.matches(key, mods):
            self.hud.toggle()
            return
        if self.hud.open:
            self.hud.on_key(key, mods)
            return
        if self.scene and self.scene.on_key(self, key, sc, action, mods):
            return
        if key in (k.KEY_ESCAPE, k.KEY_Q):
            self.pop()
        elif key == k.KEY_R:
            self.recenter()

    def _on_cursor(self, w_, x, y):
        # the cursor arrives in widget (logical) coordinates, which need not
        # match the framebuffer size
        ww, wh = self.area.get_width(), self.area.get_height()
        self.cursor_ndc = (2.0 * x / max(ww, 1) - 1.0,
                           1.0 - 2.0 * y / max(wh, 1))
        if self.hud.open:
            self.hud.on_cursor(*self.cursor_ndc)
        elif self.scene:
            self.scene.on_cursor(self, *self.cursor_ndc)

    def _on_mouse(self, w_, button, action, mods):
        if self.hud.open:
            self.hud.on_mouse(button, action)
        elif self.scene:
            self.scene.on_mouse(self, button, action, mods)

    # -- drawing ----------------------------------------------------------

    def render_frame(self, timeout=0.5, grab=None):
        """Draw one stereo frame (scene, status, HUD per eye) and wait for
        GTK to put it up. True if it was drawn within `timeout` -- never
        blocks longer, so a window the compositor is not showing (moved,
        minimized) cannot freeze the loop. With `grab`, also save it.

        GtkGLArea only lets us draw inside its render signal, so this queues
        the work and pumps GTK until the signal has run it.
        """
        self._render_job = lambda: self._draw(grab)
        self._rendered = False
        self.area.queue_render()
        deadline = time.time() + timeout
        while not self._rendered and time.time() < deadline:
            if self._main.pending():
                self._main.iteration(False)
            else:
                time.sleep(0.001)
        self._render_job = None
        self._gl()
        return self._rendered

    def _draw(self, grab=None):
        if not hasattr(self, "screen_prog"):      # first frame, during init
            self._fbo.use()
            self.ctx.clear(0.02, 0.02, 0.03, 1.0)
            return
        self.frames += 1
        self._fbo.use()
        self.ctx.enable(self.moderngl.DEPTH_TEST)
        self.ctx.clear(0.02, 0.02, 0.03, 1.0)
        self._frame_rot = None
        self._frame_rot = self.head_rot()
        try:
            for eye in (0, 1):
                self.ctx.viewport = (eye * self.eye_w, 0, self.eye_w,
                                     self.eye_h)
                if self.scene:
                    self.scene.render_eye(self, eye)
                self.status.draw()
                self.hud.render_eye(self, eye)
        finally:
            self._frame_rot = None
        if grab:
            self._save_fbo(grab)

    def grab(self, path, quiet=False):
        """Render a frame and save it as an image."""
        self._grab_quiet = quiet
        self.render_frame(grab=path)

    def _save_fbo(self, path):
        from PIL import Image
        buf = self._fbo.read(components=3)
        shot = np.frombuffer(buf, dtype=np.uint8).reshape(
            self.fb_h, self.fb_w, 3)[::-1]
        Image.fromarray(shot).save(path)
        if not getattr(self, "_grab_quiet", False):
            print("  wrote        : %s  %dx%d" % (path, self.fb_w, self.fb_h))

    # -- main loop --------------------------------------------------------

    def run(self, capture=None, capture_after=3.0):
        self.t0 = time.time()
        last = self.t0
        try:
            while not self.quit:
                now = time.time()
                self._pump()
                self._gl()
                dt = now - last
                last = now

                # Recenter on a countdown: the first sample arrives while the
                # glasses are still in your hand.
                if self.head and not self.head.centered \
                        and self.head.samples > 5:
                    el = now - self.t0
                    if el >= self.recenter_after:
                        self.head.recenter()
                        print("  recentered", flush=True)
                    else:
                        # short ttl, re-set every frame: the line disappears
                        # by itself once the countdown ends
                        self.status.set_lines(
                            ["look STRAIGHT AHEAD",
                             "recentering in %.0f"
                             % max(1, round(self.recenter_after - el))],
                            ttl=0.5)

                self.poll_control()
                # Autosave a second after the last burst of changes, so a
                # crash or kill does not lose the session's adjustments, and
                # holding a key on a slider is one write, not twenty.
                if self.config_dirty:
                    if self._config_dirty_since is None:
                        self._config_dirty_since = now
                    elif now - self._config_dirty_since >= CONFIG_AUTOSAVE:
                        self.save_config()
                        self._config_dirty_since = None
                if self.head:
                    from refract.core import handoff as _handoff
                    _handoff.poll_device(self, now)
                self.input.poll(self, now)
                # --log-axis: the axis each head movement turns about; a pure
                # pitch should come back as [1 0 0].
                if self.log_axis and self.head and now - self._axis_t >= 0.4:
                    from refract.core.head import axis_of
                    ax, ang = axis_of(self.head_rot())
                    if ang > 4.0:
                        print("  axis [%+.3f %+.3f %+.3f] %5.1f deg  %s"
                              % (ax[0], ax[1], ax[2], ang,
                                 "pitch" if abs(ax[0]) > 0.8 else
                                 "yaw" if abs(ax[1]) > 0.8 else
                                 "roll" if abs(ax[2]) > 0.8 else "MIXED"),
                              flush=True)
                        self._axis_t = now
                if self._reassert_at is not None and now >= self._reassert_at \
                        and not self.parked:
                    self._reassert_at = None
                    ok = self.reassert_output()
                    print("  window re-placed on %s%s" % (
                        self.monitor, "" if ok else " -- FAILED"), flush=True)
                self.hud.update_gaze(self, dt, now)
                if self.scene and not self.parked:
                    self.scene.update(self, dt)

                # parked: no capture, no rendering
                if self.parked:
                    self._pump()
                    time.sleep(0.05)
                    continue

                if capture and now - self.t0 >= capture_after:
                    self.grab(capture)
                    break
                # paced by GTK's frame clock (vsync); a frame that does not
                # come back within the timeout is skipped, never waited on
                self.render_frame()
        finally:
            el = time.time() - self.t0
            if self.frames:
                print("\n  rendered     : %d frames in %.1fs = %.1f fps"
                      % (self.frames, el, self.frames / el))
            if self._input:
                self._input.stop()
            if self._unplug_dialog and self._unplug_dialog.poll() is None:
                self._unplug_dialog.terminate()
            while self.scenes:
                self.scenes.pop().exit(self)
            self.save_config()
            from refract.core import handoff
            handoff.leave_2d_on_exit(self)
            if self.head:
                self.head.stop()
            self._ctl.close()
            self.close()

    @staticmethod
    def hard_exit(rc=0):
        """The vendor SDKs leave threads that never join (public SDK deinit()
        hangs; libglasses' USB thread never exits) -- a normal interpreter
        exit deadlocks. Call this instead of sys.exit at process end."""
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
