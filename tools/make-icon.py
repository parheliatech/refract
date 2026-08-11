#!/usr/bin/env python3
"""Render the Refract icon to PNGs, matching assets/refract.svg.

    .venv/bin/python tools/make-icon.py [--outdir assets/icons]

A prism with white light going in and a spectrum coming out -- the name and
the Parhelia motif in one shape. Drawn here in PIL rather than converted
from the SVG because this machine has no rasteriser (no rsvg-convert,
inkscape or cairosvg), and an installer that depends on one would fail on
someone else's machine too. The geometry below is the same as the SVG's, in
the same 256-unit space, so the two stay in step.
"""

import argparse
import os

from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

GROUND_TOP = (23, 27, 34)
GROUND_BOTTOM = (11, 13, 17)
EDGE = (43, 52, 64)
GLASS_EDGE = (207, 233, 247)

# violet -> red, bent out of the far face
SPECTRUM = [((122, 60, 255), 100, 114), ((47, 107, 255), 114, 128),
            ((0, 200, 224), 128, 142), ((53, 208, 106), 142, 156),
            ((255, 208, 35), 156, 170), ((255, 90, 52), 170, 184)]

PRISM = [(128, 58), (194, 176), (62, 176)]
EXIT = (160, 118)


def render(size, supersample=4):
    """Draw at 4x and downsample: PIL has no antialiased polygons, and at
    32 px a jagged prism looks broken rather than stylised."""
    s = size * supersample
    k = s / 256.0
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    def p(*pts):
        return [(x * k, y * k) for x, y in pts]

    # ground, with a vertical gradient painted as rows
    ground = Image.new("RGBA", (1, 256), (0, 0, 0, 255))
    gd = ImageDraw.Draw(ground)
    for y in range(256):
        t = y / 255.0
        gd.point((0, y), tuple(int(GROUND_TOP[i] + (GROUND_BOTTOM[i]
                                                    - GROUND_TOP[i]) * t)
                               for i in range(3)) + (255,))
    ground = ground.resize((s, s), Image.BILINEAR)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [8 * k, 8 * k, 248 * k, 248 * k], radius=56 * k, fill=255)
    img.paste(ground, (0, 0), mask)
    d.rounded_rectangle([8 * k, 8 * k, 248 * k, 248 * k], radius=56 * k,
                        outline=EDGE + (255,), width=max(1, int(2 * k)))

    # white light in
    d.line(p((10, 110), (101, 110)), fill=(255, 255, 255, 240),
           width=max(1, int(11 * k)))

    # the spectrum
    for rgb, y0, y1 in SPECTRUM:
        d.polygon(p(EXIT, (248, y0), (248, y1)), fill=rgb + (255,))

    # the prism: faint glass body, bright edges
    d.polygon(p(*PRISM), fill=(190, 225, 245, 38))
    d.line(p(*PRISM, PRISM[0]), fill=GLASS_EDGE + (255,),
           width=max(1, int(6 * k)), joint="curve")
    # light catching the near edge
    d.line(p(PRISM[0], PRISM[2]), fill=(255, 255, 255, 200),
           width=max(1, int(3 * k)))

    return img.resize((size, size), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "icons"))
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    for size in SIZES:
        path = os.path.join(a.outdir, "refract-%d.png" % size)
        render(size).save(path)
        print("  %s" % path)
    # a .ico too, for anything that wants one
    render(256).save(os.path.join(a.outdir, "refract.ico"),
                     sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    print("  %s" % os.path.join(a.outdir, "refract.ico"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
