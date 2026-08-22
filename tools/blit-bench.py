#!/usr/bin/env python3
"""Measure what the C fast path actually saves, in isolation.

    .venv/bin/python tools/blit-bench.py [--frames 120] [--size 1920x1080]

Running Desk and reading the fps line does NOT measure this. Two things
mask it: the render loop is vsync-bound at ~52 fps on this hardware, so a
saving below that ceiling is invisible, and on a quiet desktop
`try_pull_sample` mostly returns None -- no frame pulled, no copy, nothing
to save. Both paths then measure the compositor's idleness.

So drive a synthetic source that always has a frame waiting, and time the
one operation that differs: getting a pulled frame into a GL texture.

  python : try_pull_sample -> buf.map -> bytes(info.data) -> tex.write()
  C      : try_pull_sample -> buf.map -> glTexSubImage2D(info.data)

Same source, same texture, same number of frames. The gap is the copy
PyGObject forces and nothing else.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi                                                    # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp                        # noqa: E402,F401

from refract.core import fastblit                            # noqa: E402


def make_pipeline(w, h, frames):
    """A source whose buffers are ALL queued before we time anything.

    This is the part that makes the measurement mean something. Timing a
    drain while the source is still producing measures videotestsrc, not the
    copy: generating a 1920x1080 frame costs more than moving one, so both
    paths come out identical and the difference vanishes into the source's
    noise. Waiting for EOS puts every frame in the appsink's queue first, so
    the timed loop only ever moves pixels that already exist.
    """
    pipe = Gst.parse_launch(
        "videotestsrc pattern=blue num-buffers=%d is-live=false ! "
        "video/x-raw,format=RGBA,width=%d,height=%d ! "
        # max-buffers=0 is "no limit": a queue capped at exactly num-buffers
        # is full at the moment the source wants to finish. wait-on-eos=false
        # matters just as much -- by default appsink holds the EOS until its
        # queue has been drained, so waiting for EOS before draining (which
        # is the whole point of the prefill) deadlocks against itself.
        "appsink name=out max-buffers=0 drop=false sync=false wait-on-eos=false"
        % (frames, w, h))
    pipe.set_state(Gst.State.PLAYING)
    sink = pipe.get_by_name("out")
    sink.get_state(Gst.SECOND * 5)
    bus = pipe.get_bus()
    msg = bus.timed_pop_filtered(
        30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    if msg is None or msg.type != Gst.MessageType.EOS:
        pipe.set_state(Gst.State.NULL)
        raise SystemExit("  source did not fill the queue (%s)"
                         % (msg.type if msg else "timeout"))
    return pipe, sink


def drain_python(sink, tex, frames):
    """What ScreenCapture.latest() + Screen.write() do today."""
    got = 0
    deadline = time.time() + 60
    while got < frames and time.time() < deadline:
        sample = sink.try_pull_sample(0)
        if sample is None:
            continue
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            continue
        try:
            data = bytes(info.data)      # the 8 MB/frame this exists to avoid
        finally:
            buf.unmap(info)
        tex.write(data)
        got += 1
    return got


def drain_c(sink, tex, frames):
    got = 0
    deadline = time.time() + 60
    while got < frames and time.time() < deadline:
        rc, _, _ = fastblit.blit(sink, tex.glo, tex.size[0], tex.size[1])
        if rc == fastblit.OK:
            got += 1
        elif rc < 0:
            raise SystemExit("  fast path returned rc=%d" % rc)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--batch", type=int, default=20,
                    help="frames queued at once (each is w*h*4 bytes of RAM)")
    ap.add_argument("--streams", type=int, default=3,
                    help="report a per-frame cost for this many screens")
    a = ap.parse_args()
    w, h = (int(v) for v in a.size.lower().split("x"))

    Gst.init(None)
    if not fastblit.available():
        sys.exit("  fast path unavailable: %s" % fastblit.why_unavailable())

    # A real GL context, off screen. moderngl needs a current context and
    # glTexSubImage2D needs somewhere to put the pixels.
    import glfw
    if not glfw.init():
        sys.exit("  glfw would not start")
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    win = glfw.create_window(64, 64, "blit-bench", None, None)
    if not win:
        sys.exit("  no GL window")
    glfw.make_context_current(win)
    import moderngl
    ctx = moderngl.create_context()
    tex = ctx.texture((w, h), 4)

    print("\n  %dx%d RGBA, %d frames, %.1f MB per frame\n"
          % (w, h, a.frames, w * h * 4 / 1e6))

    # Queue in batches: every frame is held in memory until drained, and a
    # whole run at once would be gigabytes.
    batch = max(1, min(a.batch, a.frames))
    rounds = max(1, a.frames // batch)

    results = {}
    for name, fn in (("python", drain_python), ("C fast path", drain_c)):
        total, got = 0.0, 0
        for _ in range(rounds):
            pipe, sink = make_pipeline(w, h, batch)
            t0 = time.perf_counter()
            n = fn(sink, tex, batch)
            ctx.finish()                 # do not let the GPU hide behind us
            total += time.perf_counter() - t0
            pipe.set_state(Gst.State.NULL)
            got += n
        if got != batch * rounds:
            print("  %-12s only moved %d/%d frames" % (name, got,
                                                       batch * rounds))
            continue
        results[name] = total / got * 1000.0
        print("  %-12s %6.2f ms/frame   (%d frames in %.2fs)"
              % (name, results[name], got, total))

    if len(results) == 2:
        py, c = results["python"], results["C fast path"]
        print("\n  saved        %6.2f ms/frame  (%.1fx faster)" % (py - c,
                                                                   py / c))
        print("  for %d screens: %.1f ms -> %.1f ms of a frame's budget"
              % (a.streams, py * a.streams, c * a.streams))
        # 60 fps leaves 16.7 ms for EVERYTHING, so state the headroom plainly.
        print("  a 60 fps frame is 16.7 ms\n")

    glfw.destroy_window(win)
    glfw.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
