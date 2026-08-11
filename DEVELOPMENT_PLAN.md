# Refract — Development Plan

> **Status snapshot (2026-08-11):** **Phases 0–4 complete and wearer-tested.**
> `python -m refract` boots into the home screen; three head bobs open the HUD
> over any scene; **Refract Desk runs the real three-monitor topology** — flat,
> view-filling screens with a live mirror of the laptop panel between two
> Mutter virtual monitors, the pointer crossing them in the order they appear,
> and windows draggable between them. **167 automated checks**
> (65 pure-math, 17 home, 51 HUD, 34 Desk) plus on-glasses captures.
>
> A wearer session on 2026-08-11 drove a lot of change; the big one was that
> **head tracking was broken by a quaternion decode bug**, not by anything
> calibration could fix — see "The head-tracking bug" below before touching
> IMU code. Also settled that session: the HUD key combos are unreachable
> (GNOME eats them), so the way in is a head gesture; Desk fills the view at
> 1:1 texel-to-pixel because anything smaller is unreadable; and the desktop's
> monitor layout must be rearranged or dragged windows land in front of your
> eyes.
>
> **Next: Phase 5, Display Handoff** — and it starts from further along than
> planned: a graceful quit already restores 2D, the monitor layout, the
> virtual monitors and the config. Still open from the user: the go-ahead to
> delete `xrdesk.py` now that Desk has replaced it.

This document is written to be executed phase-by-phase by a model working
with limited context. Read it top to bottom once before doing anything;
follow the Working Rules exactly; do not skip acceptance checks.

## Project Overview

Refract is the parent application/shell for the VITURE Pro XR glasses on
Linux — a more usable replacement for Breezy Desktop. One persistent home
layer hosts named **sub-experiences**: **Refract Desk** (virtual desktop),
**Refract 360** (360°/spatial video), **Refract Play** (games hub),
**Refract TAK** (3D tactical map, planned separately in `~/Vibe/VRTAK-Plan`).
Visual language: the **Parhelia** design guide (as used in `~/Vibe/AeroScan`)
plus a refraction/prism/glass motif.

The two principles that drive every structural decision, both born from
Breezy's failures:

1. **Shallow, task-first root.** The root screen is launcher tiles only.
   Configuration lives exclusively in the HUD (per-container settings + one
   Global Settings dialog), reachable in ≤2 steps from anywhere. No toggle
   ever gets added to the root.
2. **Display Handoff is a first-class feature.** Glasses ↔ physical screen
   switching gets its own settings section, its own hotkey, and its own tests.

Refract is deliberately independent of Breezy Desktop and `xr-driver` — the
glasses are driven directly through the vendor SDKs (`sdk/` and
`libglasses.so`).

---

## Working Rules (read before every phase)

### How to run things

- Work from `/home/kendel/Vibe/Refract`. Always use the repo venv:
  `.venv/bin/python`. Never `pip install` into the system Python.
- App entry point: `.venv/bin/python -m refract` (flags: `--test-card`,
  `--windowed`, `--sim YAW,PITCH,ROLL`, `--capture FILE --capture-after N`,
  `--no-imu`, `--no-sbs`, `--monitor`, `--recenter-after N`).
- The glasses output is **DP-2** (EDID vendor `CVT`, product `VITURE`); the
  laptop panel is **eDP-1**. Check current modes with
  `.venv/bin/python -c "from refract.core import displaymode; print(displaymode.list_outputs())"`.

### Verification discipline (non-negotiable)

- **A clean exit is not proof that rendering is correct.** Every visual
  change must be verified by rendering with `--capture out.png
  --capture-after N`, then actually reading/viewing the PNG. Phase 1 caught a
  bug this way that a clean exit and a plausible windowed glance both missed.
- Prefer `--windowed --sim 15,5,0` for geometry checks (no hardware needed);
  use a fullscreen on-glasses capture for the phase gate.
- Anything that only a wearer can judge (comfort, sign conventions, key
  reachability, handoff feel) is **not yours to sign off** — implement, run
  the automated checks, then stop and ask the user to wear the glasses.

### Hard invariants (violating any of these is a regression)

1. **Never run two glasses SDK clients at once.** `refract.core.viture_sdk`
   (libviture_one_sdk) and `refract.core.hardware` (libglasses) both claim
   the same exclusive USB device. Two clients racing caused an observed
   segfault in `VitureDeviceProvider::ImuReadThread`. While the App's `Head`
   is running, do NOT construct `hardware.Glasses` (see Phase 3/5 for the
   sanctioned approach). Also: the external `xrDriver` process counts as a
   client — check `hardware.driver_running()` and use
   `hardware.driver_paused()` before touching the device.
1b. **Never re-derive the IMU byte layout.** The quaternion is W,X,Y,Z on the
   wire (offset 20 is the SCALAR); `viture_sdk.parse_imu()` is the single
   place that knows this and it is regression-tested. Getting it wrong is
   SILENT — valid-looking rotations, wrong motion — and no calibration can
   compensate. It cost this project days.
1c. **Never add calibration to fix head tracking.** The IMU's orientation
   relative to the optics is a hardware constant and VITURE already corrects
   for it: the euler angles are in the glasses' frame. If tracking is wrong,
   the decode or a sign constant is wrong. Wildly varying calibration results
   are a symptom of a decode bug, not of a headset that moved.
1d. **Never assume the app has keyboard focus.** A fullscreen window on the
   glasses output does not hold focus while the wearer works on the laptop,
   and GNOME swallows modifier combos before GLFW sees them. Anything that
   must be reachable needs a path that does not depend on focus: the head
   gesture, a bare key, or the control file.
2. **Never "simplify" or "harmonise" the IMU math** in `refract/core/head.py`
   or `i3d/i3d_vr.py`. They intentionally use OPPOSITE axis mappings (camera
   vs sampling ray). The long comments in head.py are load-bearing knowledge
   paid for in wearer sessions — keep them intact when editing around them.
3. **The root/home screen shows launcher tiles only.** Every settings
   control belongs in the HUD (Phase 3) or a container's settings panel.
   Refuse the temptation; it is the product's core principle.
4. **Processes that touched either SDK must end with
   `App.hard_exit()` / `os._exit()`**, never a normal interpreter exit —
   both SDKs leave threads that never join (public SDK `deinit()` hangs).
5. **xrandr is READ-ONLY under Wayland.** Mode changes go through
   `refract.core.displaymode` (Mutter DisplayConfig D-Bus). Keep the default
   method TEMPORARY (reverts on logout) — that is deliberate crash-safety.
6. **`--windowed` must keep working for every scene** — it is the only way
   to develop without wearing the glasses.
7. **Trust `rc=0` from dimension switches, not the readback.** The glasses
   re-enumerate on an SBS mode change; the immediate readback times out
   (-3). Verify via `displaymode.is_sbs("DP-2")` / `wait_for_mode()`.
