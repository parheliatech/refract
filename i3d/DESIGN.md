# i3d — SpaceWalker's 2D→3D engine, recovered and ported to Linux

This is the feature that actually distinguishes SpaceWalker: **real-time
conversion of ordinary flat video into stereo 3D**, displayed side-by-side on
the glasses. Nothing else on Linux does this.

Everything needed is recovered from the APK and **all of it is portable**.

## Recovered assets

```
i3d/model/depth_anything_fp16_640_360.tflite   stock TFLite, runs on x86 (verified)
i3d/shaders/fsh/i3d_fragment.glsl              THE stereo synthesis shader
i3d/shaders/csh/*.glsl                         12 compute shaders, plaintext
```

Not obfuscated, not encrypted (`libshader_decryptor.so` exists but these ship in
the clear). `i3d_fragment.glsl` is even fully commented, including references to
VITURE's Windows original and a Rust module
(`depth_normalization.rs::soften_edges_horizontal`).

## The algorithm

Depth-image-based rendering (DIBR). Per eye, per fragment:

1. Sample the depth map, remap to signed disparity around a **convergence plane
   at depth 0.5**:  `dFT = depth * 2.0 - 1.0`
2. **Ray-march** up to 96 steps along x looking for the first crossing where a
   surface projects onto this fragment (`f = -shiftFT - d <= iM * inc`) —
   i.e. the nearest visible surface, which resolves occlusion correctly
3. **Sub-step refinement**: linearly interpolate the exact crossing between the
   last miss and the hit, killing the staircase ripple on silhouettes
4. Re-evaluate with **`increaseEdges()`** — a depth-edge-aware 3-tap horizontal
   softening that preserves boundaries (rising/falling edge detection with
   0.01 / 0.007 thresholds)
5. Sample the source image at `uv.x - shiftFT`

Left eye uses `+msft`, right eye `-msft`. Run twice, pack side-by-side.

### Disparity strength (from `h5/b.java`, `I3DConstants`)

| Preset | msft |
|---|---|
| Low | 0.003 |
| Medium (default) | 0.007 |
| High | 0.010 |

Clamped to `[0.003, 0.010]`; fallback default `0.009`.
Windows reference values: `inc = 1/2000`, `incm = 1.0`.

## The performance trick — depth is NOT computed every frame

Measured on this laptop (i7-7600U, 4 threads, CPU TFLite):

```
input  [1, 352, 640, 3] float32
output [1, 352, 640, 1] float32
218 ms/frame  ->  4.6 fps
```

Far too slow frame-by-frame — which is exactly why SpaceWalker ships the
Qualcomm NPU blobs. But the compute-shader set reveals the real design:
**depth is inferred occasionally and propagated between inferences.**

| Shader | Role |
|---|---|
| `motion_tile_sad_depth.glsl` | tile-based SAD motion estimation |
| `warp_depth.glsl` | warps `uPrevDepth` by `uMotion` → new depth + confidence mask |
| `warp_inverse_depth.glsl` | inverse-depth variant (better for large disparity) |
| `keep_prev_luma.glsl`, `keep_prev_motion.glsl` | temporal history |
| `scene_cut_metrics.glsl` | EMA stats, coverage, `gateFlags` → when to re-infer |
| `compose_depth.glsl` | blend warped + freshly inferred depth |
| `stats_finalize.glsl`, `clear_u32.glsl`, `depth_copy.glsl`, `warp_unpack.glsl` | support |

`warp_depth.glsl` takes `uMinConfidence` and `uMaxDisplacement` and emits a mask
— so low-confidence regions can be refreshed selectively. `scene_cut_metrics`
gates full re-inference on scene changes.

So the budget is: **one inference every N frames + a cheap GPU warp for the
rest.** At 4.6 fps native inference, 24 fps playback needs roughly one real
depth pass every 5 frames, with motion-warped depth in between. Tight but not
absurd — and this is precisely what the pipeline was built to do.

## Why this host is a good fit

```
Mesa 25.2.8, OpenGL 4.6 core, OpenGL ES 3.2, compute shaders supported
Intel HD Graphics 620 (KBL GT2)
```

