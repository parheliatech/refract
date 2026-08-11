#!/usr/bin/env python3
"""
i3d_still -- SpaceWalker's 2D->3D engine, phase 1: still image -> stereo pair.

Pipeline:
    image -> Depth Anything (TFLite) -> normalize -> i3d_fragment shader x2
          -> side-by-side stereo PNG

Uses SpaceWalker's own depth model and its own DIBR shader (ported GLES2 ->
GLSL 330 core, algorithm unchanged). See DESIGN.md.

    ./i3d_still.py photo.jpg
    ./i3d_still.py photo.jpg -o out.png --strength high
    ./i3d_still.py photo.jpg --width 1920 --height 1080 --debug-depth

Output is 2*width x height, left eye | right eye, ready for the glasses in
SBS mode (../viture-hw.py 3d on).
"""

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# inference and normalization live in i3d_depth so video can keep one
# interpreter alive instead of rebuilding it per frame (see i3d_video.py)
from i3d_depth import MODEL, DepthEngine, normalize_depth   # noqa: E402,F401

FRAG = os.path.join(HERE, "shaders", "port", "i3d_fragment_330.glsl")

# I3DConstants (h5/b.java): low / medium / high, clamped [0.003, 0.010]
STRENGTH = {"low": 0.003, "medium": 0.007, "high": 0.010}
INC = 1.0 / 2000.0      # Windows INCREMENT
INCM = 1.0              # Windows increment multiple

VERT = """
#version 330 core
in vec2 aPos;
out vec2 vTextureCoord;
out vec2 vTextureCoordDepth;
void main() {
    vec2 uv = aPos * 0.5 + 0.5;
    uv.y = 1.0 - uv.y;              // image origin top-left
    vTextureCoord = uv;
    vTextureCoordDepth = uv;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""


def infer_depth(img_rgb):
    """Run Depth Anything. Returns raw float32 depth at model resolution.

    One-shot: builds an interpreter per call, which only makes sense for a
    single still. Video holds a DepthEngine open instead.
    """
    return DepthEngine().infer(img_rgb)


def render_stereo(img_rgb, depth01, out_w, out_h, msft):
    """Run the i3d fragment shader once per eye. Returns (left, right) RGB."""
    import moderngl
    ctx = moderngl.create_standalone_context()
    prog = ctx.program(vertex_shader=VERT,
                       fragment_shader=open(FRAG).read())

    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1],
                               dtype="f4").tobytes())
    vao = ctx.simple_vertex_array(prog, quad, "aPos")

    h, w, _ = img_rgb.shape
    tex_img = ctx.texture((w, h), 3, img_rgb.tobytes())
    tex_img.build_mipmaps()
    tex_img.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex_img.repeat_x = tex_img.repeat_y = False

    dh, dw = depth01.shape
    tex_d = ctx.texture((dw, dh), 1, depth01.tobytes(), dtype="f4")
    tex_d.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex_d.repeat_x = tex_d.repeat_y = False

    tex_img.use(0)
    tex_d.use(1)
    prog["sTexture"].value = 0
    prog["sTextureDepth"].value = 1
    prog["inc"].value = INC
    prog["iM"].value = INCM
    prog["uDepthCoe"].value = 1.0
    prog["uDepthTexel"].value = (1.0 / dw, 1.0 / dh)

    fbo = ctx.simple_framebuffer((out_w, out_h))
    fbo.use()

    eyes = []
    for sign in (+1.0, -1.0):          # left eye = +msft, right = -msft
        prog["mS"].value = sign * msft
        fbo.clear(0.0, 0.0, 0.0, 1.0)
        vao.render(moderngl.TRIANGLE_STRIP)
        buf = fbo.read(components=3)
        eyes.append(np.frombuffer(buf, dtype=np.uint8)
                    .reshape(out_h, out_w, 3)[::-1])   # GL origin is bottom-left
    return eyes[0], eyes[1]


def main():
    ap = argparse.ArgumentParser(description="SpaceWalker 2D->3D, still image")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--strength", default="medium",
                    help="low|medium|high or a float in [0.003,0.010]")
    ap.add_argument("--width", type=int, default=1920, help="per-eye width")
    ap.add_argument("--height", type=int, default=1080, help="per-eye height")
    ap.add_argument("--debug-depth", action="store_true",
                    help="also write the normalized depth map")
    a = ap.parse_args()

    if not os.path.exists(MODEL):
        sys.exit("depth model missing: %s" % MODEL)

    try:
        msft = STRENGTH[a.strength]
    except KeyError:
        try:
            msft = float(a.strength)
        except ValueError:
            sys.exit("--strength must be low|medium|high or a float")
    if not 0.003 <= msft <= 0.010:
        print("  warning: strength %.4f is outside SpaceWalker's [0.003, 0.010]"
              % msft)

    from PIL import Image
    src = Image.open(a.input).convert("RGB")
    print("  input        : %s  %dx%d" % (a.input, src.width, src.height))

    # sample source at output resolution so the shader's UV math is 1:1
    img = np.asarray(src.resize((a.width, a.height), Image.LANCZOS),
                     dtype=np.uint8)

    raw, dt = infer_depth(np.asarray(src, dtype=np.uint8))
    print("  depth        : %dx%d  raw range %.3f..%.3f  (%.0f ms)"
          % (raw.shape[1], raw.shape[0], raw.min(), raw.max(), dt * 1000))

    d01 = normalize_depth(raw)
    print("  normalized   : %.3f..%.3f  mean %.3f"
          % (d01.min(), d01.max(), d01.mean()))

    t0 = time.time()
    left, right = render_stereo(img, d01, a.width, a.height, msft)
    print("  stereo       : msft=%.4f  inc=1/%d  (%.0f ms for both eyes)"
          % (msft, int(1 / INC), (time.time() - t0) * 1000))

    sbs = np.concatenate([left, right], axis=1)
    out = a.output or (os.path.splitext(a.input)[0] + "_sbs.png")
    Image.fromarray(sbs).save(out)
    print("  wrote        : %s  %dx%d (SBS)" % (out, sbs.shape[1], sbs.shape[0]))

    if a.debug_depth:
        dpath = os.path.splitext(out)[0] + "_depth.png"
        Image.fromarray((d01 * 255).astype(np.uint8)).save(dpath)
        print("  wrote        : %s" % dpath)

    # quick sanity: the two eyes must actually differ
    diff = np.abs(left.astype(np.int16) - right.astype(np.int16)).mean()
    print("  mean L/R diff: %.2f  %s" % (
        diff, "OK - eyes differ" if diff > 0.5
        else "SUSPECT - eyes nearly identical, stereo may not be applied"))


if __name__ == "__main__":
    main()
