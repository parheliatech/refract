#!/usr/bin/env python3
"""
vr_testpattern -- generate labelled VR test material.

There is no way to eyeball whether a 360 projection is CORRECT: a plausible
looking image can still have yaw inverted, poles flipped, or the eyes swapped,
and you cannot tell from inside the headset. So generate source material whose
every direction is labelled, then assert that the right label lands in the
middle of the view for a known head rotation.

    ./vr_testpattern.py                    # equirect still + fisheye + SBS
    ./vr_testpattern.py --video --seconds 8

Writes into i3d/testpat/.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "testpat")

# yaw (degrees, 0 = front, +90 = right) -> label, colour
DIRS = [(0, "FRONT", (200, 40, 40)), (90, "RIGHT", (40, 160, 60)),
        (180, "BACK", (40, 80, 200)), (270, "LEFT", (200, 160, 40))]


def equirect(w=2048, h=1024, tag=""):
    """Equirect where u=0.5 is FRONT, u=0.75 RIGHT, v=0 UP -- matching
    u = 0.5 + atan2(d.x, -d.z)/2pi,  v = 0.5 - asin(d.y)/pi."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (w, h), (18, 18, 22))
    d = ImageDraw.Draw(img)

    try:
        big = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", h // 12)
        small = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", h // 32)
    except OSError:
        big = small = ImageFont.load_default()

    # 30-degree grid: verticals every 30 deg yaw, horizontals every 30 deg pitch
    for deg in range(0, 360, 30):
        x = (deg / 360.0) * w
        d.line([(x, 0), (x, h)], fill=(70, 70, 80), width=2)
        d.text((x + 6, h * 0.52), "%d" % deg, font=small, fill=(150, 150, 160))
    for pitch in range(-60, 61, 30):
        y = (0.5 - pitch / 180.0) * h
        d.line([(0, y), (w, y)], fill=(70, 70, 80), width=2)

    # direction blocks: yaw 0 sits at u = 0.5
    for yaw, name, col in DIRS:
        u = ((yaw + 180) % 360) / 360.0
        x = u * w
        d.rectangle([x - w * 0.055, h * 0.40, x + w * 0.055, h * 0.60], fill=col)
        d.text((x, h * 0.50), name, font=big, fill=(255, 255, 255),
               anchor="mm")

    d.rectangle([0, 0, w, h * 0.06], fill=(120, 60, 160))
    d.text((w * 0.5, h * 0.03), "UP", font=big, fill=(255, 255, 255),
           anchor="mm")
    d.rectangle([0, h * 0.94, w, h], fill=(60, 130, 140))
    d.text((w * 0.5, h * 0.97), "DOWN", font=big, fill=(255, 255, 255),
           anchor="mm")
    if tag:
        d.text((w * 0.5, h * 0.30), tag, font=big, fill=(255, 255, 0),
               anchor="mm")
    return img


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("ffmpeg failed:\n%s" % r.stderr[-1500:])


def main():
    ap = argparse.ArgumentParser(description="VR test material")
    ap.add_argument("--video", action="store_true", help="also write clips")
    ap.add_argument("--seconds", type=int, default=6)
    ap.add_argument("--width", type=int, default=2048)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    w, h = a.width, a.width // 2

    mono = os.path.join(OUT, "equirect_mono.png")
    equirect(w, h).save(mono)
    print("  wrote  %s  %dx%d  (equirect 360, mono)" % (mono, w, h))

    # stereo: the two eyes carry DIFFERENT labels, so a swapped-eye bug is
    # visible instead of invisible
    le = os.path.join(OUT, "_l.png")
    re_ = os.path.join(OUT, "_r.png")
    equirect(w, h, "LEFT-EYE").save(le)
    equirect(w, h, "RIGHT-EYE").save(re_)

    sbs = os.path.join(OUT, "equirect_sbs.png")
    run(["ffmpeg", "-y", "-v", "error", "-i", le, "-i", re_,
         "-filter_complex", "[0:v][1:v]hstack", sbs])
    print("  wrote  %s  %dx%d  (equirect 360, side-by-side)" % (sbs, w * 2, h))

    tb = os.path.join(OUT, "equirect_tb.png")
    run(["ffmpeg", "-y", "-v", "error", "-i", le, "-i", re_,
         "-filter_complex", "[0:v][1:v]vstack", tb])
    print("  wrote  %s  %dx%d  (equirect 360, top-bottom)" % (tb, w, h * 2))

    # VR180 fisheye, via ffmpeg's own v360 -- an independent implementation of
    # the projection, so agreeing with it is a real check on ours
    # h_fov/v_fov, NOT d_fov: ffmpeg's d_fov is the DIAGONAL field of view, so
    # d_fov=180 does not put a 180 degree circle inscribed in the frame -- it
    # spans the diagonal, and every consumer of the file then has to know that.
    fish = os.path.join(OUT, "fisheye180_sbs.png")
    run(["ffmpeg", "-y", "-v", "error", "-i", sbs,
         "-vf", "v360=input=e:in_stereo=sbs:output=fisheye:out_stereo=sbs:"
                "h_fov=180:v_fov=180:w=%d:h=%d" % (h * 2, h),
         fish])
    print("  wrote  %s  (VR180 fisheye, side-by-side)" % fish)

    eq180 = os.path.join(OUT, "equirect180_sbs.png")
    run(["ffmpeg", "-y", "-v", "error", "-i", sbs,
         "-vf", "v360=input=e:in_stereo=sbs:output=hequirect:out_stereo=sbs:"
                "d_fov=180:w=%d:h=%d" % (h * 2, h),
         eq180])
    print("  wrote  %s  (VR180 equirect, side-by-side)" % eq180)

    for p in (le, re_):
        os.unlink(p)

    if a.video:
        for name, src in (("equirect_sbs", sbs), ("fisheye180_sbs", fish),
                          ("equirect_mono", mono)):
            dst = os.path.join(OUT, name + ".mp4")
            run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", src,
                 "-t", str(a.seconds), "-r", "24", "-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-crf", "20", dst])
            print("  wrote  %s" % dst)


if __name__ == "__main__":
    main()