The compute shaders are `#version 310 es`; your Mesa stack does GLES 3.2
natively, so they run **essentially unmodified**. `i3d_fragment.glsl` is
GLES2-style (`texture2D`, `gl_FragColor`, `varying`) — trivially portable to
desktop GL or usable as-is under GLES.

## ✅ Phase 1 COMPLETE — verified working 2026-08-10

```
$ ./i3d_still.py photo.jpg --debug-depth
  depth        : 640x352  raw range -1.070..2.880  (236 ms)
  normalized   : 0.000..1.000  mean 0.370
  stereo       : msft=0.0070  inc=1/2000
  wrote        : test_sbs.png  3840x1080 (SBS)
  mean L/R diff: 7.59  OK - eyes differ
```

Correctness checks, not just "it ran":

- **Disparity scales with depth** — mean |L−R| is **8.03 in near regions vs
  3.28 in far regions**. That is the defining property of correct DIBR.
- **Depth map is visually correct** — foreground tree and field bright, sky
  dark, mid-distance structure correctly separated.
- **Anaglyph shows depth-varying fringing** — strong separation on near
  objects, near-zero in the sky.

### Measured performance (i7-7600U / HD 620)

| Stage | Cost | Notes |
|---|---|---|
| Depth inference (CPU TFLite, 4 threads) | **236 ms** | the only bottleneck |
| Stereo render, **both eyes @ 1920×1080** | **9.9 ms** | ≈100 fps |
| Stereo render, both eyes @ 960×540 | 3.5 ms | ≈288 fps |

**The march shader is not a problem.** An earlier 170 ms figure was context
creation plus shader compilation, not per-frame cost. At 1080p per eye the
stereo pass costs under 10 ms, leaving the entire real-time budget to depth.

This makes the amortization design the whole ballgame: at 24 fps video, one
inference every ~6 frames is ~236 ms of CPU per 250 ms of video, with the GPU
only ~24% occupied by stereo. Tight, but genuinely feasible — and that is
before moving inference off the CPU.

### What Phase 1 also settled

- The GLES2→GLSL 330 port is faithful (`shaders/port/i3d_fragment_330.glsl`).
- `normalize_depth()` reimplements the missing `depth_normalization.rs`:
  percentile clip (2/98) plus horizontal 3-tap soften. Percentiles matter —
  raw min/max would let outliers crush the usable range.
- SpaceWalker's `[0.003, 0.010]` disparity constants produce sane results
  unmodified; `medium` (0.007) is a good default.

## Phase 2 — on screen, everything but the headset flip

`i3d_view.py` runs the same pipeline in a real GL context and draws straight
into the framebuffer: **no readback**. Each eye is a viewport (`0..W/2`,
`W/2..W`), so at 3840×1080 the window *is* the SBS signal.

Verified windowed on the laptop (960×540 per eye):

```
$ ./i3d_view.py ~/Pictures/bike.jpg --windowed --size 1920x540 --capture out.png
  window       : 1920x540  (windowed)  eye 960x540
  [1/1] bike.jpg  depth 230 ms  total 437 ms
  mean L/R diff: 10.72  OK - eyes differ
  stereo pass  : 8.7 ms for both eyes at 960x540
```

Settled in phase 2:

- **The venv is now durable** (`i3d/.venv`; since merged into the repo-root
  `.venv` in Refract Phase 0 — repro re-verified there, L/R diff 18.16 on
  `bike.jpg`). Phase 1's environment lived in a session scratchpad under
  `/tmp` and would have evaporated.
- **Letterboxing is depth-correct.** The image is contained into the eye with
  black bars; the depth map is padded **at model resolution with 0.5** — the
  convergence plane — so the bars carry zero disparity. Resampling depth to
  1080p would have silently rescaled `increaseEdges()`'s `uDepthTexel` step.
- **Both GLFW backends see the glasses.** The pip `glfw` wheel loads a
  *wayland-only* `libglfw.so` in a Wayland session, so `PLATFORM_X11` is
  unsupported unless `GLFW_LIBRARY` points at the system library. Not a
  problem: the Wayland backend enumerates outputs by connector name and can
  fullscreen on a chosen one, which is all this needs.