8. **Don't touch:** `sdk/` (vendor SDK), `RE-FINDINGS.md`, `i3d/DESIGN.md`
   history sections, `xrdesk.py` and `virtual-monitors.py` (until the Phase 4
   port reaches parity and the USER confirms), the root `vdisplay.py` shim
   (until xrdesk is deleted). "SpaceWalker" references in RE docs are
   provenance (the actual vendor APK) — do not rename them.
9. New code goes under `refract/`; temporary/scratch files go in the session
   scratchpad, never the repo root.
10. **Feedback where the eyes are:** every interactive state change must
    surface in the headset (status overlay / HUD), never only on the
    terminal. The wearer cannot see the terminal.

### Known traps (each of these has already burned a session)

| Trap | Rule |
|---|---|
| GLFW framebuffer size lies under Wayland (window × content scale, ×3.0 on the glasses) | take size from `ctx.screen.size` — `App` already does |
| IMU floats are big-endian | `viture_sdk.be_float` handles it; don't re-parse |
| Recentring on the first IMU sample | it arrives while the glasses are in your hand; `App` runs a countdown — don't bypass it |
| libglasses floods stdout with `[I][libglasses]` logs | fd-redirect around hardware calls inside the shell (Phase 5 work item) |
| `pkill -f` matches the launching shell too | use the pid file + control file pattern (xrdesk `control()`), not bare pkill |
| Content placed by rotating about the world origin ORBITS out of view | pivot about the content's own centre (see `testcard.py`) |
| PIL `frame.tobytes()` copies 6 MB/frame | write numpy arrays to textures directly |
| GNOME swallows modifier combos before GLFW sees them | confirmed on a head for BOTH Ctrl+Super+R and Ctrl+Alt+R; use the head gesture, a bare key, or the control file |
| Rearranging monitors kills a `RecordMonitor` stream | arrange first, start mirrors after; restart them on any later re-arrange |
| Mutter rejects layouts with gaps | "not adjacent"; move an output to another row, not to a distant column |
| `cursor-mode` 2 is METADATA, not composited | only mode 1 (EMBEDDED) draws the pointer into the frame |
| A static virtual monitor stops producing buffers | "no sample pending" is not a fault; count frames written instead |
| A second Refract silently eats your control-file commands | `already_running()` guards it; check for a stray session before believing a test result |
| Persistent status text never clears itself | give `set_lines()` a `ttl`, or it hangs in the view forever |

### When to stop and ask the user

- Before deleting any file you did not create in the current phase.
- Before any wearer-required acceptance step (say exactly what to run and
  what to look for).
- Before changing display modes or stopping `xr-driver` **if**
  `hardware.driver_running()` is True (it is normally False on this machine).
- If the two-SDK coexistence experiment (Phase 3) is needed: it may crash;
  ask first, run it in a throwaway subprocess.

---

## The head-tracking bug (found 2026-08-11) — read this before touching IMU code

Symptom reported by the wearer: *"tilting my head up results in down motion,
and roll is induced when I make changes in pitch."*

Cause: **the quaternion component order was wrong on arrival.** The wire
layout, per `sdk/sample/src/main.c`, is

```c
quaternionW = makeFloat(data + 20);   quaternionX = makeFloat(data + 24);
quaternionY = makeFloat(data + 28);   quaternionZ = makeFloat(data + 32);
```

W first. The code read those four floats straight into `(x, y, z, w)`, so the
scalar landed in a vector slot. A scrambled quaternion still normalises to a
unit quaternion and still yields a valid rotation matrix — it just describes
the wrong rotation. Measured: a 50° rotation about X, read the old way,
becomes **180° about a degenerate axis**. Nothing ever crashed.

Three consequences worth internalising:

1. **No calibration could ever fix it.** The three-hold ritual was solving for
   a mount rotation against data that had no consistent mapping, which is why
   it produced a different answer every run (`+0.462`, `+0.301`, `-0.040` for
   the same axis) and why "just calibrate again" never converged.
2. **The wearer was right that no calibration should be needed.** The IMU's
   orientation relative to the optics is a hardware constant, and VITURE
   already corrects for it — `viture.h` reports eulerRoll/eulerPitch/eulerYaw
   in the GLASSES' frame. `Head(mode="euler")` (now the default) uses those
   directly: no basis, no handedness argument, nothing to calibrate.
3. **Stored calibrations from before the fix are poison** and are rejected by
   version (`Head.SETTINGS_VERSION`), not silently reused.

Guardrails now in place: `viture_sdk.parse_imu()` is a pure function so the
byte layout is regression-tested without hardware (`tests/selftest.py`,
"imu wire format"), and `python -m refract --log-axis` prints the axis each
movement actually turns about, flagging `MIXED` when pitch bleeds into roll.

Settled on a head after the decode fix (2026-08-11): yaw and roll came out
correct, pitch came out inverted. So **the vendor reports pitch positive
nose-DOWN** — `EULER_SIGNS = (1.0, -1.0, 1.0)`. That is one hardware
constant, not a per-session calibration, and it is pinned by
`tests/selftest.py` ("head conventions").

