# i3d — 2D→3D conversion, ported to Linux

Real-time conversion of ordinary flat video into stereo 3D for the glasses.
The design notes, the measurements and the Python here are original work;
`DESIGN.md` documents how it was figured out and what it cost.

## Two files are NOT in this repository

`i3d/model/depth_anything_fp16_640_360.tflite` and `i3d/shaders/` were
recovered from VITURE's SpaceWalker Android app while working out how the
feature is built. They are **not redistributed here**: VITURE's Terms of
Service grant only a limited, non-sublicensable licence for personal use and
explicitly forbid decompiling their content or redistributing it (sections
3.1 and 3.2). Studying an application you own to make your own hardware work
is one thing; republishing the vendor's assets is another, and this
repository does not do the second.

So the scripts in this directory will not run out of the box. To use them
you need to supply your own equivalents:

| what | where to get it |
|---|---|
| **depth model** | Any monocular depth model exported to TFLite works. Depth Anything is the obvious choice — see the [upstream repository](https://github.com/LiheYoung/Depth-Anything) and mind its licence, which differs by model size. Point `MODEL` in `i3d_depth.py` at your file. |
| **shaders** | `DESIGN.md` describes what the stereo-synthesis shader does and the pipeline it sits in, in enough detail to write your own. |

The depth model must output a single-channel depth map; note that the one
this was developed against emits **unnormalised** depth (measured range
−0.78 … 2.62), which the pipeline normalises itself.

## Status

Phases 1–3 complete: still images, video, and head-tracked VR playback.
Phases 4–5 (temporal depth, iGPU inference) are the open research track. None
of this is required by the Refract shell — Desk, the HUD and Display Handoff
do not touch it.