- **Mode setting needs D-Bus, not xrandr.** Under Wayland xrandr is read-only,
  so `sbs-display.py` drives `org.gnome.Mutter.DisplayConfig`
  (`GetCurrentState` → swap the target's mode id → `ApplyMonitorsConfig`).
  Defaults to the *temporary* method, which reverts on logout. The glasses are
  identified by EDID (`CVT` / `VITURE`) → `DP-2`.

Remaining for phase 2 is the hardware flip itself — `viture-hw.py 3d on`, then
`sbs-display.py on` to make Mutter drive the 3840×1080 mode it starts
advertising, then `i3d_view.py --monitor DP-2`. That needs someone wearing the
glasses to confirm, and it briefly stops `xr-driver.service` for exclusive USB
access.

## ✅ Phase 3 — video, playing at full frame rate

`i3d_video.py`: `ffmpeg → rgb24 → Depth Anything → i3d_fragment ×2 → SBS`.
Decode runs in a thread, inference in another, GL on the main thread.

```
$ ./i3d_video.py movie.mp4 --windowed --size 1920x540 --every 8 --realtime
  played       : 288 frames in 12.0 s = 24.07 fps
  depth        : 27 inferences, 311 ms mean (every 8)
  stereo       : 7.6 ms mean for both eyes at 960x540
  dropped      : 11 frames to hold sync
```

24.07 fps against a 24.000 fps source — full rate.

### The stutter, and why the obvious design causes it

The first version threaded it the natural way: a worker that takes each frame,
computes depth, forwards the pair. That puts a **300 ms inference inside the
frame path** — the renderer receives *nothing* while it runs, then gets a
burst. At `--every 8` that is three dead stops per second. It measured 20 fps
with 44% of frames dropped, and looked far worse than that number suggests.

Inference is now completely off the frame path (`DepthWorker`). Frames flow
continuously carrying the most recent *finished* depth map; submissions replace
any pending job instead of queueing, so the worker always chews on the freshest
frame available. Depth lags the picture — that is what a hold *is*, and what
phase 4 exists to fix — but the picture itself never waits.

| | fps | dropped |
|---|---|---|
| inference in the frame path | 20.2 | 128 / 288 |
| inference off the frame path | **24.1** | 11 / 240 |

### Also settled in phase 3

- **Thread count is not "use all of them".** On this 2-core / 4-thread CPU:
  2 threads → 497 ms, **3 threads → 311 ms**, 4 threads → 331 ms. Handing every
  thread to TFLite starves decode and the render loop and costs more than it
  buys. Default is 3.
- **GLFW lies about framebuffer size on Wayland.** It returns window size ×
  content scale — **3.0** on the glasses, a 160×100 mm panel — while the EGL
  drawable stays at window size. Every eye viewport came out 3× too large and
  the readback blew up. Both viewers now take the size from GL
  (`ctx.screen.size`). This was latent in phase 2 and only fired once a window
  landed on the glasses.
- **Films carry letterbox baked into the frame.** The depth model invents
  geometry over those black bars. `--crop auto` strips them — but a single
  `cropdetect` probe ate the sides off a dark scene, so it samples 5 points
  across the file and takes the **union**: a dark stretch can only under-crop,
  never cut real content.
- **Per-frame percentile normalization makes video breathe** — the raw→[0,1]
  mapping shifts under the shader as bright objects enter and leave. `--smooth`
  (default 0.8) EMAs the percentile bounds. Free.
- **`tex.write(frame)`, not `tex.write(frame.tobytes())`** — the latter copies
  6 MB per frame at 1920×1080, which is real time on this CPU.
- **Inference moved to `i3d_depth.py`.** Phase 1 rebuilt the TFLite interpreter
  on every call. Phase 1 reproduces bit-identically after the move (same raw
  range, same L/R diff 18.16 on `bike.jpg`).
- **The picture edge wobbles ~5 px** where DIBR exposes the frame border, and
  the two eyes' borders sit ~8 px apart — that gap *is* the stereo shift.
  Inherent to the method, not a defect.

### What phase 3 does not fix

`--every 8` updates depth roughly twice a second while the picture moves at 24.
Anything in motion drags a stale depth map behind it. `--sync` renders each
frame against its own depth for reference, at about 3 fps. The gap between
those two is precisely phase 4.

## VR / 360 playback — a separate path, not a phase

`i3d_vr.py`. Worth being clear about why this is not phase 3.5: **VR footage is
already stereoscopic.** The entire i3d depth pipeline — inference, DIBR,
motion warping — has nothing to do here and is bypassed. The work is
projection plus orientation: one perspective camera per eye, sampling the
source, driven by the glasses' IMU.

| | |
|---|---|
| Projections | equirect 360, equirect 180 (hequirect), fisheye 180 |
| Layouts | mono, side-by-side, over-under, `--swap-eyes` |
| Detection | filename tags, then aspect ratio (360 mono is 2:1, SBS 360 is 4:1, over-under 360 is 1:1, VR180 SBS is two squares) |
| Look | VITURE IMU, or arrows / mouse-drag |

### Verified against ffmpeg's v360, not against opinion

There is no way to eyeball whether a projection is *correct*. A plausible
image can still have yaw inverted, poles flipped or the eyes swapped, and you
cannot tell from inside the headset — you just feel ill. So
`vr_testpattern.py` generates source material where every direction is
labelled and each eye carries a different tag, and the checks are assertions:

```
yaw   0  block rgb (200, 40, 40)  -> FRONT  OK      pitch +85 -> UP band    exact
yaw  90  block rgb (40, 160, 60)  -> RIGHT  OK      pitch -85 -> DOWN band  exact
yaw 180  block rgb (40, 80, 200)  -> BACK   OK      --swap-eyes: exact swap (0.00)
yaw 270  block rgb (200, 160, 40) -> LEFT   OK      eyes differ: mean 10.90

vs ffmpeg v360 (an independent implementation of the same projections):
  equirect360   0.46 / 255
  equirect180   0.28 / 255
  fisheye180    0.26 / 255
```

**Yaw was inverted** on the first run — +90 looked left. Nothing but the
labelled pattern would have caught it before it reached someone's head.

### The FOV trap, twice

`ffmpeg`'s `d_fov` is the **diagonal** field of view. Comparing a 90-degree
horizontal view against `d_fov=90` made all three projections look wrong by a
uniform ~18-25/255 — one systematic mismatch wearing three costumes. Stating
`h_fov`/`v_fov` explicitly dropped equirect to 0.46 and left fisheye genuinely
broken at 30.69, which is how the real bug got isolated.

The second bite: the fisheye *test file* had been generated with `d_fov=180`,
so its circle spanned the frame diagonal rather than being inscribed. Hence
`--fisheye-fov` (default 180) rather than a baked-in constant — real VR180
rigs ship 180, 190 and 200 degree lenses, and guessing looks nearly right in
the centre while warping at the edges, where it is hardest to notice and most
nauseating.

### ✅ Head tracking — four separate wrong assumptions

Each was invisible in the data and only diagnosable by asking the wearer what
their head did versus what the picture did. None of it is documented.

| Symptom | Actual cause |
|---|---|
| View swings wildly | euler is in **DEGREES**, not radians |
| Picture sits at a 45 deg cant | **subtracting euler angles cannot recentre a rotation** — rest pose is pitch ~126 deg, so the decomposition axes are nowhere near the head's |
| Clean three-way axis cycle | IMU body axes are **permuted** relative to the camera |
| One axis reversed alone | SDK quaternion is **LEFT-handed** |

The cant is the important one. Naive per-axis subtraction is not a rotation
operation; composing properly (`R_ref^T * R_current`) is identity at the
reference pose by construction, so the view starts upright however the glasses
happen to be lying. Everything after that was a constant, not a bug.

Guessing the constant cost several rounds — the space is 24 signed
permutations x 2 handedness states, and each candidate costs a relaunch and the
wearer putting the headset on. `--calibrate` solves it instead: three
directional HOLDS (right / up / tilt-right), each with a known target axis,
determine the matrix outright and `det` settles handedness from measurement.
Measured here:

```
up         [ 0.145 -0.989 -0.040]  49.1 deg
right      [ 0.999 -0.048  0.024]  76.6 deg
tiltright  [ 0.224 -0.136 -0.965]  48.1 deg
det -0.946  ->  IMU frame is LEFT-handed
```

Two design lessons paid for the hard way:

- **Oscillating motions cannot determine sign.** "Pan back and forth" records
  whichever direction you were moving when sampled. The first calibration
  collapsed all three axes onto the same vector and looked plausible.
- **Controls need feedback where the eyes are.** Cycling the basis printed its
  state to a terminal the wearer cannot see, and the calibration prompts ran on
  the laptop. Both are useless in a headset. Hence the HUD, and hence `f`
  cycling the four sign states live — conjugating by a 180 deg rotation flips
  exactly TWO axes, so four states cover every arrangement.

Also: auto-recentring on the first IMU sample points the view somewhere
arbitrary, because that sample arrives while the glasses are still in your
hand. There is a countdown now.

### ✅ Real material — hardware decode is the whole game

VR masters are 4K-8K. Measured on this box with a deliberately harsh 4K clip
(~240 Mbps, noise, far above real content):

| path | h264 | hevc |
|---|---|---|
| software decode | 16 fps | 27 fps |
| VAAPI + CPU scale + rgb24 | 31 fps | — |
| **VAAPI -> NV12, no scale** | **57 fps** | **55 fps** |
| full pipeline, fullscreen on the glasses | **50 fps** | |

- **This iGPU decodes but does NOT scale.** `scale_vaapi` fails with
  `VAProfile is not supported`, so any resize lands back on the CPU and gives
  away most of the decode win. Decode at native resolution — the projection
  shader resamples anyway, so scaling was never needed.
- **NV12 beats rgb24 twice over**: 1.5 bytes/px instead of 3, and no CPU colour
  conversion. That is the 31 -> 57 fps step.
- **The colour matrix must come from the file.** Hardcoding BT.709 put a
  visible green bias on BT.601 content (6.32/255 against ffmpeg). Reading the
  tag, and matching swscale's rule that *untagged means BT.601* regardless of
  resolution, brings it to 1.80/255 — the remainder being chroma upsampling on
  a worst-case noise clip.

Still unmeasured: **8K**, which is 4x the pixels of the above and may not hold
realtime, and real fisheye rigs — `--fisheye-fov` defaults to 180 but 190 and
200 degree lenses are common.

### The glasses fall back to 2D behind your back

Stereo "breaking" — both eyes seeing one squeezed frame containing both halves
— is not a software fault: the headset has reverted to 2D and the 3840-wide SBS
signal is being shown as a single 1920 image. Our own IMU handling causes it.
The tracker stops `xr-driver` for exclusive USB and restarts it on exit, and
the driver appears to reset the panel to 2D on start, so every tracked session
leaves the glasses wrong for the next one. `i3d_vr.py` now checks and switches
back before opening its window.

### The one place the depth pipeline could return

**Mono** 360 footage is not stereo, and i3d could synthesize it — depth per
frame, then DIBR inside the projection sampler. That is the only overlap
between the two paths, and it is optional.

## Build plan

| Phase | Work |
|---|---|
| 1 | Still-image proof: PNG → TFLite depth → `i3d_fragment` twice → SBS PNG. No timing pressure, validates the shader port and disparity constants. |
| 2 | GL context + texture plumbing; render SBS 3840×1080 fullscreen on `DP-1` with `viture-hw.py 3d on`. |
| 3 | ✅ done — video via ffmpeg → per-frame stereo, 24 fps with depth held between async inferences. |
| 4 | Port the temporal pipeline: motion tiles → `warp_depth` → `compose_depth`, gated by `scene_cut_metrics`. This is what buys real-time. |
| 5 | Optimize inference: OpenVINO or ONNX Runtime on the HD 620 iGPU instead of CPU TFLite. Biggest single win available. |
| 6 | Head-tracked placement using the working 60–240 Hz IMU feed. |

Phase 1 is self-contained and is the right place to start — if the shader
produces a correct stereo pair from a still image, everything downstream is
plumbing and optimization.

## Honest risks

- **Inference speed is the whole project.** 218 ms CPU is the number to beat;
  everything else is comfortable. If the iGPU path doesn't deliver, this laptop
  may cap out at low-fps or low-resolution playback.
- `depth_anything_fp16_640_360` outputs **unnormalized** depth (measured range
  −0.78 … 2.62 on noise). The shader expects `[0,1]`, so the normalization pass
  the shader comments attribute to `depth_normalization.rs` must be
  reimplemented — that Rust source is *not* in the APK.
- The 12 compute shaders are recovered but their **host-side orchestration is
  not** — pass order, uniform values, tile sizes and gate thresholds have to be
  re-derived experimentally.