Also from the same session: **Desk defaults to yaw-only** (`desk.yaw_only`).
Humans are jittery, and micro-pitch and micro-roll ride on top of every
glance and make text swim; yaw is the axis you actually navigate a row of
monitors with. `yaw_only()` keeps the heading and discards the other two.
Toggle it off in the HUD for a fully world-locked desk. Watch the sign when
touching it: `head_yaw_deg()` reports yaw in the OPPOSITE sense to `rot_y`
(inherited from xrdesk's follow code), so rebuilding a rotation from it
needs a negation — a bug the test caught before it reached a headset.

If the euler constants ever need re-deriving:
`tools/imu-probe.py` records the glasses through known poses (held in the
hand, watching the terminal — not worn) and `tools/imu-solve.py` searches all
48 order/sign combinations against that recording. The solver is round-trip
validated: it recovered a hidden convention to 0.1° with a 49° margin.

## Architecture Decisions (settled — do not reopen)

- **A1. One process, scenes not processes.** USB exclusivity + `deinit()`
  hang make multi-process unworkable. Sub-experiences implement the `Scene`
  interface in `refract/core/render.py` and live on `App`'s scene stack. The
  HUD is an overlay pass, not a scene.
- **A2. Desk's side monitors are Mutter virtual monitors**
  (`refract/core/vdisplay.py` — real extended displays; pointer, windows,
  clipboard all come free). Center monitor = ScreenCast mirror of eDP-1.
- **A3. Python + moderngl/GLFW stack stays.** Stereo pass measured ~10 ms at
  1080p/eye. No rewrites without a measured limit.
- **A4. One config file:** `~/.config/refract/config.json`, sections
  `global` / `desk` / `three60` / `play` / `tak` (`refract/core/config.py`;
  legacy `~/.config/xrdesk.json` auto-imports into `desk`). IMU calibration
  lives in `global.imu` (boot falls back to `desk.imu` from migration).
- **A5. HUD toggle key: proposal `Ctrl+Super+R`,** configurable, stored in
  `global.hud_key`. Super combos may be compositor-captured (see traps) —
  first wearer test of Phase 3 must confirm or pick the fallback
  (`Ctrl+Alt+R`). Avoid `Ctrl+Super+Space` (Breezy's recenter).

## Code Inventory (what exists and is verified, as of Phase 1)

```
refract/__main__.py        boot: IMU first, then SBS, then window (order is
                           load-bearing: SBS re-enumerates the display)
refract/core/viture_sdk.py public SDK binding; parse_imu() OWNS the byte
                           layout and is regression-tested -- see the
                           head-tracking bug section
refract/core/gesture.py    HeadBob: three nods open the HUD; designed around
                           not firing while someone reads
refract/core/head.py       Head (IMU→camera matrix), calibration solve
                           (synthetic-tested), CalibrationSession state machine
refract/core/hardware.py   libglasses binding; info() VERIFIED on device;
                           driver_paused() context manager
refract/core/displaymode.py Mutter DisplayConfig; list/find/apply/is_sbs
                           VERIFIED live (glasses = DP-2)
refract/core/render.py     App (window, GL, scene stack, per-eye loop,
                           recenter countdown, SIGUSR1, capture),
                           Scene base, Overlay, WorldScreen, text_panel,
                           panel_image; VERIFIED windowed + on-glasses
refract/core/vdisplay.py   Mutter virtual monitors (from Phase 0; used
                           daily by xrdesk.py)
refract/core/config.py     config load/save + xrdesk.json migration
refract/shell/testcard.py  Phase 1 gate scene (keep; it is the renderer's
                           regression test)
refract/shell/registry.py  SubExperience registry -- the launcher and (Phase
                           3) the HUD quick-switch are BUILT FROM THIS LIST
refract/shell/home.py      HomeScene + pure-numpy pointer projection/hit test
refract/shell/coming.py    ComingSoonScene placeholder for unported entries
refract/core/settings.py   Setting schema + get/adjust/activate/apply_all;
                           scenes return one from settings_schema()
refract/shell/hud.py       Hud overlay owned by App (combo, quick-switch,
                           container settings, Global Settings page)
refract/shell/globalsettings.py  the one Global Settings schema
refract/shell/calibrate.py CalibrationScene: prompts on the glasses
refract/desk/scene.py      DeskScene: 3 screens, mirror centre, follow,
                           layout persistence, settings schema
refract/desk/layout.py     pure planner that lines the desktop's logical
                           monitor order up with the 3D arrangement
refract/core/vdisplay.py   ScreenCapture(specs): virtual monitors AND
                           mirrors of physical outputs in one session
tools/imu-probe.py         record the IMU through known poses, held in the
tools/imu-solve.py         hand; solve the euler convention from it. Only
                           needed if the constants ever come into doubt.
tests/run.py               THE test entry point: run.py [--all|--only|--list]
tests/selftest.py          pure math, no display: head solve + pointer hit
                           test + desk monitor arrangement (34 checks)
tests/smoke_home.py        home navigation, needs a display not the glasses:
                           keyboard/pointer/launch/return (17 checks)
tests/smoke_hud.py         HUD: combo, quick-switch, live settings (36)
tests/smoke_desk.py        Desk: real monitors, mirror, layout (23)
xrdesk.py                  UNTOUCHED proto-Desk; delete only after the
                           wearer confirms parity (Phase 4 step 7)
i3d/                       UNTOUCHED proto-360 + 2D→3D research
viture-ctl.py, viture-hw.py, viture-probe.py, sbs-display.py
                           standalone CLIs; keep runnable
```

**Run the tests with one command:**

```
.venv/bin/python tests/run.py          # quick set, ~9 s  -> 133 checks
.venv/bin/python tests/run.py --all    # + desk, ~27 s    -> 167 checks
.venv/bin/python tests/run.py --list   # what each suite covers
.venv/bin/python tests/run.py --only hud [-v]
```

Each suite runs as a subprocess (only one GL context can be current per
process, so two window-driving suites in one interpreter cannot both work).
Display-requiring suites are skipped with a note when there is no session,
so the quick set is safe over SSH. Non-zero exit if anything fails, with the
failing suite's output tail printed.

`desk` is excluded by default because it makes two real monitors appear and
disappear on the desktop; pass `--all` when that is acceptable.

Regression anchors (rerun when you touch anything they cover):

- `tests/selftest.py` — **65 checks**, no display, no glasses: **the IMU wire
  format** (the byte layout that caused the head-tracking bug), head solve and
  handedness, pitch direction, yaw-only steadying, the head-bob gesture
  (mostly that it does NOT fire), pointer projection/hit test, desk carousel
  and the monitor-arrangement planner.
- `tests/smoke_home.py` — **17 checks**: launcher keys, pointer hover/click,
  launch/return. One PNG per step in `--outdir`.
- `tests/smoke_hud.py` — **51 checks**: bindings, input stealing while the
  scene keeps running, quick-switch, settings applying live/persisting/
  clamping, every row reachable and drawn (they used to overflow), and quit
  requiring two deliberate choices.
- `tests/smoke_desk.py` — **34 checks**: real monitors, mirror pixels, the
  pointer being drawn INTO the frame, the mirror surviving a desktop
  rearrange, fill mode (a centred screen must land on NDC ±1), flat default,
  layout maths, control words; asserts no virtual monitors are left behind.
  Refuses to run if another Refract owns the monitors, and says so.
- Renderer: `.venv/bin/python -m refract --test-card --windowed --sim 15,5,0
  --capture /tmp/tc.png --capture-after 2` → PNG shows two eyes, card
  labelled TL/TR/BL/BR correctly, ~50 fps in the exit log.
- Shell: `.venv/bin/python -m refract --windowed --sim 0,0,0 --capture
  /tmp/home.png --capture-after 2` → four tiles, wordmark, status bar.
- i3d: `.venv/bin/python i3d/i3d_still.py ~/Pictures/bike.jpg` → mean L/R
  diff exactly **18.16**.

---

## Phases

### Phase 0 — Rename and restructure  ✅ COMPLETE 2026-08-10

Repo renamed `SpaceWalker`→`Refract`; `refract/` package skeleton;
`vdisplay.py` moved into core with root shim; venvs merged (i3d repro
bit-identical); config migration written. `python -m refract --version`
works; all legacy entry points still run.

### Phase 1 — Core runtime  ✅ COMPLETE 2026-08-10

Extracted head/hardware/displaymode/render into `refract/core/` with
behaviour and comments preserved; test-card gate passed windowed AND
fullscreen on glasses (captured frames inspected; ~58 fps, live IMU).
Notes learned: libglasses stdout noise (silence it in Phase 5);
`get_film_mode` errors -4 (film stays parked); orbit-vs-pivot trap.

### Phase 2 — Shell: root launcher + status overlay  ✅ COMPLETE 2026-08-11
*(automated checks pass; wearer sign-off still outstanding — see Acceptance)*

Goal: `python -m refract` boots into the Refract home screen.

Steps, in order:

1. `refract/shell/registry.py` — the sub-experience registry. A dataclass
   (`name`, `title`, `subtitle`, `accent_rgb`, `available: bool`,
   `scene_factory: callable | None`) and `REGISTRY` list with entries:
   Desk, 360, Play (available=False), TAK (available=False). Desk and 360
   are also unavailable until their ports land — point their factories at a
   `ComingSoonScene` (a WorldScreen showing the panel + "ports in Phase N")
   so tile→launch→Esc→home is exercisable NOW.
2. `refract/shell/home.py` — `HomeScene(Scene)`. Tiles are `WorldScreen`
   panels (use `panel_image`, `alpha=True`) arranged on the cylinder at
   distance ~1.3 m, yaw-spaced like xrdesk's `rebuild()` does for monitors.
   Root shows tiles + wordmark ONLY (invariant 3).
3. Keyboard navigation: Left/Right move focus, Enter launches
   (`app.push(entry.scene_factory())`), Esc at home = quit. Focused tile
   re-renders its texture with `focused=True` (cache both textures at
   enter(); do not regenerate per frame).
4. Mouse navigation: track `glfw.set_cursor_pos_callback` +
   `set_mouse_button_callback` on `App` (add thin hooks in `App` that
   forward to the scene like `_on_key` does). Hit test: project each tile's
   centre through `app.mvp(eye=0)`, take NDC x/y, focus the nearest tile
   within a radius; click = launch. Draw a small cursor dot via `Overlay`
   at the pointer NDC. (Head-gaze pointing is explicitly out of scope.)
5. Status overlay (extend `App.status` usage in HomeScene.update): clock,
   glasses present (`hardware.find_pid()` — cache it, refresh every ~5 s,
   it reads sysfs), SBS state (`displaymode.is_sbs("DP-2")` — cache ~5 s;
   it shells out to xrandr, never call per frame). No brightness in the bar
   yet (needs the SDK-coexistence answer from Phase 3).
6. Wire `__main__.py`: no args → boot HomeScene (same boot order: IMU →
   SBS → window). Keep `--test-card` working.

Acceptance (automated) — **done**: `tests/selftest.py` (pointer math, no
display) and `tests/smoke_home.py` (drives keyboard/pointer/launch/return
frame by frame against a real window) both pass; windowed and on-glasses
captures inspected.
Acceptance (wearer) — **OUTSTANDING**: ask the user to run
`.venv/bin/python -m refract`, then confirm on the glasses that (a) arrow
keys move the highlight, (b) the mouse pointer lands where it looks like it
does — pointer aiming is the one thing the maths cannot prove — (c) Enter
on Desk/360 opens the placeholder and Esc returns home, (d) tile size and
distance are comfortable. Do NOT mark this signed off without their word.

**Done when:** boot lands in the shell, tiles launch a scene and Esc
returns, wearer confirms mouse + keyboard navigation both work on the
glasses.

*Phase 2 notes (learned during the build):*

- **The screen shader hardcoded `alpha = 1.0`**, so `WorldScreen(alpha=True)`
  enabled blending but never blended — transparent panel corners drew as
  black boxes. Fixed with a `uUseAlpha` uniform. **This matters for Phase 4:**
  desktop capture arrives as BGRx whose alpha is often 0, so opaque screens
  must keep forcing alpha to 1. Only UI panels opt in.
- Alpha panels now also clear `depth_mask` while drawing, or their
  transparent corners write depth and punch holes in later geometry.
- **Pointer hit testing projects all four corners of a tile**, not a
  half-width from one edge. The one-sided version was biased off-axis
  (measured 0.181/0.158/0.163/0.202 for four symmetric tiles) because a
  rectilinear projection stretches distance from the view axis non-linearly.
  Two consequences worth keeping in mind for the HUD: outer tiles project
  **wider**, not narrower; and the axis-aligned box of the projected
  trapezoid sits slightly outward of the projected centre (measured skew
  ≤ 0.0105 NDC) — it still contains the centre, which is all hit testing
  needs.
- `App` gained `render_frame()` / `grab()` (so tests can step frames) and
  cursor/mouse callbacks that forward to `Scene.on_cursor` / `on_mouse`.
- **Status-bar reads are cached for 5 s** (`StatusProbe`): `find_pid()` walks
  sysfs and the SBS check is a D-Bus round trip. Neither may run per frame —
  the status bar is the classic place a per-frame syscall hides.
- Tile accents come from the real Parhelia palette (teal / amber / magenta /
  radar green), brightened for a black field seen through tinted optics;
  unavailable entries are desaturated by `panel_image(available=False)`.

### Phase 3 — HUD navigator + settings  ✅ COMPLETE 2026-08-11
*(automated checks pass; wearer sign-off + step 5 experiment outstanding)*

Goal: the configurable key combo opens the HUD over ANY scene: quick-switch
row, per-container settings, Global Settings. The scene behind keeps
rendering (it only loses input focus).

Steps:

1. `refract/core/settings.py` — settings schema. A schema is a list of
   dicts: `{key, label, kind: "bool"|"int"|"float"|"enum"|"action",
   min, max, step, options, value_from_config, on_change}`. Scenes return
   theirs from `settings_schema()`; Global Settings is a schema owned by
   the shell. Writing a value updates `config` (correct section) and calls
   `on_change` immediately — settings apply live, no OK button.
2. `refract/shell/hud.py` — `Hud` overlay component owned by `App` (NOT a
   scene): `open`, `close`, `toggle`, `on_key`, `render_eye`. When open,
   `App._on_key` routes keys to the HUD first. Render as `Overlay`-style
   NDC panels (screen-space, head-locked) — build the world-locked variant
   only if the user asks after wearing it (record A5 result then).
   Rows: [quick-switch: one entry per REGISTRY item] [container settings of
   the active scene] [Global Settings]. Selection: arrows + Enter, mouse
   hover + click (reuse Phase 2 hit-test approach in NDC space).
3. Key combo: read from `global.hud_key` (default `"ctrl+super+r"`), parse
   into GLFW mods+key, detect in `App._on_key` BEFORE scene routing.
   First wearer test must confirm GNOME doesn't swallow it fullscreen
   (traps table); fallback default `ctrl+alt+r`.
4. Global Settings v1 content: IMU rate (enum 60/90/120/240 →
   `head.v.lib.set_imu_fq`), recenter-after (float), HUD key (display
   only + note to edit config), calibrate (action → push a
   CalibrationScene wrapping `CalibrationSession`), Display Handoff
   (placeholder section, text "Phase 5"). Brightness/volume: ONLY if the
   coexistence experiment (below) says it's safe; otherwise show current
   values as read-only text captured at boot, with a note.
5. **Coexistence experiment — ✅ RUN 2026-08-11 (authorised by the user).**
   Result: **outcome (b) — leave brightness/volume read-only.** Measured,
   with the IMU streaming and `xrDriver` stopped:

   ```
   [2] IMU streaming: 225 samples
   [4] libglasses initialize() SUCCEEDED
   [E] Failed to send USB command - MsgID: 0x0005, error code: -1
   [5] read: brightness=-3 volume=-3        (-3 = TIMEOUT)
   [6] write brightness -> rc=-3
   [8] IMU after: 848 samples (delta 623) -> STILL ALIVE
   ```

   Three things worth keeping:
   - **No segfault, and the IMU was undisturbed** — the second client never
     completed any I/O, so it never raced. Do NOT read this as "two SDK
     clients are safe": the documented `ImuReadThread` segfault came from
     clients that both got through. Invariant 1 stands.
   - **`initialize()` returns SUCCESS on a device it cannot talk to.** Never
     treat a successful init as proof the device is usable; check the
     per-command return codes.
   - The device needed no recovery afterwards (IMU, USB and SBS all fine).

   Consequence: the only remaining route to brightness/volume/film is
   **`mcu_with_rsp`** on the client that already owns the device (the public
   SDK's undocumented command+response escape hatch — see README "Two SDKs
   exist"). Scoped into Phase 5, which already has to touch hardware. The
   MCU message ids are in `RE-FINDINGS.md`; `viture-hw.py` remains the
   working standalone path when Refract is not running.
6. Quick-switch behaviour: selecting an available entry does
   `app.switch(factory())` (exit current scene, enter new); unavailable
   entries render greyed and unselectable.

Acceptance (automated) — **done**: `tests/smoke_hud.py` (36 checks) drives
the combo, arrows, Enter and pointer; asserts input is stolen from the scene
while it keeps updating underneath, quick-switch keeps home on the stack,
settings apply live + persist + clamp, and a fresh scene re-applies stored
values. Captures inspected windowed and on the glasses.
Acceptance (wearer) — **OUTSTANDING**: confirm in-headset that (a) the combo
actually reaches the app — GNOME may eat `Ctrl+Super+R`, which is why
`Ctrl+Alt+R` is bound as well; report which one works, (b) the HUD sits at a
comfortable distance and is readable, (c) the scene visibly keeps running
behind it, (d) head-locked placement feels right or should become
world-locked.

**Done when:** from inside any scene: switch scenes via HUD without
visiting home; change a container setting and a global setting; every
settings surface reachable in two steps; wearer confirmed the key combo.

*Phase 3 notes (learned during the build):*

- **A5 settled by measurement, and the answer was "neither".** On a head,
  NEITHER `ctrl+super+r` nor `ctrl+alt+r` reached the app — GNOME swallows
  both before a fullscreen GLFW window sees them. Two consequences:
  - The primary way into the HUD is now a **triple head bob**
    (`refract.core.gesture.HeadBob`, wearer's own suggestion): no keyboard,
    works with the glasses on, and the compositor cannot intercept it.
    Toggle it off in Global Settings.
  - The keyboard fallback is a **bare `h`**, which the compositor has no
    reason to grab. The combos stay bound in case another session passes
    them through. `echo hud > /tmp/refract.ctl` always works.
  - **Opening the HUD is not the same as being able to USE it.** The bob
    arrives through the IMU, which needs no window focus — but the keys
    afterwards do, and the focused window is whatever the wearer last used
    on the laptop. So `App.grab_focus()` asks politely
    (`glfw.focus_window`) and, if refused, CLICKS our own window through
    the RemoteDesktop session (GNOME focuses on click). The pointer is
    handed back to the laptop panel on close, or it is stranded on the
    glasses output where our own window hides it.
  - Fallback when focus is still denied: **head pointing plus dwell** —
    look at a row, hold ~1.1 s to fire. The HUD footer says which mode it
    is in and draws a dwell bar, so it is never a mystery.
  - The row list **scrolls**. Desk has eleven settings and they overflowed
    the panel, drawing over the footer with the last rows unreachable; the
    chips row stays pinned and the rest slide to keep the selection
    visible. `tests/smoke_hud.py` now asserts every row is drawable.
  - **Quit lives on every HUD page**, and takes TWO deliberate choices. One
    touch would be wrong: with dwell activation merely LOOKING at a row for
    1.1 s fires it, so a single-step quit would end a work session by
    accident. The row turns red and says "Really quit? choose again to
    confirm"; choosing anything else, or five seconds, cancels it.
    Quitting also puts the glasses **back to 2D** if we were the ones who
    switched them to SBS — leaving them in side-by-side meant the wearer had
    to run `viture-hw.py 3d off` by hand before the panel was usable again.
    (The full version is phase 5; this is just not making a mess on exit.)
  - A **single-instance guard** refuses to start a second Refract: two
    instances fight over the glasses, both create virtual monitors, both
    rearrange the desktop, and they race for the same control file. Found
    while testing — a stray session silently ate commands meant for another.
  - Gesture design is mostly about NOT firing: a false trigger while
    reading is worse than a missed gesture. It works on deviation from a
    moving baseline (so a slow look down is not a bob), requires each dip
    to complete inside 0.9 s, and needs three inside 2.2 s. Nine tests
    cover it, most of them asserting it stays shut — reading down a page,
    slow looks, and head jitter must all do nothing.
- **The ≤2-step rule shaped the layout:** container settings are on the HUD's
  ROOT page, not behind a submenu. Toggle (1) → adjust (2). Global Settings
  is one Enter away. The root *screen* still has no settings at all — with
  HomeScene active the HUD honestly prints "this container has no settings".
- **The HUD is owned by `App`, not a scene**, so it draws over whatever runs
  and only gates input; `run()` calls `scene.update()` unconditionally. The
  smoke test pins that the scene keeps living behind it.
- `App.switch()` now **keeps the root scene** underneath, or quick-switching
  from Desk to 360 would make Esc mean "quit" instead of "back to home".
- Layout trap: the value text was drawn AFTER the ± arrows and painted over
  them, so wide values ("25.00 deg") produced rows that looked like they had
  no controls. Values now live in a fixed-width field with the arrows
  outside it.
- Enum settings **clamp rather than wrap** — wrapping an IMU rate from 240
  back to 60 on one extra keypress is a surprise you feel in the tracking.
- `settings.apply_all()` runs at scene entry so a restored config actually
  takes effect instead of only being displayed.
- Tests must pass a **throwaway `config=` dict** to `App`; otherwise they
  write the user's real `~/.config/refract/config.json`.
- A second `App` cannot be constructed in one process (`glXGetCurrentContext:
  cannot detect OpenGL context`) — test relaunch behaviour by entering a
  fresh scene instead.
- **Brightness/volume stayed read-only**: reading them needs libglasses while
  the IMU holds the device exclusively. Reading once at boot before
  `Head.start()` was considered and rejected — libglasses leaves a USB thread
  that never joins, so "sequential" is not actually sequential. This is what
  step 5 exists to settle.

### Phase 4 — Refract Desk (port xrdesk)  ✅ COMPLETE 2026-08-11
*(automated checks pass; wearer parity sign-off outstanding — step 7 of this
phase, deleting xrdesk.py, is BLOCKED on it)*

Goal: `refract/desk/` scene at parity with `xrdesk.py`, plus the spec's
monitor topology (center mirrors eDP-1, sides are virtual monitors).

Steps:

1. Read ALL of `xrdesk.py` first. The port is a transplant, not a rewrite —
   every constant and comment moves intact unless it duplicates core.
2. `refract/desk/capture.py` — mirror capture of a PHYSICAL monitor.
   Extend the ScreenCast path: unlike vdisplay's virtual monitors, a
   mirror is `ScreenCast.RecordMonitor(connector)` on a plain ScreenCast
   session consumed by an appsink (no RemoteDesktop binding needed for
   read-only capture; cursor-mode 2 composites the pointer). Prototype it
   standalone against eDP-1 before wiring the scene.
3. `refract/desk/scene.py` — `DeskScene`: three `WorldScreen`s; center
   texture ← eDP-1 mirror; left/right ← `VirtualDisplays([two sizes],
   capture=True)`. Keep xrdesk's: `rebuild()` spacing math, follow mode +
   `follow_yaw` easing, distance/size/curve keys (`[ ] - = c f r`),
   control-file commands (rename paths to `/tmp/refract.ctl` +
   `/tmp/refract.pid`, same single-word protocol), SIGUSR2 follow toggle,
   layout persistence in config section `desk` (+ "reset layout" as a
   settings action).
4. `settings_schema()`: distance, size, curve, follow, follow-threshold,
   spacing, reset-layout action — so the Phase 3 HUD renders Desk's panel
   with zero custom UI.
5. Registry: flip Desk's entry to `available=True`, factory → `DeskScene`.
6. Parity checklist vs `xrdesk.py` (each item verified, capture or wearer):
   recenter (key + SIGUSR1), follow on/off + easing, distance/size/curve
   live changes, config round-trip (change → quit → relaunch → restored),
   `--windowed`, calibration entry (via HUD action), MCU button msgid
   logging still prints.
7. Only after the USER confirms parity while wearing: delete `xrdesk.py`,
   `virtual-monitors.py`, root `vdisplay.py` shim; move `viture-ctl.py`
   and `viture-probe.py` to `tools/` (nothing path-loads them anymore);
   update README's Files section.

**Done when:** wearer does real work in Desk (laptop screen mirrored
center, independent windows left/right, cursor crosses all three), exits
to shell, relaunches, layout restored.

*Phase 4 notes (learned during the build):*

- **One capture session hosts both kinds of stream.** `vdisplay` grew
  `ScreenCapture(specs)` where a spec is `("virtual", (w, h))` or
  `("monitor", connector)`; `VirtualDisplays` stays as a thin subclass so
  `xrdesk.py` keeps working. The two kinds differ in one way that matters: a
  virtual stream's caps DEFINE the monitor's resolution and must be forced,
  while a mirror's size comes from the output — forcing one there silently
  rescales it. Mirrors therefore learn their size at runtime, which is why
  `WorldScreen.resize()` exists.
- **"Cursor continuity comes free from Mutter" was not true as written.**
  Measured with Desk running: `eDP-1@0 DP-2@1920 Meta-0@5760 Meta-1@7680` —
  Mutter parks new virtual monitors at the far right, so the pointer crosses
  them in an order that has nothing to do with the 3D arrangement, and the
  virtual screens are only reachable by travelling right *through* the
  glasses output. `desk/layout.py` plans a matching arrangement and
  `displaymode.apply_positions()` applies it (temporary method, restored on
  exit), behind the **"Match desktop layout" setting, default OFF** — it
  rearranges the wearer's desktop, which is their call, not ours. The
  planner is pure and tested; enabling it is a wearer decision.
- **Performance: Desk is the first thing to cost real frames.** Measured on
  the glasses: 35.8 fps before, 44.2 fps after. The profile (windowed):

  | what runs | frame cost | fps |
  |---|---|---|
  | pull + upload 3 streams | 27.2 ms | 31.7 |
  | pull only, no texture upload | 19.7 ms | 43.2 |
  | neither (vsync bound) | 0.4 ms | 52.4 |

  So the dominant cost is pulling samples — PyGObject hands back `bytes`,
  copying ~8 MB per stream per frame, and there is no zero-copy path through
  that API. Fix: `CONTENT_HZ = 30` refreshes desktop *content* at half rate
  while the shell keeps rendering every frame. Head tracking at 60 Hz is
  what stops a headset feeling sick; a desktop updating at 30 is merely a
  desktop updating at 30. `CONTENT_HZ` is the lever if more is needed.
  (These numbers are with a busy desktop — the test itself was generating
  damage. An idle desktop costs less, because `try_pull_sample` returns
  None.)
- **Wearer feedback (2026-08-11), both acted on:** *"don't curve the
  monitors"* and *"when I'm looking at the exact center of a monitor, it
  should fill the entire FOV — right now it's scaled smaller and the
  dithering makes it hard to read."*
  - `curve` now defaults to **0.0** (flat).
  - New `fill` mode, **default on**: the width is derived from the FOV and
    the distance rather than stored, so a screen you face fills the whole
    frustum and keeps filling it when moved (constant ANGULAR size).
    Verified numerically: the corners land at NDC ±1.000 and texel:pixel is
    **1.00** — native. The old 1.15 m default was **63%** of the fill width,
    i.e. a 1920-wide capture minified into ~1210 pixels, which is exactly
    what made text shimmer. Legibility was a resampling problem, not a
    filtering one.
  - Textures also request 16× anisotropy, which matters for the side screens
    seen at a steep angle.
  - While filling, the "Screen size" row is INFO (read-only) — an editable
    number that silently does nothing is worse than none. Touching `-`/`=`
    leaves fill mode and seeds the manual width with what is on screen.
  - Cost: **44.2 → 40.0 fps** on the glasses. The centre screen now covers
    100% of the viewport instead of 63%, so ~2.5× the fragments. Worth it;
    legibility was the complaint. Frustum-culling the side screens' uploads
    was considered and rejected — at fill size each screen is ~74° wide, so
    the neighbours' inner edges sit at the very edge of view and would not
    be culled anyway.
- With fill on, each screen subtends the whole FOV and the neighbours sit at
  roughly ±76°, so you turn your head to face them. That is the intended
  consequence of one-screen-fills-the-view, not a spacing bug.
- **Wearer round 2 (2026-08-11), all three fixed:**
  - *"I don't see a mouse"* — **Mutter cursor-mode was 2 (METADATA), not 1
    (EMBEDDED)**, so the pointer was delivered out-of-band for a client to
    draw and was never in the pixels we re-render. The code comment even
    claimed 2 meant composited. Proven fixed by warping the pointer to
    (1500,800) on an empty virtual monitor and diffing: a 14x20 cluster of
    change appears at exactly x1498-1511 y798-817. `ScreenCapture` gained
    `move_pointer()` for that test — note the D-Bus signature is `(sdd)`,
    the stream is named by string, not object path.
  - *"update rate is horrible when typing"* — a flat 30 Hz content throttle
    applied to all three screens, so the one being typed on waited behind
    two idle ones. Now the FACED screen refreshes every frame
    (`CONTENT_HZ_FOCUS = 0`) and the rest tick at 8 Hz. Measured on the
    glasses: **40.0 -> 53.4 fps**, and the typed-on screen is current.
  - *"too much head turning to reach the side monitors"* — unavoidable
    geometrically: three view-filling screens are ~76 deg apart. Answer is
    to move the arc, not the head: keys `1 2 3`, `,`/`.`, and control words
    `left|centre|right` swing the chosen screen to front, eased (an instant
    76 deg jump of everything you are looking at is how you make people
    ill). A `Screen angle` setting tightens the arc for those who prefer
    overlap; off-centre screens depth-stagger so the faced one occludes.
- **Rearranging the desktop KILLS a RecordMonitor stream.** Measured: the
  mirror delivered 105 frames in 3 s, then `ApplyMonitorsConfig` ran and it
  delivered 2, then 0, and never recovered — which is exactly how a wearer
  found it ("the center virtual monitor is frozen"). RecordVirtual streams
  are unaffected. So Desk runs TWO capture sessions and the order is
  load-bearing: virtual monitors up -> arrange the layout -> **then** start
  the mirror, and restart the mirror after any later re-arrange. Anything
  that calls `apply_positions()` while a mirror is running must restart it.
- **Mutter rejects non-adjacent layouts** ("Logical monitors not adjacent"),
  so the glasses output cannot be banished to a distant gap. It goes on a
  SECOND ROW under the desk monitors instead: sideways drags between the
  monitors never cross it, and only a deliberate downward drag can reach it.
  Without this, dragging a window toward a virtual monitor lands it on the
  glasses output — in front of the wearer's eyes, hiding the very monitors
  they were aiming for. That is why `arrange` now defaults ON.
- Incidental: the glasses output's LOGICAL size is 1280x360, not 3840x1080 —
  a 3x scale, the same content-scale quirk that makes GLFW lie about the
  framebuffer size.
- **A static virtual monitor stops producing buffers once painted**, so
  "is a sample pending?" is NOT a health check — an idle screen legitimately
  has nothing queued. `DeskScene.frames_written` counts content actually put
  on each screen; the smoke test asserts on that instead.
- `App` now owns the control channel (`/tmp/refract.ctl`, `/tmp/refract.pid`)
  and dispatches shell-wide words (`recenter`, `save`, `hud`, `quit`) before
  offering the rest to the scene via `Scene.on_command`. Phase 5's handoff
  rides the same channel.

### Phase 5 — Display Handoff  *(the Breezy-killer feature)*  ← IN PROGRESS

**Done so far (2026-08-11):**

- `refract/core/handoff.py` — `park` / `resume` / `toggle`, built out of the
  scene's own `exit()`/`enter()` rather than a second teardown that could
  drift. Measured on the device: **park 0.4–1.1 s, resume 2.5–2.7 s**, both
  inside the 3 s target. Park stops the captures, restores the desktop's
  monitor layout, drops the glasses out of SBS and iconifies the window;
  resume puts all of it back, verified to return the layout byte-identical
  to the running state.
- `python -m refract.ctl park|resume|handoff|…` — the outside-the-app
  control CLI, so a GNOME custom shortcut can drive handoff from anywhere.
  This is the answer to a keyboard we do not reliably own.
- **libglasses stdout is silenced properly**: it has
  `xr_device_provider_set_log_level`, so the fd-redirect this plan assumed is
  not needed. Level 1 = 0 lines, level 2 = 52.
- **Wear detection: `get_wear_status` DOES NOT EXIST in the x86_64
  `libglasses.so`.** It was in the Android APK teardown (RE-FINDINGS line
  90), which is a different library. The full export list is
  create/destroy/initialize/start/stop/shutdown, get+set for brightness,
  volume, display size/distance/mode, duty cycle, film mode, log level,
  `switch_dimension`, `native_dof_recenter`, `register_state_callback`,
  `execute_usb_command[_with_response]`. So on Linux the only candidate
  channel was MCU events on the public SDK.
- **Wear detection is IMPOSSIBLE on this hardware — settled, stop looking.**
  A wearer put the glasses on and off three times with EVERY MCU event
  logged (`--log-mcu` / `tools/mcu-log.py`, which print every occurrence
  rather than the first per id): **nothing arrived**. The ids seen in other
  contexts — `0x030b` on reconnect, `0x030d` around an SBS switch, `0x0301`
  once and never reproduced — are unrelated to wear. A 12 s idle baseline
  also produces zero events, so the channel is event-driven and quiet, not
  merely being missed.
- **MCU `0x030d` IS decoded: it is a display-mode event.** Observed twice in
  one session with matching payloads: `data=04` when the glasses went into
  side-by-side, `data=01` when they dropped back to 2D at park. (Note this
  is a different encoding from the SDK's own `display_mode`, 0x31/0x32.)
  Worth following up: if the glasses' own display button emits this too, it
  becomes a hardware handoff trigger — the nearest thing to the wear sensor
  this hardware does not have. It is also a cheaper way to notice a mode
  change than polling xrandr.
  The first attempt at this test was invalid and the lesson generalises:
  `Head._on_mcu` prints each id ONCE to keep the shell quiet, so any event
  that also fires at startup is invisible on its repeats — which is exactly
  the case for a wear event. When hunting an event, always log every
  occurrence.
- **Auto-park therefore triggers on the CABLE, not the head**
  (`handoff.poll_device`, 2 s interval, sysfs only): unplugging parks,
  replugging resumes. A park the wearer chose deliberately survives a
  replug — the flag is only set when the unplug is what actually parked it,
  a distinction a test caught after the first version got it wrong.

**Four bugs this phase surfaced, all fixed:**

1. `DeskScene.exit()` left `started = True`, so a resume came back with
   Mutter's default monitor placement and no mirror — the exact state park
   exists to undo.
2. A leftover control word is executed by the NEXT instance: a stale `quit`
   made a fresh launch exit 3 s in, which reads as a crash. The control file
   is now cleared at startup.
3. `swap_buffers` on an iconified window blocks forever (no frame callback),
   so `quit` while parked looked like a hang. The parked branch polls and
   idles instead, and never swaps.
4. "Only restore the SBS mode we switched" was too literal — the glasses
   were usually ALREADY in SBS at launch, so every run declined to fix it and
   they stayed stuck in a mode useless as an ordinary monitor. Exit now
   leaves 2D by default (`global.keep_sbs` opts out) and checks the actual
   mode rather than assuming.

**Still to do:** the wearer test list below, auto-park on wear-off (blocked
on decoding those MCU ids), USB unplug handling, and `mcu_with_rsp` for
brightness/volume (4b).

The single biggest usability complaint about Breezy, built as its own
feature with its own tests. The park/resume path wraps quirks that are all
already documented: SBS re-enumeration, exclusive USB, temporary-vs-
persistent Mutter config.

Steps:

1. `refract/core/handoff.py` — `park(app)` / `resume(app)`:
   park = capture streams stopped (Desk), glasses SBS off
   (`head.set_sbs(False)`), `displaymode.apply_mode(sbs=False)` for DP-2,
   status note on the laptop side? (physical display was never blanked —
   center is a MIRROR, so the laptop screen is already live; park mostly
   means "make the glasses/2D state sane and stop burning CPU").
   resume = inverse, reusing the boot-order rule (SBS before window sizing
   is not needed here — window already exists — but WAIT for
   `wait_for_mode` before restarting captures). Target < 3 s each way.
2. Global hotkey: in-window key first (glfw); a system-global hotkey needs
   a GNOME custom shortcut invoking `refract-ctl park` — implement
   `python -m refract.ctl park|resume|recenter` writing the control file.
3. Wear detection investigation (timeboxed): `nm -D
   ~/.local/share/xr_driver/lib/libglasses.so | grep -i
   'wear\|proximity\|sensor'`; watch `Head.seen_msgids` while the user
   takes glasses on/off (MCU events); monitor USB add/remove (GLib/GUdev)
   — unplug must trigger park + a status message, never a crash. Record
   findings here. Auto-park on wear-off is an enhancement behind a Global
   Settings enum: auto-park / do nothing.
4. Silence libglasses stdout (fd redirect around hardware calls) — needed
   the moment handoff calls hardware from inside the App.
4b-signature. **`mcu_with_rsp`'s signature is recovered** (static, no device).
   The vendor `.so` ships unstripped; `mcu_with_rsp` tail-calls a static
   `cmd_exec` whose mangled name demangles to
   `cmd_exec(hid_device_*, unsigned short, unsigned char*, unsigned short,
   unsigned char**, unsigned short*)`, and the prologue shows the device
   handle is supplied internally from `g_mcu_dev`. So:

   ```c
   int mcu_with_rsp(uint16_t msgid, uint8_t *data, uint16_t len,
                    uint8_t **rsp, uint16_t *rsp_len);
   ```

   Note `rsp` is a pointer-to-pointer: the SDK hands back its own buffer
   rather than filling one of ours.

   What is still missing is the OPCODE for brightness/volume.
   `xr_device_provider_set_brightness_level` in libglasses dispatches on USB
   product id first (0x1011-0x101b, 0x1101, 0x1104 ...), so the command
   bytes live in the branch for 0x101d and need more disassembly.
   **Do not guess opcodes against the device** — RE-FINDINGS' safety note
   stands: blind writes to a 64-byte vendor HID pipe risk firmware-update
   and calibration paths. Recover them statically first.

4b. **Hardware control via `mcu_with_rsp`** (carried over from phase 3 step
   5, which proved libglasses cannot reach the device while the IMU owns
   it). Drive brightness/volume/film through the public SDK client that
   already holds the USB, using the MCU ids in `RE-FINDINGS.md`; verify
   against `viture-hw.py` readings taken with Refract stopped. Delivering
   this turns the two read-only Global Settings rows into live controls.
5. Wearer test list (scripted, user-executed): park/resume ×5 consecutive;
   unplug mid-Desk → replug → resume; park → lid close/open → resume;
   `kill -9` the shell → physical display sane after logout/login (the
   TEMPORARY config method is the safety net — verify it).

**Done when:** the test list passes and the behaviours are switchable from
Global Settings in ≤2 steps.

### Phase 6 — Refract 360 (port i3d_vr)

1. Read `i3d/DESIGN.md` (all of it) and `i3d/i3d_vr.py` before porting.
   The projection code is v360-verified — port INTACT, do not rewrite the
   sampling math, and remember its IMU mapping is deliberately opposite to
   head.py's (invariant 2).
2. `refract/three60/` — scene wrapping i3d_vr's renderer: projections
   (equirect 360/180, fisheye + `--fisheye-fov`), layouts (mono/SBS/OU,
   swap-eyes), VAAPI→NV12 decode path with the colour-matrix-from-file
   rule, IMU look driven by the SHELL's Head (not i3d's own tracker — but
   through i3d's own axis mapping).
3. In-headset file picker: panel list of a media directory (config
   `three60.media_dir`), arrows/enter, built from the panel toolkit.
4. Playback controls on keys + `settings_schema` (pause/seek step,
   projection override, eye swap, disparity for the future 2D→3D path).
5. The shell owns SBS state — delete i3d_vr's own "glasses fell back to
   2D" pre-flight when running as a scene (keep it in the standalone).
6. Regression: `vr_testpattern.py` assertions must still pass against the
   ported projection module.

**Done when:** from the shell, pick a 360 file, watch it head-tracked,
adjust playback via HUD, exit back to shell — without touching a terminal.

### Phase 7 — Refract Play  *(scope deliberately thin)*

A library of launchable entries (Steam/native games, SBS-3D videos routed
to the 360/i3d player), per-entry display preset (2D big-screen vs
SBS-content-3D), launch + park capture appropriately, return to shell on
exit. Detail this phase only when it starts.

### Phase 8 — Refract TAK

Largest new build; has its own plan (`~/Vibe/VRTAK-Plan`). Do not start
before Desk, 360 and Handoff have shipped and the scene/settings/HUD
machinery is stable.

### Research track (parallel) — i3d phases 4–5

Temporal depth pipeline (motion tiles → `warp_depth` → `compose_depth`)
and iGPU inference (OpenVINO/ONNX) per `i3d/DESIGN.md`. Independent of the
shell's critical path; lands in 360/Play as a "2D→3D" toggle when ready.

---

## Cross-cutting Concerns

- **Testing without a wearer:** `--windowed` + `--sim` everywhere; labelled
  test patterns with assertions for anything geometric (`vr_testpattern.py`
  caught an inverted yaw that eyeballing could not).
- **Parhelia/refraction styling** is incremental: functional-but-plain per
  phase, one dedicated styling sweep after Phase 4 when the real surfaces
  exist (`panel_image` is the single source for panel/tile chrome — style
  there, not per-scene).
- **Docs:** update README's Files section and this plan's status snapshot at
  the end of every phase; append findings to the phase's notes rather than
  rewriting history.

## Honest Risks

- **The laptop is the ceiling** (i7-7600U / HD 620). Stereo pass ~10 ms is
  fine; three PipeWire captures + a busy desktop will contend. Measure at
  each phase (the run's exit line prints fps; keep it above ~45).
- **Wear detection may not exist** on Pro XR — the Phase 5 hotkey is the
  primary deliverable; auto-detection is an enhancement.
- **Mutter API stability:** ScreenCast/RemoteDesktop/DisplayConfig failures
  are SILENT (see vdisplay.py's three-conditions docstring). Assert monitor
  materialisation loudly; log stream caps when captures start.
- **Handoff is the crash-prone area** (re-enumeration + exclusive USB +
  threads that never join). Budget real time; it is the feature the product
  is judged on.
- **Scope creep at the root screen.** The registry + schema machinery exists
  precisely so the lazy path is the correct path. Hold the line.
