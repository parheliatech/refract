#!/usr/bin/env python3
"""
i3d_view -- phase 2: the i3d stereo pass on screen, fullscreen on the glasses.

Phase 1 (i3d_still.py) proved the shader port offline. This puts the same
pipeline in a real GL context and draws it to a display:

    image -> Depth Anything (TFLite) -> normalize -> i3d_fragment x2
          -> left half | right half of the framebuffer

No readback. Each eye is a viewport into the window, so at 3840x1080 the
window IS the side-by-side signal the glasses expect in 3D mode.

    ./i3d_view.py --list-monitors
    ./i3d_view.py photo.jpg --windowed              # laptop screen, safe
    ./i3d_view.py photo.jpg --anaglyph --windowed   # verify depth by eye
    ./i3d_view.py ~/Pictures/*.jpg --monitor DP-2   # fullscreen on the glasses

Glasses must be in SBS mode first:  ../viture-hw.py 3d on

Keys:  <- ->  prev/next image      1/2/3  low/medium/high strength
       a      anaglyph toggle      s      screenshot the framebuffer
       f      cycle fit/stretch    ESC/q  quit
"""

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from i3d_still import (FRAG, INC, INCM, STRENGTH, VERT,      # noqa: E402
                       infer_depth, normalize_depth)


def contain(w_src, h_src, w_dst, h_dst):
    """Fractional rect (x0, y0, x1, y1) of a w_src x h_src image letterboxed
    into w_dst x h_dst. Used to place image and depth identically."""
    s = min(w_dst / w_src, h_dst / h_src)
    w, h = w_src * s, h_src * s
    x0, y0 = (w_dst - w) / 2 / w_dst, (h_dst - h) / 2 / h_dst
    return x0, y0, 1.0 - x0, 1.0 - y0


