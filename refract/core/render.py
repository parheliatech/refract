"""Stereo renderer, scene stack, and the minimal glass-panel UI toolkit.

The frame is drawn per eye as: active scene -> status overlay -> HUD (Phase
3). Scenes are in-process modules on a stack (architecture decision A1: one
process owns the glasses; switching modes is a scene swap, no device handoff,
no GL context churn).

Extraction of xrdesk.py's renderer, behaviour preserved -- including the
comments that record why each non-obvious line is the way it is.
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
CTL_PATH = os.path.join(RUNTIME_DIR, "refract.ctl")
PID_PATH = os.path.join(RUNTIME_DIR, "refract.pid")


def already_running():
    """PID of another live Refract, or None.

    Two instances is not a harmless mistake: they fight over the glasses
    (USB is exclusive), they each create their own virtual monitors, they
    BOTH rearrange the desktop layout, and they race for the same control
    file -- a command typed for one is as likely to be eaten by the other.
    Observed in the wild while testing.
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
            cmd = f.read().decode(errors="replace")
    except OSError:
        return None                     # stale pid file, nobody home
    return pid if "refract" in cmd else None


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
        # v=0 at the TOP. Verified against the raw PipeWire frame rather than
        # reasoned about: the source arrives top-row-first, and this mapping
        # reproduces it the right way up in the framebuffer.
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
    through the lens, which is the whole naming conceit. Shared chrome: the
    HUD reuses this in Phase 3."""
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
    """A textured screen floating in world space (curved or flat). The one
    drawable every scene so far needs: desktop monitors, launcher tiles, the
    test card."""

    def __init__(self, app, size_px, width_m=1.15, distance=1.2,
                 yaw=0.0, curve=1.0, alpha=False):
        self.app = app
        self.size_px = size_px
        self.tex = app.ctx.texture(size_px, 4)
        self.tex.filter = (app.moderngl.LINEAR, app.moderngl.LINEAR)
        self.tex.repeat_x = self.tex.repeat_y = False
        # Side screens are seen at a steep angle, where isotropic filtering
        # smears text into mush. Cheap on any GPU that supports it.
        try:
            self.tex.anisotropy = 16.0
        except Exception:                                 # noqa: BLE001
            pass
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
        self.tex = self.app.ctx.texture(size_px, 4)
        self.tex.filter = (self.app.moderngl.LINEAR, self.app.moderngl.LINEAR)
        self.tex.repeat_x = self.tex.repeat_y = False
        try:
            self.tex.anisotropy = 16.0
        except Exception:                                 # noqa: BLE001
            pass
        if self._geom:
            self.set_geometry(**self._geom)
        return True

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
    """A screen-space texture strip (NDC rect), drawn per eye over the scene.
    Used for the status bar now and the HUD navigator in Phase 3."""

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
        seconds -- the default for anything a scene wants to SAY.

        Persistent status text is clutter: it floats in the middle of the
        view forever, and once the HUD exists there is nowhere it needs to
        live permanently. Feedback should appear when something changes and
        then get out of the way.
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
        """A single word from the control file. Return True if handled."""
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
        """Phase 3: [{key, label, kind, ...}] rendered into a HUD panel."""
        return []


class App:
    """The persistent Refract process: window, GL context, IMU, scene stack.

    options: fov, ipd, monitor, windowed, size_win, platform, recenter_after.
    """

    def __init__(self, head=None, sim_rot=None, fov=46.0, ipd=0.063,
                 monitor="DP-2", windowed=False, size_win=(1920, 540),
                 platform="any", recenter_after=25.0, title="Refract",
                 config=None, log_axis=False):
        from refract.core import config as config_mod
        self.config = config_mod.load() if config is None else config
        self.config_dirty = False
        self._config_mod = config_mod
        self.head = head
        self.sim_rot = sim_rot
        self.fov = fov
        self.ipd = ipd
        self.monitor = monitor
        self.recenter_after = recenter_after
        self.log_axis = log_axis
        self._axis_t = 0.0
        self._input = None                # lazy pointer-input session
        self.sbs_ours = False             # did WE switch the glasses to SBS?
        self.parked = False               # display handed back to the laptop
        self._device_t = 0.0              # glasses-presence poll
        self._device_present = True
        self._parked_by_unplug = False
        self.scenes = []
        self.quit = False
        self.t0 = None
        self.frames = 0

        import glfw
        self.glfw = glfw
        if platform != "any":
            try:
                glfw.init_hint(glfw.PLATFORM,
                               {"x11": glfw.PLATFORM_X11,
                                "wayland": glfw.PLATFORM_WAYLAND}[platform])
            except AttributeError:
                pass
        if not glfw.init():
            raise RuntimeError("glfw init failed")

        mon = None
        if not windowed:
            for m in glfw.get_monitors():
                if glfw.get_monitor_name(m).decode() == monitor:
                    mon = m
            mon = mon or glfw.get_primary_monitor()
            vm = glfw.get_video_mode(mon)
            win_w, win_h = vm.size.width, vm.size.height
        else:
            win_w, win_h = size_win

        # Tell the desktop who we are. GNOME matches a window to its
        # .desktop file by app id, and the id must equal the desktop file's
        # basename -- refract.desktop -> "refract". Without it the shell has
        # nothing to match, so the dock shows a generic icon and calls the
        # window "unknown" however nicely the window itself is titled.
        for hint in ("WAYLAND_APP_ID", "X11_CLASS_NAME", "X11_INSTANCE_NAME"):
            h = getattr(glfw, hint, None)
            if h is not None:
                try:
                    glfw.window_hint_string(h, APP_ID)
                except Exception:                         # noqa: BLE001
                    pass

        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        # a fullscreen window minimises itself the moment it loses focus
        # otherwise
        glfw.window_hint(glfw.AUTO_ICONIFY, False)
        self.win = glfw.create_window(win_w, win_h, title, mon, None)
        if not self.win:
            glfw.terminate()
            raise RuntimeError("could not create window")
        glfw.show_window(self.win)
        glfw.restore_window(self.win)
        glfw.make_context_current(self.win)
        glfw.swap_interval(1)

        import moderngl
        self.moderngl = moderngl
        self.ctx = moderngl.create_context()
        # glfw lies about the framebuffer size under wayland (window size x
        # content scale, 3.0 on the glasses panel) -- take it from GL.
        self.fb_w, self.fb_h = self.ctx.screen.size
        self.eye_w, self.eye_h = self.fb_w // 2, self.fb_h

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

        # Three quick nods toggle the HUD. Needed because GNOME swallows the
        # key combinations before a fullscreen window sees them -- measured
        # on a head: neither Ctrl+Super+R nor Ctrl+Alt+R ever arrived.
        from refract.core.gesture import HeadBob
        self.bob = HeadBob()

        self.cursor_ndc = (0.0, 0.0)
        glfw.set_key_callback(self.win, self._on_key)
        glfw.set_cursor_pos_callback(self.win, self._on_cursor)
        glfw.set_mouse_button_callback(self.win, self._on_mouse)

        # Recentre over a SIGNAL as well as a key. A window launched in the
        # background never receives keyboard input, so 'r' is unreachable
        # exactly when you most need it -- and a bad reference pose does not
        # merely offset the view, it makes head pitch come out as roll,
        # because your motion is then measured in a tilted frame.
        #     pkill -USR1 -f refract
        # Without these, `kill`, a logout or a shutdown ends the process
        # outright and NOTHING in the finally block runs: the laptop panel
        # stays dark, the monitor layout stays rearranged and the glasses
        # stay in side-by-side. Asking the loop to stop instead means the
        # ordinary cleanup path does its job.
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
        # Discard any command left over from a previous run. A control word
        # is addressed to the instance that was running when it was written,
        # and an unconsumed one outlives it: a stale "quit" made the NEXT
        # launch exit three seconds in, which reads as a crash.
        try:
            os.unlink(CTL_PATH)
        except OSError:
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
            print("  recentred", flush=True)

    def grab_focus(self):
        """Take the keyboard by clicking our own window.

        A fullscreen window on the glasses output does NOT hold focus while
        the wearer is working on the laptop panel, so the HUD opens and
        every keystroke still goes to whatever they were using. Asking
        politely (glfw.focus_window) is refused by the compositor. Clicking
        is not: GNOME focuses on click, and we can inject one through the
        RemoteDesktop session.

        Returns True if we ended up focused.
        """
        g = self.glfw
        if g.get_window_attrib(self.win, g.FOCUSED):
            return True
        g.focus_window(self.win)          # polite first; sometimes enough
        g.poll_events()
        if g.get_window_attrib(self.win, g.FOCUSED):
            return True
        try:
            inp = self._ensure_input()
            if inp and inp.ready():
                w, h = inp.frame_sizes()[0] or (3840, 1080)
                inp.move_pointer(0, w * 0.5, h * 0.5)
                inp.click()
                for _ in range(10):
                    g.poll_events()
                    time.sleep(0.01)
        except Exception as e:                            # noqa: BLE001
            print("  focus: click failed: %s" % e, flush=True)
        return bool(g.get_window_attrib(self.win, g.FOCUSED))

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
        """Dispatch one control word: shell-wide first, then the scene."""
        cmd = (cmd or "").strip().lower()
        if not cmd:
            return
        if cmd == "recenter":
            self.recenter()
        elif cmd == "save":
            self.config_dirty = True
            self.save_config()
        elif cmd == "hud":
            self.hud.toggle()
        elif cmd == "quit":
            self.quit = True
        elif cmd in ("park", "resume", "handoff"):
            from refract.core import handoff
            {"park": handoff.park, "resume": handoff.resume,
             "handoff": handoff.toggle}[cmd](self)
        elif not (self.scene and self.scene.on_command(self, cmd)):
            print("  ctl: unknown command %r" % cmd, flush=True)
            return
        print("  ctl: %s" % cmd, flush=True)

    def poll_control(self):
        try:
            with open(CTL_PATH) as f:
                cmd = f.read().strip()
            os.unlink(CTL_PATH)
        except OSError:
            return
        self.command(cmd)

    def head_rot(self):
        if self.sim_rot is not None:
            return self.sim_rot
        if self.head:
            return self.head.matrix()
        return np.eye(3, dtype="f4")

    def mvp(self, eye, world=None, model=None, rot=None):
        """proj @ view(eye) @ world @ model.

        For a camera at world position p = R * (offset,0,0), the view matrix
        translation is -R^T * p, which collapses to just -(offset,0,0).
        Rotating it again -- as an earlier version did -- swings the eye
        separation around as you turn your head.
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
        glfw = self.glfw
        if action not in (glfw.PRESS, glfw.REPEAT):
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
        if key in (glfw.KEY_ESCAPE, glfw.KEY_Q):
            self.pop()
        elif key == glfw.KEY_R:
            self.recenter()

    def _on_cursor(self, w_, x, y):
        # Cursor arrives in WINDOW coordinates, so normalise against the
        # window -- not against ctx.screen.size, which is the drawable. They
        # happen to match on this host, but the wayland framebuffer-size lie
        # means that is a coincidence, not a rule.
        ww, wh = self.glfw.get_window_size(self.win)
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

    def render_frame(self):
        """One stereo frame: scene then status, per eye. Split out of run()
        so tests can drive the shell frame by frame."""
        self.frames += 1
        self.ctx.screen.use()
        self.ctx.clear(0.02, 0.02, 0.03, 1.0)
        for eye in (0, 1):
            self.ctx.viewport = (eye * self.eye_w, 0, self.eye_w, self.eye_h)
            if self.scene:
                self.scene.render_eye(self, eye)
            self.status.draw()
            self.hud.render_eye(self, eye)

    def grab(self, path, quiet=False):
        from PIL import Image
        buf = self.ctx.screen.read(components=3)
        shot = np.frombuffer(buf, dtype=np.uint8).reshape(
            self.fb_h, self.fb_w, 3)[::-1]
        Image.fromarray(shot).save(path)
        if not quiet:
            print("  wrote        : %s  %dx%d" % (path, self.fb_w, self.fb_h))

    # -- main loop --------------------------------------------------------

    def run(self, capture=None, capture_after=3.0):
        glfw = self.glfw
        self.t0 = time.time()
        last = self.t0
        try:
            while not glfw.window_should_close(self.win) and not self.quit:
                now = time.time()
                dt = now - last
                last = now

                # Recentre on a COUNTDOWN, shell-wide. Taking the reference
                # from the first sample points the view wherever the glasses
                # happened to be lying (that sample arrives while they are
                # still in your hand), and a background-launched window has no
                # keyboard focus, so 'r' may not reach us to fix it.
                if self.head and self.head.qref is None \
                        and self.head.samples > 5:
                    el = now - self.t0
                    if el >= self.recenter_after:
                        self.head.recenter()
                        print("  recentred", flush=True)
                    else:
                        # ttl, and re-set every frame while counting: without
                        # it the last countdown line hangs in the view
                        # forever once recentring completes, because nothing
                        # ever clears it
                        self.status.set_lines(
                            ["look STRAIGHT AHEAD",
                             "recentring in %.0f"
                             % max(1, round(self.recenter_after - el))],
                            ttl=0.5)

                self.poll_control()
                if self.head:
                    from refract.core import handoff as _handoff
                    _handoff.poll_device(self, now)
                if self.head and self.config.setdefault("global", {}).get(
                        "head_bob", True):
                    fwd = self.head_rot() @ np.array([0.0, 0.0, -1.0],
                                                     dtype="f4")
                    pitch = math.degrees(math.asin(
                        max(-1.0, min(1.0, float(fwd[1])))))
                    if self.bob.update(now, pitch):
                        print("  head bob -> hud", flush=True)
                        self.hud.toggle()
                # Say what axis the head actually turned about. Guessing sign
                # conventions has failed repeatedly; this states outright
                # whether a pitch comes back as a rotation about X (clean) or
                # something tilted (a Z component IS the roll bleed).
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
                self.hud.update_gaze(self, dt, now)
                if self.scene and not self.parked:
                    self.scene.update(self, dt)

                # parked means the machine is the wearer's again: no capture,
                # no rendering into a display they are not looking through
                if self.parked:
                    # Do NOT swap buffers while parked. The window is
                    # iconified, so the compositor never sends a frame
                    # callback and swap_buffers blocks forever -- which
                    # froze the loop and made "quit" while parked look like
                    # a hang. Poll and idle instead.
                    glfw.poll_events()
                    time.sleep(0.05)
                    continue

                self.render_frame()

                if capture and now - self.t0 >= capture_after:
                    self.grab(capture)
                    break

                glfw.swap_buffers(self.win)
                glfw.poll_events()
        finally:
            el = time.time() - self.t0
            if self.frames:
                print("\n  rendered     : %d frames in %.1fs = %.1f fps"
                      % (self.frames, el, self.frames / el))
            if self._input:
                self._input.stop()
            while self.scenes:
                self.scenes.pop().exit(self)
            self.save_config()
            # Hand the glasses back the way we found them. We put them into
            # side-by-side at boot; leaving them there means the wearer has
            # to run viture-hw.py by hand before the panel is usable as an
            # ordinary display again. (The full version of this is phase 5's
            # Display Handoff; this is just not making a mess on the way out.)
            # Ask the display what mode it is ACTUALLY in rather than
            # assuming. Switching when already in 2D just waits out the
            # re-enumeration timeout and makes quitting feel hung; assuming
            # parked means 2D left the glasses in SBS, because the mode can
            # come back on its own when our fullscreen window goes away.
            # "Only restore what we switched" was too literal: the glasses
            # were often ALREADY in SBS at launch (left there by a previous
            # session), so every run declined to fix it and they stayed stuck
            # in a mode that is useless as an ordinary monitor. Default to
            # leaving a usable 2D panel; global.keep_sbs opts out.
            leave_2d = not self.config.setdefault("global", {}).get(
                "keep_sbs", False)
            if leave_2d and self.head:
                try:
                    from refract.core import displaymode
                    if displaymode.is_sbs(self.monitor):
                        self.head.set_sbs(False)
                        print("  glasses     : back to 2D", flush=True)
                    else:
                        print("  glasses     : already 2D", flush=True)
                except Exception as e:                    # noqa: BLE001
                    print("  glasses: could not restore 2D: %s" % e,
                          flush=True)
            if self.head:
                self.head.stop()
            glfw.terminate()

    @staticmethod
    def hard_exit(rc=0):
        """The vendor SDKs leave threads that never join (public SDK deinit()
        hangs; libglasses' USB thread never exits) -- a normal interpreter
        exit deadlocks. Call this instead of sys.exit at process end."""
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
