#!/usr/bin/env python3
"""
i3d_video -- phase 3: video through the i3d stereo pass, on the glasses.

    ffmpeg -> rgb24 frames -> Depth Anything -> i3d_fragment x2 -> SBS

Three threads, because the stages have wildly different costs: decode is
~2 ms/frame, depth is ~250 ms/frame, the stereo pass is ~20 ms/frame at
1920x1080 per eye. Decode and inference run ahead of the renderer through
bounded queues; GL stays on the main thread.

Phase 3 is about correctness, not speed. With --every 1 (depth on every frame)
playback runs at inference rate, roughly 4 fps. --every N infers every Nth
frame and HOLDS the previous depth in between -- the honest stand-in for
phase 4, which replaces the hold with a motion warp.

    ./i3d_video.py clip.mp4 --windowed --size 1920x540
    ./i3d_video.py clip.mp4 --monitor DP-2 --every 6 --realtime
    ./i3d_video.py clip.mp4 --start 90 --duration 20 --capture-at 30 out.png

No audio -- this is a video path, not a player.

Keys:  space pause   1/2/3 strength   a anaglyph   s screenshot   ESC/q quit
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from i3d_depth import DepthEngine                       # noqa: E402
from i3d_still import FRAG, INC, INCM, STRENGTH, VERT   # noqa: E402
from i3d_view import contain, pick_monitor              # noqa: E402

SENTINEL = object()


def probe(path):
    """(width, height, fps, duration_s) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    st = j["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 25.0
    dur = float(j.get("format", {}).get("duration") or 0.0)
    return st["width"], st["height"], fps, dur


def _cropdetect_once(path, at, probe_s):
    cmd = ["ffmpeg", "-v", "info", "-nostats"]
    if at:
        cmd += ["-ss", str(at)]
    cmd += ["-i", path, "-t", str(probe_s),
            "-vf", "cropdetect=24:2:0", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    last = None
    for line in err.splitlines():
        i = line.find("crop=")
        if i != -1:
            last = line[i + 5:].strip()
    if not last:
        return None
    try:
        w, h, x, y = (int(v) for v in last.split(":"))
    except ValueError:
        return None
    return w, h, x, y


def detect_crop(path, duration, probes=5, probe_s=1.0):
    """cropdetect at several points -> 'w:h:x:y', or None.

    Films ship with the letterbox BAKED INTO THE FRAME. Those bars are not our
    padding -- the depth model sees black rectangles and invents geometry over
    them, and they waste most of the panel. Cropping them off first is both the
    quality fix and free resolution.

    Probing ONE moment is how you lose half the picture: a night scene or a
    fade is black at the edges and cropdetect happily eats real content. Sample
    across the file and take the UNION -- the largest rect any probe wanted --
    so a dark stretch can only ever under-crop, never cut the frame.
    """
    if duration and duration > 20:
        points = [duration * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)][:probes]
    else:
        points = [0.0]
    rects = [r for r in (_cropdetect_once(path, at, probe_s) for at in points)
             if r]
    if not rects:
        return None
    x0 = min(x for _, _, x, _ in rects)
    y0 = min(y for _, _, _, y in rects)
    x1 = max(x + w for w, _, x, _ in rects)
    y1 = max(y + h for _, h, _, y in rects)
    # even dimensions keep the scaler happy on chroma-subsampled input
    w, h = (x1 - x0) & ~1, (y1 - y0) & ~1
    return "%d:%d:%d:%d" % (w, h, x0, y0)


class Decoder(threading.Thread):
    """ffmpeg -> rgb24 frames at eye resolution, letterboxed by ffmpeg."""

    def __init__(self, path, w, h, fps, start, duration, stretch, out_q, stop,
                 crop=None, hwaccel=False, pix_fmt="rgb24"):
        super().__init__(daemon=True)
        self.w, self.h, self.out_q, self.stop = w, h, out_q, stop
        self.pix_fmt = pix_fmt
        # nv12 is 1.5 bytes/px against rgb24's 3, and skips the CPU colour
        # conversion entirely -- the shader does it. On this box that is the
        # difference between 31 fps and 57 fps on 4K.
        self.frame_bytes = w * h * 3 if pix_fmt == "rgb24" else w * h * 3 // 2

        cmd = ["ffmpeg", "-v", "error"]
        if hwaccel:
            # VAAPI DECODE only. This iGPU has no VAAPI scaler -- scale_vaapi
            # fails with "VAProfile is not supported" -- so any resizing stays
            # on the CPU, which is why callers should decode at native size.
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
        if start:
            cmd += ["-ss", str(start)]           # before -i: keyframe seek
        cmd += ["-i", path]
        if duration:
            cmd += ["-t", str(duration)]

        vf = None
        if (w, h) != (0, 0) and not (stretch and pix_fmt == "nv12"
                                     and w == 0):
            vf = ("scale=%d:%d" % (w, h) if stretch else
                  "scale=%d:%d:force_original_aspect_ratio=decrease,"
                  "pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (w, h, w, h))
        if crop:
            vf = "crop=%s,%s" % (crop, vf) if vf else "crop=%s" % crop
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-f", "rawvideo", "-pix_fmt", pix_fmt, "-an", "-sn", "pipe:1"]
        self.cmd = cmd
        self.proc = None
        self.frames = 0

    def run(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                     bufsize=0)
        nbytes = self.frame_bytes
        buf = bytearray(nbytes)
        view = memoryview(buf)
        try:
            while not self.stop.is_set():
                got = 0
                while got < nbytes:
                    n = self.proc.stdout.readinto(view[got:])
                    if not n:
                        return
                    got += n
                frame = np.frombuffer(bytes(buf), dtype=np.uint8)
                if self.pix_fmt == "rgb24":
                    frame = frame.reshape(self.h, self.w, 3)
                self.frames += 1
                while not self.stop.is_set():
                    try:
                        self.out_q.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            self.out_q.put(SENTINEL)
            self.close()

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


class DepthWorker(threading.Thread):
    """Inference OFF the frame path.

    The obvious design -- a thread that takes each frame, computes depth and
    forwards the pair -- puts a 300 ms inference *inside* the pipeline: the
    renderer receives nothing at all while it runs, then gets a burst. At
    --every 8 that is three dead stops a second, which is exactly what bad
    stuttering looks like.

    So frames never wait here. The renderer reads `depth` whenever it likes and
    always gets the most recent FINISHED map; submissions replace any pending
    job rather than queueing, so the worker is always chewing on the freshest
    frame it can. Depth simply lags the picture -- which is the honest
    behaviour of a hold, and what phase 4's motion warp exists to fix.
    """

    def __init__(self, stop, smooth, threads, pad_rect):
        super().__init__(daemon=True)
        self.stop = stop
        self.smooth, self.threads = smooth, threads
        self.pad_rect = pad_rect          # (x0,y0,x1,y1) content, normalized
        self.cv = threading.Condition()
        self.job = None
        self.depth = None                 # published, read without the lock
        self.infer_ms = []
        self.ready = threading.Event()

    def submit(self, frame):
        with self.cv:
            self.job = frame              # latest wins
            self.cv.notify()

    def run(self):
        eng = DepthEngine(smooth=self.smooth, threads=self.threads)
        while not self.stop.is_set():
            with self.cv:
                while self.job is None and not self.stop.is_set():
                    self.cv.wait(0.2)
                job, self.job = self.job, None
            if job is None:
                continue
            d, dt = eng.infer_norm(job)
            self._blank_pad(d)
            self.depth = d
            self.infer_ms.append(dt * 1000.0)
            self.ready.set()

    def _blank_pad(self, d):
        """Fix the depth the model returned for the letterbox bars.

        ffmpeg pads with black and the model hallucinates over it. Filling the
        bars with a flat 0.5 looks principled -- the bars ARE at the
        convergence plane -- but it puts a depth cliff right at the picture
        edge, and the march tears the content into a ragged silhouette against
        it. Replicating the adjacent content depth outward keeps the field
        continuous; the bars are black, so whatever disparity they carry is
        invisible.
        """
        if self.pad_rect is None:
            return
        x0, y0, x1, y1 = self.pad_rect
        dh, dw = d.shape
        ix0, ix1 = int(round(x0 * dw)), int(round(x1 * dw))
        iy0, iy1 = int(round(y0 * dh)), int(round(y1 * dh))
        if iy0 > 0:
            d[:iy0, :] = d[iy0]
        if iy1 < dh:
            d[iy1:, :] = d[iy1 - 1]
        if ix0 > 0:
            d[:, :ix0] = d[:, ix0:ix0 + 1]
        if ix1 < dw:
            d[:, ix1:] = d[:, ix1 - 1:ix1]


def main():
    ap = argparse.ArgumentParser(description="SpaceWalker 2D->3D, video")
    ap.add_argument("video")
    ap.add_argument("--monitor", help="monitor name (e.g. DP-2) or index")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--size", default="1920x540", help="windowed size WxH")
    ap.add_argument("--strength", default="medium", help="low|medium|high|float")
    ap.add_argument("--every", type=int, default=1,
                    help="submit a frame for depth every Nth frame. Inference "
                         "runs asynchronously, so this caps how often it is "
                         "asked, not how long the picture waits.")
    ap.add_argument("--sync", action="store_true",
                    help="correctness mode: every frame waits for its OWN "
                         "depth. No lag, no frame rate (~3 fps).")
    ap.add_argument("--depth-threads", type=int, default=3,
                    help="TFLite threads (default 3). Measured on this 2-core "
                         "/ 4-thread CPU: 2 threads 497 ms, 3 threads 311 ms, "
                         "4 threads 331 ms -- taking every thread starves "
                         "decode and the render loop and pays for itself.")
    ap.add_argument("--smooth", type=float, default=0.8,
                    help="EMA on the normalization percentiles, 0 = off. "
                         "Stops the scene breathing frame to frame.")
    ap.add_argument("--realtime", action="store_true",
                    help="drop frames to hold wall-clock sync instead of "
                         "playing every frame as fast as possible")
    ap.add_argument("--start", type=float, default=0.0, help="seek, seconds")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds")
    ap.add_argument("--anaglyph", action="store_true")
    ap.add_argument("--stretch", action="store_true")
    ap.add_argument("--crop", default="auto",
                    help="'auto' (cropdetect, the default), 'off', or an "
                         "explicit w:h:x:y. Strips baked-in letterbox bars, "
                         "which otherwise poison the depth map at the edges.")
    ap.add_argument("--capture-at", type=int, metavar="N",
                    help="write frame N and exit (needs a FILE too)")
    ap.add_argument("--capture", metavar="FILE")
    ap.add_argument("--platform", choices=["x11", "wayland", "any"],
                    default="any")
    a = ap.parse_args()

    try:
        msft = STRENGTH[a.strength]
    except KeyError:
        msft = float(a.strength)

    src_w, src_h, fps, dur = probe(a.video)
    print("  video        : %s  %dx%d  %.3f fps  %.1f s"
          % (os.path.basename(a.video), src_w, src_h, fps, dur))

    crop = None
    if a.crop == "auto":
        crop = detect_crop(a.video, dur)
    elif a.crop != "off":
        crop = a.crop
    if crop:
        cw, ch = (int(v) for v in crop.split(":")[:2])
        if (cw, ch) == (src_w, src_h):
            crop = None
            print("  crop         : none needed")
        else:
            print("  crop         : %s  (%dx%d -> %dx%d, %.1f%% of frame "
                  "was baked-in bars)"
                  % (crop, src_w, src_h, cw, ch,
                     100.0 * (1 - (cw * ch) / (src_w * src_h))))
            src_w, src_h = cw, ch

    import glfw
    if a.platform != "any":
        glfw.init_hint(glfw.PLATFORM,
                       {"x11": glfw.PLATFORM_X11,
                        "wayland": glfw.PLATFORM_WAYLAND}[a.platform])
    if not glfw.init():
        sys.exit("glfw init failed")

    mon, _ = pick_monitor(glfw, a.monitor)
    if a.windowed:
        win_w, win_h = (int(v) for v in a.size.lower().split("x"))
        mon = None
    else:
        mon = mon or glfw.get_primary_monitor()
        vm = glfw.get_video_mode(mon)
        win_w, win_h = vm.size.width, vm.size.height

    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    # AUTO_ICONIFY defaults TRUE: a fullscreen window minimises itself as soon
    # as it loses input focus, which a background launch guarantees. It then
    # sits mapped, fullscreen and correctly placed on the glasses -- showing
    # the desktop. Looks exactly like a window-placement bug and is not one.
    glfw.window_hint(glfw.AUTO_ICONIFY, False)
    win = glfw.create_window(win_w, win_h, "i3d video", mon, None)
    if not win:
        glfw.terminate()
        sys.exit("could not create window")
    glfw.show_window(win)
    glfw.restore_window(win)
    glfw.make_context_current(win)
    # vsync stays ON even in realtime: the video clock decides which frames to
    # show, the compositor decides when to show them. Turning it off just adds
    # tearing on top of an already irregular cadence.
    glfw.swap_interval(1)

    import moderngl
    ctx = moderngl.create_context()

    # Size from GL, NOT glfw.get_framebuffer_size(): the Wayland backend
    # multiplies by the output's content scale (3.0 on the glasses), while the
    # actual EGL drawable stays at window size.
    fb_w, fb_h = ctx.screen.size
    eye_w, eye_h = fb_w // 2, fb_h
    print("  window       : %dx%d  (%s)  eye %dx%d" % (
        fb_w, fb_h, "fullscreen %s" % glfw.get_monitor_name(mon).decode()
        if mon else "windowed", eye_w, eye_h))
    if not a.windowed and (fb_w, fb_h) != (3840, 1080):
        print("  note         : not 3840x1080 -- glasses probably still in 2D "
              "(../viture-hw.py 3d on)")

    pad_rect = None if a.stretch else contain(src_w, src_h, eye_w, eye_h)

    stop = threading.Event()
    q_dec = queue.Queue(maxsize=4)
    dec = Decoder(a.video, eye_w, eye_h, fps, a.start, a.duration,
                  a.stretch, q_dec, stop, crop=crop)
    dep = DepthWorker(stop, a.smooth, a.depth_threads, pad_rect)
    dec.start()
    dep.start()
    sync_eng = (DepthEngine(smooth=a.smooth, threads=a.depth_threads)
                if a.sync else None)

    prog = ctx.program(vertex_shader=VERT, fragment_shader=open(FRAG).read())
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1],
                               dtype="f4").tobytes())
    vao = ctx.simple_vertex_array(prog, quad, "aPos")
    prog["sTexture"].value = 0
    prog["sTextureDepth"].value = 1
    prog["inc"].value = INC
    prog["iM"].value = INCM
    prog["uDepthCoe"].value = 1.0

    tex_img = ctx.texture((eye_w, eye_h), 3)
    tex_img.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex_img.repeat_x = tex_img.repeat_y = False
    tex_d = None

    state = {"msft": msft, "anaglyph": a.anaglyph, "paused": False,
             "shot": False, "quit": False}

    def on_key(w, key, sc, action, mods):
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        if key in (glfw.KEY_ESCAPE, glfw.KEY_Q):
            state["quit"] = True
        elif key == glfw.KEY_SPACE:
            state["paused"] = not state["paused"]
        elif key in (glfw.KEY_1, glfw.KEY_2, glfw.KEY_3):
            state["msft"] = STRENGTH[{glfw.KEY_1: "low", glfw.KEY_2: "medium",
                                      glfw.KEY_3: "high"}[key]]
            print("  strength     : %.4f" % state["msft"])
        elif key == glfw.KEY_A:
            state["anaglyph"] = not state["anaglyph"]
        elif key == glfw.KEY_S:
            state["shot"] = True

    glfw.set_key_callback(win, on_key)

    shown = 0
    dropped = 0
    render_ms = []
    t_start = time.time()
    last_report = t_start
    frame = depth = None

    try:
        while not glfw.window_should_close(win) and not state["quit"]:
            if not state["paused"]:
                try:
                    item = q_dec.get(timeout=1.0)
                except queue.Empty:
                    glfw.poll_events()
                    continue
                if item is SENTINEL:
                    break
                frame = item

                if a.realtime:
                    # keep wall-clock sync: skip decoded frames we are late for
                    target = t_start + shown / fps
                    while (time.time() > target + 1.5 / fps
                           and not q_dec.empty()):
                        nxt = q_dec.get_nowait()
                        if nxt is SENTINEL:
                            item = SENTINEL
                            break
                        frame = nxt
                        dropped += 1
                        shown += 1
                        target = t_start + shown / fps
                    if item is SENTINEL:
                        break
                    slack = target - time.time()
                    if slack > 0:
                        time.sleep(slack)

                if sync_eng is not None:
                    # correctness mode: this frame's own depth, no lag, no fps
                    depth, dt = sync_eng.infer_norm(frame)
                    dep._blank_pad(depth)
                    dep.infer_ms.append(dt * 1000.0)
                else:
                    if shown % a.every == 0:
                        dep.submit(frame)
                    if dep.depth is None:
                        # only the very first frame ever waits for depth
                        dep.submit(frame)
                        dep.ready.wait(10.0)
                    depth = dep.depth

                # write the array directly -- .tobytes() copies 6 MB per frame
                # at 1920x1080, which is real time on this CPU
                tex_img.write(frame)
                dh, dw = depth.shape
                if tex_d is None:
                    tex_d = ctx.texture((dw, dh), 1, dtype="f4")
                    tex_d.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    tex_d.repeat_x = tex_d.repeat_y = False
                    prog["uDepthTexel"].value = (1.0 / dw, 1.0 / dh)
                tex_d.write(depth)
                shown += 1

            if frame is None:
                glfw.poll_events()
                continue

            tex_img.use(0)
            tex_d.use(1)
            fbo = ctx.screen
            fbo.use()
            fbo.color_mask = (True, True, True, True)
            ctx.clear(0.0, 0.0, 0.0)

            t0 = time.time()
            if state["anaglyph"]:
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
            render_ms.append((time.time() - t0) * 1000)

            if state["shot"] or (a.capture_at and shown >= a.capture_at
                                 and a.capture):
                from PIL import Image
                buf = ctx.screen.read(components=3)
                shot = np.frombuffer(buf, dtype=np.uint8).reshape(
                    fb_h, fb_w, 3)[::-1]
                out = a.capture or "i3d_video_shot.png"
                Image.fromarray(shot).save(out)
                print("  wrote        : %s  %dx%d (frame %d)"
                      % (out, fb_w, fb_h, shown))
                state["shot"] = False
                if a.capture_at:
                    if not state["anaglyph"]:
                        l, r = shot[:, :eye_w], shot[:, eye_w:]
                        d = np.abs(l.astype(np.int16)
                                   - r.astype(np.int16)).mean()
                        print("  mean L/R diff: %.2f  %s" % (
                            d, "OK - eyes differ" if d > 0.5 else "SUSPECT"))
                    break

            glfw.swap_buffers(win)
            glfw.poll_events()

            now = time.time()
            if now - last_report >= 2.0:
                el = now - t_start
                glfw.set_window_title(win, "i3d  %.1f fps" % (shown / el))
                print("  %6.1fs  %5d frames  %.2f fps  depth %.0f ms  "
                      "stereo %.1f ms  dropped %d" % (
                          el, shown, shown / el,
                          np.mean(dep.infer_ms[-10:]) if dep.infer_ms else 0,
                          np.mean(render_ms[-30:]) if render_ms else 0,
                          dropped))
                last_report = now
    finally:
        stop.set()
        dec.close()
        el = time.time() - t_start
        print("\n  played       : %d frames in %.1f s = %.2f fps"
              % (shown, el, shown / el if el else 0))
        if dep.infer_ms:
            print("  depth        : %d inferences, %.0f ms mean (every %d)"
                  % (len(dep.infer_ms), np.mean(dep.infer_ms), a.every))
        if render_ms:
            print("  stereo       : %.1f ms mean for both eyes at %dx%d"
                  % (np.mean(render_ms), eye_w, eye_h))
        if dropped:
            print("  dropped      : %d frames to hold sync" % dropped)
        glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