def fit_rgb(img, out_w, out_h, stretch):
    """Source image at framebuffer-eye resolution, letterboxed unless stretch."""
    from PIL import Image
    if stretch:
        return np.asarray(img.resize((out_w, out_h), Image.LANCZOS),
                          dtype=np.uint8)
    x0, y0, x1, y1 = contain(img.width, img.height, out_w, out_h)
    w, h = round((x1 - x0) * out_w), round((y1 - y0) * out_h)
    canvas = Image.new("RGB", (out_w, out_h), (0, 0, 0))
    canvas.paste(img.resize((w, h), Image.LANCZOS),
                 ((out_w - w) // 2, (out_h - h) // 2))
    return np.asarray(canvas, dtype=np.uint8)


def fit_depth(d01, img_w, img_h, out_w, out_h, stretch):
    """Depth letterboxed to match fit_rgb, kept at MODEL resolution.

    Model-res matters: increaseEdges() steps by uDepthTexel, so resampling the
    depth to 1080p would silently change the edge-softening scale.

    The bars take the adjacent content depth (edge replication), not a flat
    0.5: a constant there is a depth cliff at the picture edge and the march
    tears the content against it. The bars are black, so their disparity is
    invisible either way.
    """
    if stretch:
        return d01
    dh, dw = d01.shape
    x0, y0, x1, y1 = contain(img_w, img_h, out_w, out_h)
    w, h = max(1, round((x1 - x0) * dw)), max(1, round((y1 - y0) * dh))
    from PIL import Image
    inner = np.asarray(Image.fromarray(d01).resize((w, h), Image.BILINEAR),
                       dtype=np.float32)
    ox, oy = (dw - w) // 2, (dh - h) // 2
    return np.pad(inner, ((oy, dh - h - oy), (ox, dw - w - ox)),
                  mode="edge").astype(np.float32)


class Frame:
    """One image, prepared for the shader."""

    def __init__(self, path, eye_w, eye_h, stretch):
        from PIL import Image
        src = Image.open(path).convert("RGB")
        self.path = path
        self.rgb = fit_rgb(src, eye_w, eye_h, stretch)
        raw, self.infer_ms = infer_depth(np.asarray(src, dtype=np.uint8))
        self.infer_ms *= 1000.0
        self.depth = fit_depth(normalize_depth(raw), src.width, src.height,
                               eye_w, eye_h, stretch)


def pick_monitor(glfw, want):
    mons = glfw.get_monitors()
    if want is None:
        return None, mons
    for i, m in enumerate(mons):
        name = glfw.get_monitor_name(m).decode()
        if want == name or want == str(i):
            return m, mons
    sys.exit("no monitor %r (see --list-monitors)" % want)


def main():
    ap = argparse.ArgumentParser(description="SpaceWalker 2D->3D, on screen")
    ap.add_argument("images", nargs="*")
    ap.add_argument("--monitor", help="monitor name (e.g. DP-2) or index")
    ap.add_argument("--list-monitors", action="store_true")
    ap.add_argument("--windowed", action="store_true",
                    help="windowed instead of fullscreen")
    ap.add_argument("--size", default="1920x540",
                    help="windowed size WxH (default 1920x540)")
    ap.add_argument("--strength", default="medium", help="low|medium|high|float")
    ap.add_argument("--anaglyph", action="store_true",
                    help="red/cyan overlay instead of SBS -- for eyeball checks")
    ap.add_argument("--stretch", action="store_true",
                    help="fill the eye, ignore aspect (default letterboxes)")
    ap.add_argument("--platform", choices=["x11", "wayland", "any"],
                    default="any",
                    help="force a GLFW platform. Default lets GLFW pick; both "
                         "backends enumerate the glasses and can fullscreen on "
                         "a chosen output. x11 needs GLFW_LIBRARY pointed at "
                         "the system libglfw (the pip wheel is wayland-only "
                         "in a wayland session).")
    ap.add_argument("--capture", metavar="FILE",
                    help="render one frame to FILE and exit (no interaction)")
    a = ap.parse_args()

    import glfw
    if a.platform != "any":
        glfw.init_hint(glfw.PLATFORM,
                       {"x11": glfw.PLATFORM_X11,
                        "wayland": glfw.PLATFORM_WAYLAND}[a.platform])
    if not glfw.init():
        sys.exit("glfw init failed")

    if a.list_monitors:
        for i, m in enumerate(glfw.get_monitors()):
            vm = glfw.get_video_mode(m)
            x, y = glfw.get_monitor_pos(m)
            mw, mh = glfw.get_monitor_physical_size(m)
            print("  [%d] %-8s %dx%d @%dHz  at +%d+%d  %dx%dmm" % (
                i, glfw.get_monitor_name(m).decode(),
                vm.size.width, vm.size.height, vm.refresh_rate, x, y, mw, mh))
        glfw.terminate()
        return 0

    if not a.images:
        sys.exit("give at least one image (or --list-monitors)")

    try:
        msft = STRENGTH[a.strength]
    except KeyError:
        msft = float(a.strength)

    mon, mons = pick_monitor(glfw, a.monitor)
    if a.windowed:
        win_w, win_h = (int(v) for v in a.size.lower().split("x"))
        mon = None
    else:
        if mon is None:
            mon = glfw.get_primary_monitor()
        vm = glfw.get_video_mode(mon)
        win_w, win_h = vm.size.width, vm.size.height

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    # AUTO_ICONIFY defaults TRUE: a fullscreen window minimises itself the
    # instant it loses input focus, which a background launch guarantees. The
    # window is then mapped, fullscreen and correctly placed on the glasses --
    # and showing the desktop. Cost an hour of chasing phantom placement bugs.
    glfw.window_hint(glfw.AUTO_ICONIFY, False)
    win = glfw.create_window(win_w, win_h, "i3d", mon, None)
    if not win:
        glfw.terminate()
        sys.exit("could not create window")
    glfw.show_window(win)
    glfw.restore_window(win)
    glfw.make_context_current(win)
    glfw.swap_interval(1)

    import moderngl
    ctx = moderngl.create_context()

    # Size from GL, NOT glfw.get_framebuffer_size(): the Wayland backend
    # multiplies by the output's content scale (3.0 on the glasses, a 160x100mm
    # panel), while the actual EGL drawable stays at window size. Trusting glfw
    # there makes every eye viewport 3x too large and the readback blow up.
    fb_w, fb_h = ctx.screen.size
    eye_w, eye_h = fb_w // 2, fb_h
    print("  window       : %dx%d  (%s)  eye %dx%d" % (
        fb_w, fb_h, "fullscreen %s" % glfw.get_monitor_name(mon).decode()
        if mon else "windowed", eye_w, eye_h))
    if not a.windowed and (fb_w, fb_h) != (3840, 1080):
        print("  note         : not 3840x1080 -- glasses are probably still in "
              "2D mode (../viture-hw.py 3d on)")
    prog = ctx.program(vertex_shader=VERT, fragment_shader=open(FRAG).read())
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1],
                               dtype="f4").tobytes())
    vao = ctx.simple_vertex_array(prog, quad, "aPos")
    prog["sTexture"].value = 0
    prog["sTextureDepth"].value = 1
    prog["inc"].value = INC
    prog["iM"].value = INCM
    prog["uDepthCoe"].value = 1.0

    state = {"idx": 0, "msft": msft, "anaglyph": a.anaglyph,
             "stretch": a.stretch, "reload": True, "shot": bool(a.capture)}
    tex_img = tex_d = None
    frame = None

    def on_key(w, key, sc, action, mods):
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        if key in (glfw.KEY_ESCAPE, glfw.KEY_Q):
            glfw.set_window_should_close(w, True)
        elif key == glfw.KEY_RIGHT:
            state["idx"] = (state["idx"] + 1) % len(a.images)
            state["reload"] = True
        elif key == glfw.KEY_LEFT:
            state["idx"] = (state["idx"] - 1) % len(a.images)
            state["reload"] = True
        elif key in (glfw.KEY_1, glfw.KEY_2, glfw.KEY_3):
            state["msft"] = STRENGTH[{glfw.KEY_1: "low", glfw.KEY_2: "medium",
                                      glfw.KEY_3: "high"}[key]]
            print("  strength     : %.4f" % state["msft"])
        elif key == glfw.KEY_A:
            state["anaglyph"] = not state["anaglyph"]
        elif key == glfw.KEY_F:
            state["stretch"] = not state["stretch"]
            state["reload"] = True
        elif key == glfw.KEY_S:
            state["shot"] = True

    glfw.set_key_callback(win, on_key)

    while not glfw.window_should_close(win):
        if state["reload"]:
            path = a.images[state["idx"]]
            t0 = time.time()
            frame = Frame(path, eye_w, eye_h, state["stretch"])
            for t in (tex_img, tex_d):
                if t:
                    t.release()
            h, w, _ = frame.rgb.shape
            tex_img = ctx.texture((w, h), 3, frame.rgb.tobytes())
            tex_img.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex_img.repeat_x = tex_img.repeat_y = False
            dh, dw = frame.depth.shape
            tex_d = ctx.texture((dw, dh), 1, frame.depth.tobytes(), dtype="f4")
            tex_d.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex_d.repeat_x = tex_d.repeat_y = False
            prog["uDepthTexel"].value = (1.0 / dw, 1.0 / dh)
            print("  [%d/%d] %s  depth %.0f ms  total %.0f ms" % (
                state["idx"] + 1, len(a.images), os.path.basename(path),
                frame.infer_ms, (time.time() - t0) * 1000))
            state["reload"] = False

        tex_img.use(0)
        tex_d.use(1)
        fbo = ctx.screen
        fbo.use()
        fbo.color_mask = (True, True, True, True)
        ctx.clear(0.0, 0.0, 0.0)

        t0 = time.time()
        if state["anaglyph"]:
            # left eye -> red, right eye -> cyan, same pixels
            ctx.viewport = (0, 0, fb_w, fb_h)
            for sign, mask in ((+1.0, (True, False, False, True)),
                               (-1.0, (False, True, True, True))):
                fbo.color_mask = mask
                prog["mS"].value = sign * state["msft"]
                vao.render(moderngl.TRIANGLE_STRIP)
            fbo.color_mask = (True, True, True, True)
        else:
            for sign, vp in ((+1.0, (0, 0, eye_w, eye_h)),
                             (-1.0, (eye_w, 0, eye_w, eye_h))):
                ctx.viewport = vp
                prog["mS"].value = sign * state["msft"]
                vao.render(moderngl.TRIANGLE_STRIP)
        ctx.finish()
        render_ms = (time.time() - t0) * 1000

        if state["shot"]:
            from PIL import Image
            buf = ctx.screen.read(components=3)
            shot = np.frombuffer(buf, dtype=np.uint8).reshape(fb_h, fb_w, 3)[::-1]
            out = a.capture or "i3d_view_shot.png"
            Image.fromarray(shot).save(out)
            print("  wrote        : %s  %dx%d" % (out, fb_w, fb_h))
            state["shot"] = False
            if a.capture:
                if not state["anaglyph"]:
                    l, r = shot[:, :eye_w], shot[:, eye_w:]
                    d = np.abs(l.astype(np.int16) - r.astype(np.int16)).mean()
                    print("  mean L/R diff: %.2f  %s" % (
                        d, "OK - eyes differ" if d > 0.5 else "SUSPECT"))
                print("  stereo pass  : %.1f ms for both eyes at %dx%d"
                      % (render_ms, eye_w, eye_h))
                break

        glfw.set_window_title(
            win, "i3d  %s  msft=%.3f  stereo %.1f ms" % (
                os.path.basename(frame.path), state["msft"], render_ms))
        glfw.swap_buffers(win)
        glfw.poll_events()

    glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
