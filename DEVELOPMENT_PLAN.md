# Refract — Development Plan

> **Status (2026-08-21):** The core shell works and has been tested wearing
> the glasses. `python -m refract` opens a home screen; three head-nods open
> a menu (HUD) over anything running; **Refract Desk** gives you three
> virtual monitors, with your laptop screen mirrored onto the middle one and
> the mouse pointer crossing all three in the order you'd expect; **Display
> Handoff** hands the desktop back to your laptop and takes it again on
> command. A fast, hardware-free test suite (175+ checks, run with
> `tests/run.py`) covers the math and logic; anything visual is also checked
> by capturing a screenshot and looking at it, since a clean exit proves
> nothing about what was actually drawn.
>
> **Not built yet:** 360° video, a games hub, a tactical map, and a
> "live sky" view showing aircraft and satellites overhead. These are
> planned but not started — see the roadmap near the end of this document.

> **Where things stand (2026-10-05) -- read this first when resuming.**
> Nothing from 2026-10-04 is committed yet; the working tree also holds the
> owner's earlier plugin work (`refract/shell/plugins.py`, `registry.py`,
> README, part of `tests/selftest.py`). Done that day, all tested
> (269 checks): venv rebuilt for Python 3.14 + installer/launcher guard;
> moderngl now loads GL through GLFW (no `libGL.so` dev symlink needed);
> glasses found by EDID only (no "any 1080p monitor" fallback); unplug /
> output checks moved off the render thread; config autosave + corrupt-file
> recovery; the glasses' hidden **extended IMU report** (raw accel + gyro,
> msgId 0x53) and the **accelerometer temple-tap detector** -- verified live
> in Refract (left = recenter, right = HUD, both sides firing correctly).
> `global.imu_aux` is ON in the owner's config.
>
> Review backlog worked 2026-10-05 (all tested, 294 checks; nothing
> committed): capture asks PipeWire for BGRx and swizzles red/blue on the
> GPU (no CPU colour conversion; no fast-blit rebuild needed); mipmapped
> Desk screens; one head pose per frame for both eyes; optional motion
> prediction (`global.predict_ms`, HUD "Motion prediction", OFF by default);
> frame-rate-independent Desk follow easing; quaternion/axis-calibration
> path DELETED (euler only; `--imu-mode` and "Calibrate axes" gone); control
> channel is now a datagram socket with replies (`refract/core/control.py`;
> `refract.ctl` exits non-zero on failure); `already_running()` matches
> only `python -m refract`; plugins from `$REFRACT_PLUGIN_PATH` import as
> `refract_plugins.<folder>` (no sys.path changes) and a sub-experience that
> fails to start is reported in the glasses instead of crashing the shell
> (`App.launch`); App slimmed -- gestures in `refract/core/headinput.py`,
> exit-time 2D restore in `handoff.leave_2d_on_exit`; brightness range 0-8;
> legacy root scripts moved to `tools/`, button notes to `docs/`. Also fixed
> a latent crash: `conflicts.ask_desktop` used `shutil` without importing it
> (app-grid launch with Breezy holding the glasses would have died).
>
> Open threads, roughly in priority order:
> 1. **Desk "screens not restored"** (live run 2026-10-04): with Desk up,
>    the wearer ended up pulling the glasses to get out. Unclear whether
>    leaving Desk (Esc / HUD home) failed to reach it, or they were waiting
>    for the run's timed exit. Once the cable dropped, park + restore worked.
>    Ask what they did, then reproduce.
> 2. **Needs the glasses:** a Desk session to confirm the BGRx capture
>    (colours right) and mipmaps (side screens steadier), plus
>    `tests/run.py --all` (briefly creates real monitors); and try "Motion
>    prediction" at 30 ms to decide its default.
> 3. Decide whether `imu_aux` should default ON (it is what makes temple
>    taps work; head tracking is identical) and whether to retire
>    TempleTap/HeadBob.
> 4. Not done on purpose: trimming the long debugging-history comments
>    (house style, owner's call); the vendored VITURE/OpenCV binaries in the
>    repo (~110 MB) -- check the SDK licence allows redistribution.
> 5. GLFW fullscreen lands on the LAPTOP while the glasses are in 2D (GNOME
>    50, laptop at 1.25x). Refract is unaffected (SBS first); anything else
>    that wants a window on the glasses in 2D should use GTK 4.
>
> Tools added: `tools/imu-aux-probe.py` (at-rest check of the extended
> report), `tools/temple-tap-probe.py` (now records accel; prompts drawn
> on the glasses with GTK), `tools/tap-replay.py` (scores a capture against
> both detectors -- re-run after touching tap gates). Captures
> `tap-accel.json` / `tap-worn.json` are local (gitignored).

This document exists so that someone else — human or AI — can pick this
project up without re-learning things the hard way. It records what Refract
is, why it's built the way it is, what's been tried and rejected, and what's
next. Deep hardware/reverse-engineering notes (exact byte layouts, USB
opcodes, disassembly) live separately in the local `RE-FINDINGS.md` file, not
here — this document stays at the level of "what happened and why it
matters," not "here is the raw hex."

---

## What Refract is

Refract is a shell application for VITURE Pro XR glasses on Linux — a home
screen that lives on the glasses and hosts different "sub-experiences":

- **Desk** — three virtual monitors, working today.
- **360** — 360°/spatial video playback — planned, not built.
- **Play** — a hub for games and 3D video — planned, not built.
- **TAK** — a 3D tactical map — planned, has its own separate design doc.
- **AeroTrace** — a live 3D picture of the air above you (aircraft,
  satellites, drones) — a future idea, not started.

Two rules shape almost every design decision, both learned from what makes
existing XR desktop tools annoying to use:

1. **The home screen only ever shows a simple list of things to launch.**
   Every setting and toggle lives in the HUD menu instead, reachable in at
   most two steps from anywhere. This is a hard rule, not a guideline — it's
   easy to slowly turn a clean launcher into a cluttered control panel one
   "just this one setting" at a time, and this project deliberately refuses
   that.
2. **Switching between the glasses and your laptop screen (Display
   Handoff) is a first-class feature**, not an afterthought — it gets its
   own hotkey, its own settings, and its own tests.

Refract talks to the glasses directly through VITURE's own software
development kit, which means it cannot run at the same time as other tools
that also want exclusive access to the glasses — Breezy Desktop being the
common one. Refract checks for this itself at startup and offers to stop or
remove the conflicting software rather than just failing with a confusing
error.

---

## How it's built (in plain terms)

- **One long-running program**, not a separate process per feature. The
  glasses only allow one program to talk to them at a time, and disconnecting
  and reconnecting is slow and occasionally unstable — so Desk, 360, TAK
  etc. are all just different "scenes" inside a single running app, switched
  between instantly with no reconnect.
- **Desk's two side screens are real virtual monitors** created through
  GNOME's own desktop tools — so the mouse, dragging windows, and the
  clipboard all work normally on them, exactly like a real second monitor.
  The middle screen is different: it's a live mirror of your actual laptop
  screen, not a separate monitor.
- **Settings live in one file** (`~/.config/refract/config.json`), grouped by
  feature, and changes apply immediately — there's no "OK" button to click.
- **The menu (HUD) opens with a head gesture — three quick nods —** rather
  than a keyboard shortcut. This wasn't the original plan; see "Lessons
  learned" below for why.
- Rendering uses Python with OpenGL (via `moderngl`) and a window library
  called GLFW. This has been fast enough so far and there's no plan to
  rewrite it unless it stops being fast enough.

---

## What's done, and what isn't

| Piece | Status |
|---|---|
| Home screen (launcher) | done, tested wearing the glasses |
| HUD menu (settings, switching, quitting) | done, tested wearing the glasses |
| Desk (three virtual monitors) | done, tested wearing the glasses |
| Display Handoff (park/resume) | working; a few more real-world scenarios still to test |
| Driver-conflict detection at startup | done |
| Vehicle motion compensation (Desk) | prototype, off by default; not yet confirmed on a moving vehicle |
| 360° video | not started |
| Games/video hub ("Play") | not started |
| Tactical map ("TAK") | not started — separate design document |
| Live sky view ("AeroTrace") | future idea, not started |

Cleaned up (2026-08-22): two standalone scripts left over from before
Refract existed as its own thing — `virtual-monitors.py` (built to hand
virtual monitors to *Breezy Desktop* to render, from back when this project
depended on Breezy rather than replacing it) and the root-level `vdisplay.py`
compatibility shim that existed only to keep it working. Neither was used by
the app itself — Desk has always used `refract/core/vdisplay.py` — and
nothing else in the repo imported either file, so both were deleted outright
rather than folded into a "pending confirmation" list.

---

## Lessons learned (the ones worth remembering)

These are things that cost real debugging time, written down so nobody has
to rediscover them.

**Head tracking was broken by a data-parsing bug, not a hardware problem.**
Tilting your head up moved the view down, and pitching your head also caused
unwanted roll. The cause was that the glasses send head-orientation data in a
specific order, and the code was reading the values in the wrong order. This
kind of mistake doesn't crash anything or look obviously wrong — it just
quietly produces the wrong rotation, which is why several rounds of manual
calibration never fixed it: there was nothing a calibration step could
correct, because the data was already scrambled going in. Once the parsing
was fixed, head tracking needed **no calibration at all** — the glasses
already report angles in their own frame of reference. The one hardware
quirk that remained: the glasses report "look up" as a negative number, the
opposite of what you'd expect, so that's corrected with a single sign flip.

**GNOME (the desktop environment) intercepts most keyboard shortcuts before
Refract's window ever sees them**, especially while wearing the glasses,
where the "focus" of your keyboard is unpredictable — it might be on the
glasses window, or it might still be on whatever you were doing on the
laptop. Two shortcut combinations were tried for opening the HUD menu and
neither reached the app. The fix was to stop relying on the keyboard as the
primary way in: opening the HUD now uses a **head gesture** (three quick
nods) instead, which the glasses can always detect regardless of what has
keyboard focus. A single unmodified key and a background command file remain
as fallbacks.

### How the head-nod gesture actually works

A **"bob"** is defined as: your head pitches down at least **6 degrees**
away from wherever you'd settled, then comes back up, and the whole dip —
down and back — happens in under **0.9 seconds**. **Three bobs like that,
landing within a 2.2-second window, open (or close) the HUD.**

The tricky part isn't detecting a nod — it's *not* detecting one when you
didn't mean it. Two very different things can look similar to raw sensor
data, and both had to be designed against:

- **Reading, or glancing around, must never trigger it.** Looking down to
  read something is also a downward pitch change — but it's a much *slower*
  one, and it doesn't snap back up on its own. So a dip only counts as part
  of a bob if it completes (down-and-back) inside that 0.9-second window;
  anything slower is treated as a deliberate look, not a gesture, and it
  quietly becomes the new "neutral" position instead of counting toward
  anything.
- **A single stray twitch must never trigger it.** One dip on its own means
  nothing — it takes three within 2.2 seconds, which isn't something a head
  does by accident. After the HUD opens or closes, there's also a brief
  1.2-second "cooldown" where nothing counts, so the up-and-down settling of
  a real nod can't accidentally start counting toward the *next* trigger.

"Wherever you'd settled" — the neutral position a bob is measured against —
isn't fixed. It continuously drifts to follow wherever you're currently
holding your head, but slowly (over about 1.2 seconds), so it can track a
change in posture without being fast enough to "chase" and absorb an actual
nod before it's recognized.

This logic (`refract/core/gesture.py`) is written as a small, self-contained
piece of code that just takes a stream of (time, head-pitch) readings and
says yes/no — which means it can be, and is, tested automatically against
recorded nod patterns without anyone actually needing to put the glasses on.

### To investigate: other actions triggered by head movement, not just the HUD

The nod gesture proves head movement is a workable, keyboard-free input
channel. **Not built yet, but worth investigating:** using a *different*
head movement to trigger other frequent actions the same way — recentering
being the obvious first candidate, since right now it needs a key press, the
glasses' own button, or the control CLI, none of which are guaranteed to be
reachable for the same reasons the HUD key combo wasn't.

Recentering is also a good *first* gesture to add precisely because getting
it wrong costs nothing — worst case, it recenters when you didn't mean it
to, and you recenter again. That makes it a much safer place to experiment
than, say, a gesture that quits the app or changes display mode.

Things to work out before building this, based on what the HUD gesture
already taught us:

- **It needs its own distinct motion, not a variation on the nod.** A
  "double nod" is tempting but risky: partway through, it looks identical to
  the first two nods of the three-nod HUD sequence, so the detector (and the
  wearer) can't tell which gesture is happening until it's over. A motion on
  a different axis — a head **shake** (left-right-left, yawing rather than
  pitching), or a **tilt/roll**, held briefly — would not be confusable with
  a nod at all, which is probably the safer direction.
- **It has to pass the same two tests the nod gesture was designed
  against**: it must not fire on ordinary movement (turning to look at
  something, walking), and it must not fire on a single accidental twitch.
  That means reusing the same shape of detector — quick, sharp motion that
  snaps back, evaluated against a slowly-drifting "neutral" position, and
  requiring a short repeated pattern rather than a single motion.
- **It should reuse the existing detector approach**, not grow a second
  bespoke one: a small, pure function fed a stream of head-orientation
  samples that returns yes/no, testable against recorded motion without
  needing the glasses on, the same shape as `HeadBob` in
  `refract/core/gesture.py`. If a shake or tilt version is built, it likely
  wants its own class alongside `HeadBob` in that same file, sharing its
  general design (baseline drift, dip/return timing, a short trigger
  window) rather than duplicating it by copy-paste.
- **Worth testing on a wearer early**, more than most features — a gesture
  that's comfortable to imagine and annoying to actually perform (or that
  turns out to fire during normal use) is exactly the kind of thing that
  only shows up once someone tries it on.

#### Direction being tried (2026-08-30): temple taps, not a head motion

The head bob turned out to be inconvenient in practice. The gesture set
being evaluated instead:

- **Three quick taps on the RIGHT eyeglass temple → open/close the HUD**
  (replacing the three-nod bob).
- **Three quick taps on the LEFT temple → recenter.**

A finger tap is a sharp mechanical impulse into the frame, a completely
different signature from any head rotation, and right-vs-left should split
by the *sign* of the roll/yaw kick — which is what makes one gesture the
HUD and the other recenter. Whether it is actually separable from ordinary
motion (nods, head turns, pushing the glasses up your nose, talking) on
this IMU alone is an open question the data collection answers first.

**Data-gathering step:** `tools/temple-tap-probe.py` records the raw
VITURE IMU through a scripted set of phases — right ×3, left ×3, single
taps, then control motions (nods, head turns, talking/chewing, pushing
the glasses up the nose, on/off, walking) — and writes every sample (raw
hex payload + parsed euler/quat + SDK timestamp) to JSON, with a terminal
summary. It only records; `--analyse FILE` re-runs the summary on an
existing capture. It uses a new optional `raw_handler` hook on
`viture_sdk.Viture`, alongside the parsed `handler`. Run it **wearing the
glasses** — a tap through the worn frame is not the same as one on a
headset held in the hand.

**First capture — findings (2026-08-30, worn, one wearer, one session):**

- **The IMU payload is orientation only.** Raw length 36: euler (bytes
  0–11) + quaternion (20–35), and nothing that varies like an
  accelerometer. A tap must be detected as an *angular* pulse; there is
  no linear-accel channel.
- **A tap is a small YAW pulse on the SDK's euler yaw.** ~0.3–0.8°,
  half-width ~30–70 ms, snapping back in ~25–50 ms, often with a smaller
  opposite-sign rebound right after (the impulse ringing). **Right temple
  → +yaw, left temple → −yaw**, 100% consistent across every burst — that
  sign *is* the HUD-vs-recenter discriminator. Roll and pitch barely move
  during a tap (< ~0.3°); that stillness on the other two axes is the
  main thing separating a tap from everything else.
- **Quaternion-derived yaw was worse** — less responsive to the fast
  transient, no better at rejecting nods. Use the euler yaw the SDK
  already reports.
- **"Three quick taps" measured ~320–400 ms tap-to-tap**, with ~2.5–3.5 s
  between deliberate groups. The real noise floor (a genuine still hold)
  is tiny: yaw-residual σ ≈ 0.04°.
- **Rejection held up.** With gates {|yaw pulse| 0.3–2.0°, half-width
  < 75 ms, coincident |pitch|,|roll| residual < 0.6°} plus a
  refractory that folds the rebound in, and requiring **3 same-sign taps
  within 1.3 s**: every control phase produced 0–1 tap-like events and no
  false triple. Nods bleed heavily into yaw on this unit (pitch residual
  1.6–8.9° at those blips) but the pitch gate kills them. The closest
  call was **`adjust` (pushing the glasses up the nose)** — tap-sized on
  every axis; only the half-width gate (nose-pushes are ~95–110 ms wide)
  and the triple-pattern requirement separated it. That phase deserves
  more adversarial material in a second capture before this is trusted in
  the wild.
- Sample rate is **~202 Hz even when 240 is requested** — the glasses
  appear to cap there. A 30–40 ms pulse is ~6–8 samples; adequate.

**Detector — `TempleTap` in `refract/core/gesture.py`**, a sibling of
`HeadBob`, fed raw euler `update(t, roll, pitch, yaw)` in degrees (raw SDK
angles — the recentered camera matrix would smear the sub-degree yaw pulse
through the sign/handedness fixes in `head.matrix()`). Per sample it keeps
a short moving-average neutral pose per axis, frozen during a pulse and
for the length of a 3-tap sequence.

A *tap* is a yaw-deviation excursion that:
- reaches |amp| 0.36–2.5° (below 0.36 is incidental head yaw / fidget —
  the cost is a feather-light tap gets missed),
- **peaks within 75 ms of crossing the threshold** — a finger snaps the
  frame; a neck turn's yaw climbs over hundreds of ms. This rise-time gate
  does most of the work, and replaced an earlier "returns to baseline
  within 120 ms" rule that a real worn tap's slow yaw settle kept failing,
- at that peak has |yaw| ≥ **0.60 ×** the coincident roll/pitch deviation
  (a nod's yaw is a tenth of its pitch; this wearer's taps drag ~0.5° of
  roll along with ~0.5° of yaw, so an *absolute* roll/pitch cap could not
  be set tight enough to catch nods without also killing the taps — the
  ratio can),
- never swings roll or pitch more than 3° during the rise.

**The neutral pose tracks between taps.** Freezing it for the whole
sequence (so "all three measure against one zero") backfired: the head
drifts in the ~300 ms gaps, so tap 3 entered already off-zero and read as
a slow ramp, not a snap — `rise_ms ≈ 160`, rejected. Now it is frozen
only during a pulse and its 140 ms refractory; between taps it follows the
head (τ ≈ 0.18 s). An opposite-sign pulse within 400 ms of a counted tap
is treated as that tap's rebound — ignored, sequence kept.

Fires once, then a 1 s cooldown, when **three taps of the same sign** land
within 1.8 s, ≤ 0.9 s apart: `+1` right → toggle HUD, `−1` left →
recenter.

Pure and testable: `tests/selftest.py::test_temple_tap` drives synthetic
streams (at 112 Hz, the low end of what the panel delivers); replaying the
real capture fires `right_x3 → [+1 ×5]`, `left_x3 → [−1 ×6]`, and **every**
control phase silent — baseline, rests, single taps, head turns, nods,
talk/chew, nose-push, walk.

**Fed from the IMU thread, not the render loop.** The loop samples the
head once per vsync'd frame (~60 Hz) and a tap is ~40 ms — aliased away.
`Head.on_sample` is an optional hook called for every IMU sample on the
SDK read thread; `RenderApp._imu_sample` runs the detector there and
leaves the verdict in `_tap_pending` for the loop to act on (HUD toggle /
recenter must stay on the main thread). Timing uses the SDK `ts` (ms).

**IMU rate raised to 240 Hz requested** (was 120; the panel sustains
~200). At 120 Hz a 40 ms pulse is 3–4 samples — too few to measure a rise
time. `Head.start(rate_hz=…)`, `__main__` passes `global.imu_rate`
(default now 240), the Global Settings default matches.

`python -m refract --log-tap` prints a 2 s heartbeat (real IMU sample
rate + peak yaw/roll/pitch deviation) and every detector decision
(`enter` / `tap` / `rejected` / `FIRE`, with rise-time and cross-axis
numbers) — how to see on a head whether taps reach the detector and what
shape they have.

Gated on **`global.temple_tap`** (`globalsettings.py`), **on by default**;
head-bob (`global.head_bob`) is also still on, so either opens the HUD.

**History / soft spots:**
- 1st on-head test: nothing fired. The detector was driven from the
  vsync'd render loop (~60 Hz) — a 40 ms tap was aliased away. Moved to
  the IMU thread.
- 2nd: still nothing. `CROSS_MAX = 0.8°` absolute, taken as the max over
  the whole pulse window, vetoed almost every real tap — this wearer's
  taps carry ~0.5° of roll. `--log-tap` showed taps arriving fine (yaw
  0.35–0.6°) and thrown out on cross-axis. Replaced with the rise-time +
  ratio gates.
- 3rd: fired once, then mostly `rejected rise_ms≈160`. Frozen sequence
  baseline — fixed by tracking between taps. Raised `AMP_MIN` to 0.36.
- 4th: fired once or twice per session, still `rejected rise_ms≈160` for
  most. Cause: a *firm* tap spikes the yaw and then the whole head sways
  after it in the same direction, so taking the global max over the window
  put the "peak" on the slow sway → rise time blew past the gate.
  Rebuilt the pulse test around the **first local peak** (lock it once yaw
  drops `PEAK_DROP` below its running max) plus a **mandatory retreat**
  (yaw must fall back under `RELEASE ×` that peak before `MAX_WIDTH`, else
  it's a turn that timed out) and a `RUNAWAY` cap on the running max.
  Coaching that came out of it: tap **sharp and light**, not hard — a hard
  tap sways the head and reads as a turn.

Added **`global.temple_tap_count`** (2 or 3, default 3; applied at
startup). 2 lands a clean sequence ~2× as often but the replay shows it
also fires on a still hold and on walking — always the `−1`/recenter
side, which is the cheap one to get wrong, but 3 stays the default.

Still one capture, one wearer, and still not cleanly reproducible on a
head. Next: a fresh capture through the (now much improved) probe and
retune against it, or accept that a 0.4–0.9° yaw pulse buried in
tap-induced head motion is marginal on this IMU and fall back to a bigger
gesture (a deliberate head-shake).

#### Update (2026-10-04): the glasses CAN send the accelerometer

The orientation-only conclusion above was about the STOCK report. The
firmware has a second, undocumented report -- "imu aux", switched on with
msgId `0x53` -- that carries raw gyro (rad/s) and accelerometer (g) along
with the same euler angles, at the same ~200 Hz. Found by disassembling the
firmware; verified on the glasses (|accel| = 0.999 g at rest, noise
~0.0003 g). No firmware change: the stock SDK sends it through its
undocumented `mcu_with_rsp` export and passes the longer payload through.
Byte layout and how it was found: `RE-FINDINGS.md`, "Extended IMU report".

What exists now:
- `Viture.set_imu_aux()`, `parse_aux()`; `parse_imu()` tells the two reports
  apart by length (36 vs 46 bytes).
- `Head.start(aux=True)` / `Head.set_aux()`; the per-sample hook gets
  `(euler, quat, ts, aux)`. The extended report has no quaternion, which
  tracking (euler only since 2026-10-05) does not need.
- `global.imu_aux` (HUD: "Accelerometer stream", off by default, live).
  With it on, `--log-tap` reports the accelerometer detector's peak jolt.
- `tools/temple-tap-probe.py` records the extended report by default;
  `tools/imu-aux-probe.py` is the quick at-rest check.

**Worn capture done (2026-10-04, `tap-accel.json`).** A temple tap is a
one-sample jolt along the IMU's **Y axis: +Y right temple, -Y left**, in all
50 taps (peak 0.83-1.7 g, 98-99 % along Y). The biggest non-tap jolt
(marching) was 0.78 g but 90 % off Y; the only Y-aligned one (glasses off)
was 0.43 g. `AccelTap` in `refract/core/gesture.py` gates on both (>= 0.6 g
AND >= 80 % along Y). Replay: **13/13 triples fire with the right side, 0
false fires** in every control phase; the yaw-pulse `TempleTap` caught 1/13
of the same capture. Refract uses `AccelTap` automatically whenever the
extended report is on (`global.imu_aux`), `TempleTap` otherwise. Not yet
tried live in Refract -- see the next note.

**Window placement note:** while the glasses are in **2D** (1920x1080, the
same size as the laptop panel), a GLFW fullscreen window asked for the
glasses lands on the laptop instead (GNOME 50, laptop at 1.25x; both GLFW
backends). GTK 4's `fullscreen_on_monitor()` lands correctly, so the tap
probe draws its prompts with GTK. Refract is unaffected: it switches to
side-by-side (3840x1080) before creating its window, and a live launch
landed on the glasses. The
ICM-42688-P's own tap engine would also report the tap's axis and
direction, but no USB command reaches its registers, so that one would
need a firmware patch -- probably unnecessary with the raw accel in hand.

### Prototype: cancelling out vehicle motion in Desk

**Reported problem:** riding a bus while wearing the glasses in Desk, the
view panned left and right every time the bus turned a corner — because the
glasses' IMU can't tell "the wearer turned their head" apart from "the
vehicle turned under the wearer." Both are just a rotation to it.

**GPS was considered and ruled out.** A GPS-derived heading comes from
position changes over time, so it needs continuous forward motion to mean
anything, updates at only 1–10 Hz with real lag, and gives nothing useful
while turning at low speed or stopped. Wrong tool for correcting rotation in
real time.

**The right fix: a second rotation source that moves with the vehicle but
not with the wearer's head**, subtracted from the glasses' rotation before
Desk renders — the same technique used in vehicle-mounted sim rigs and
shipboard/aircraft AR setups. Something resting on a lap or tray turns with
the vehicle without turning with the wearer's head, which is exactly the
signal needed.

**No extra hardware needed, at least to prototype it.** Checked directly on
the dev machine (2026-08-22): it already has a working built-in
accelerometer and gyroscope (Intel's "Integrated Sensor Hub" — common on
laptops from this era, not just 2-in-1s), exposed by Linux's IIO subsystem
at `/sys/bus/iio/devices`, world-readable with no udev rules or root needed.
Confirmed live by reading the same file twice a beat apart and seeing the
number change while the laptop just sat on a desk. A dedicated USB IMU
dongle (a self-contained one, not a bare sensor board needing wiring) would
be the fallback if a laptop's own sensor turns out too easy to jostle
independently of the vehicle, but wasn't needed to get a first version
working.

**Built so far, wired into Desk behind a toggle (2026-08-22):**

- `refract/core/vehicle.py` — `YawIntegrator`, pure math (feed it
  (time, angular-rate) samples, read back an accumulated angle in degrees),
  and `LaptopIMU`, the thin layer that actually reads the sensor and feeds
  it. Same split as `gesture.HeadBob`: the math is regression-tested with
  synthetic data (`tests/selftest.py::test_vehicle_yaw`), no hardware
  needed to run the tests. Calibration is non-blocking (`begin_calibrate()`
  / `poll_calibrate()`, driven a sample at a time from Desk's per-frame
  `update()`) — the old blocking `calibrate()` sleeps for its whole
  duration and stayed only as a convenience for the standalone tool below;
  calling it from inside the render loop would freeze the shell for the
  entire calibration window, the same class of mistake `cap.start()`
  already had to be redesigned around in Phase 4.
- **"Cancel vehicle motion" in Desk's settings** (`refract/desk/scene.py`),
  off by default. Turning it on forces **Yaw only** on too if it was off —
  said out loud on the headset — because the compensation only composes
  cleanly with `yaw_only()`'s already-stripped-down rotation; folding it
  into a full 3D orientation would mean decomposing an arbitrary rotation
  into yaw-then-pitch-roll, which nothing here attempts. `yaw_only()`
  gained an `offset_deg` parameter for exactly this — see
  `tests/selftest.py::test_head_conventions`'s offset checks.
- **Compensates YAW ONLY** (turning left/right) — that's the reported
  problem, and it's also all `desk.yaw_only` already lets through from the
  glasses, so the two line up. Compensating pitch/roll too (the bus tilting
  into a turn, a hill) would need continuously knowing which way the laptop
  is resting, which needs a real sensor-fusion filter blending the
  accelerometer and gyro — not attempted yet.
- **Drift is expected and handled by calibrating, not by claiming
  perfection.** A gyroscope reports a rate, not an angle — recovering an
  angle means summing (rate x time) every sample, and any small zero-rate
  error in the sensor (every MEMS gyro has one) accumulates into a slow
  false rotation. Calibration measures and removes that error while the
  laptop sits still first, which mostly fixes it but not forever — this is
  meant to be recalibrated periodically (toggle it off and on), not trusted
  as an all-day absolute reference. Desk says so on the headset when
  calibration finishes.
- `tools/vehicle-imu-probe.py` — a standalone script (same pattern as
  `tools/imu-probe.py`) that calibrates and prints a live running yaw
  number, so the sensor and the math can be validated by actually watching
  the number track a turn, independent of Desk.

**UNVERIFIED SIGN — the one thing an actual ride has to confirm.**
`DEFAULTS["vehicle_mode"]` and `VEHICLE_YAW_SIGN` live at the top of
`refract/desk/scene.py`. The laptop's gyro axis-and-sign convention (picked
automatically from whichever way gravity points at calibration time) has no
guaranteed relationship to the glasses' own yaw sign — unlike `EULER_SIGNS`
in `head.py`, which was pinned down by wearing the glasses, this one has not
been checked against a moving vehicle. **If turning "Cancel vehicle motion"
on makes the panning WORSE** (drifts faster, or keeps panning the same
direction it did before) **rather than steadying the view, flip
`VEHICLE_YAW_SIGN` to `-1.0`.** That is the entire fix if the sign is
backwards — nothing else here should need to change.

**Still to do:** the actual bus/car ride to confirm the sign and that the
compensated view holds still through a real turn — a wearer-and-rider
judgement call no automated test can make — and, longer-term, deciding
whether the laptop-on-lap assumption is solid enough or whether this wants
the dedicated-USB-dongle fallback mentioned above for a more rigidly
vehicle-mounted reference.

**Only one program can talk to the glasses at a time.** This isn't a soft
restriction — trying to have two things access the glasses simultaneously
has caused a crash in the glasses' own software. Refract refuses to start a
second copy of itself for the same reason, and checks for (and can remove)
other software trying to hold the glasses at the same time.

**Switching the glasses between a normal screen and side-by-side 3D mode
causes them to briefly disconnect and reconnect.** Checking the mode
immediately after switching often reports a failure even though the switch
actually worked — so Refract trusts that the switch command succeeded and
double-checks the real mode a moment later, rather than trusting an
immediate read.

**Declaring the SBS switch a success without checking is worse than not
checking at all.** At boot, Refract used to skip the switch entirely if
`xrandr` already reported the wide mode (a reading that can be stale after a
crash left the panel and the compositor disagreeing), and separately threw
away the result of the poll that confirms the switch actually took. Both
holes let the app carry on straight into rendering as if side-by-side were
active when the panel was still 2D — the glasses show a broken image (both
eyes seeing the same squeezed half), no error, no crash, nothing to search
for. It was also invisible when the IMU failed to start at all: `self.head`
being `None` silently disables both the SBS switch *and* the head-bob HUD
gesture for the whole session, and the only sign was one easy-to-miss log
line among the driver-conflict output. Fixed by always confirming the real
mode with `wait_for_mode()` (not just trusting a prior `is_sbs()` read or
the switch command's return) and logging loudly, specifically, on failure —
both at boot and when handing the panel back to 2D on exit, where
`head.set_sbs(False)` had the same silently-ignored-result problem.

**A hardcoded `--monitor DP-2` default silently rendered onto the laptop
panel on a machine where the glasses came up as `DP-1` instead.**
`render.py`'s fullscreen window setup falls back to the *primary* monitor
whenever the requested connector name is not found among GLFW's monitor
list — a reasonable fallback for "somehow nothing matched," but it made a
wrong guess indistinguishable from a working launch: no error, a normal
frame rate, just the wrong screen. `displaymode.py` already had
`glasses_connector()` (finds the glasses by EDID vendor, not a guessed
name) but nothing in `__main__.py` called it. Fixed by defaulting
`--monitor` to `None` and auto-detecting via EDID when it is, falling back
to the old `DP-2` guess (loudly, not silently) only if detection itself
fails. `--monitor` still overrides, for the rare case EDID detection picks
wrong.

**Uninstalling XRLinuxDriver through Refract's own conflict-resolution flow
quietly took USB permissions with it.** The glasses' device node has no
special udev rule of its own — that access came from XRLinuxDriver's
installer, which Refract never depended on registering in its own right.
Once that driver is gone, the node reverts to the kernel default
(`root:root`, no group), `lsusb` still sees the device (read-only), but the
vendor SDK's `init()` silently fails to *claim* it and reports a generic
"are the glasses plugged in?" — confusing when they plainly are. Fixed with
`udev/99-refract-xr.rules` (installed via `tools/install-udev-rule.sh`,
kept separate from the deliberately root-free `install.sh`) granting access
independent of whatever other XR software has or hasn't been installed.
`install.sh` now also detects (but does not silently fix) an unwritable
device node and points at the installer script.

**A manual cable reseat is not one clean unplug/replug -- it blips
absent/present several times over a few seconds while the connector settles,
and Display Handoff's cable-detection used to react to every blip.**
Observed on a real reseat: three separate parks, a spawned "quit?" dialog,
and on the last cycle the monitor-layout restore itself failed ("Logical
monitors not adjacent") from being asked to tear down and rebuild the
arrangement faster than Mutter could settle -- which is what actually ended
the session, not a deliberate quit. Fixed by debouncing only the
"it's-really-gone" direction (`handoff.CONFIRM_UNPLUG`, 3 seconds of
sustained absence before it counts): a replug is still trusted and acted on
immediately with no added delay, since a wearer plugging back in wants that
to feel instant, and there is nothing to lose by resuming promptly. A blip
shorter than the confirm window now has NO effect at all -- Desk is never
torn down for it in the first place, which is a stronger guarantee than
recovering gracefully after the fact.

**Restoring a monitor layout is not "put the old numbers back" -- Mutter
rejects any config where the monitors as a WHOLE are not adjacent, and
Desk's park/exit restore only had numbers for the two REAL monitors.**
Confirmed even with the unplug debounce above in place and only a single,
genuine park happening: the pre-Desk snapshot (`_saved_positions`) is taken
before the virtual monitors exist, so it only covers `eDP-1`/the glasses
output. Reapplying just that snapshot while the virtuals are still present
(deliberately -- restoring has to happen before they vanish, or the
snapshot would reference outputs that no longer exist) left them exactly
where Desk's own arrange had put them, and a layout with some monitors
restored and others untouched is not guaranteed adjacent as a set. Mutter
rejected it ("Logical monitors not adjacent"), the exception was caught and
logged, and — the actually damaging part — because it was caught, the
restore was silently treated as done: `_saved_positions` still got cleared,
so the ORIGINAL layout was gone for good, and the next arrange-on snapshot
just captured the corrupted state as the new "original." Every park/resume
cycle after the first compounded on top of that. Fixed with
`DeskScene._restore_positions()`, which uses `layout.plan_positions()` (the
same function that already knows how to park the glasses output safely)
instead of a bare `apply_positions()` — it explicitly places every
currently-existing monitor, real ones back at their saved spot and any
still-present virtuals parked on a second row, so the result is always a
Mutter-valid set.

**GLFW's `focus_window()` is a documented no-op under Wayland ("Wayland has
no concept of client-controlled focus") — Display Handoff's auto-resume
relied on it anyway.** `park()` iconifies the window; `resume()` called
`restore_window()` + `focus_window()` to bring it back. That pair works for
a DELIBERATE handoff, where the wearer is about to click something and
naturally refocuses it themselves, but an unplug-triggered park auto-calls
`resume()` on replug with nobody about to click anything -- confirmed live,
the window stayed hidden behind the desktop until manually clicked on the
taskbar, exactly the "replug should look like nothing changed" case this
was supposed to serve, failing hardest. Fixed by not iconifying at all for
an unplug-triggered park (`park(app, reason="unplugged")` skips it,
tracked via `app._parked_iconified`) -- if the window is never minimized,
`resume()` has nothing to reclaim and nothing Wayland can refuse. Deliberate
handoff (empty `reason`) is unchanged.

**The glasses can "disconnect" without the USB device ever going away.**
Reported from a live Desk session: suddenly the wearer was looking at a
squeezed side-by-side image on the LAPTOP panel, other windows had
rearranged, and there was no keyboard to recover -- the only way out was to
physically unplug. Cause: a momentary DP dropout (a cable/connector flex,
which happens a lot while tapping the temple for the gesture) makes Mutter
migrate our fullscreen window onto the laptop panel and reshuffle the
desktop around the output that vanished -- all while the USB device stays
present, so `poll_device`'s cable check never fired and the shell just kept
rendering into the wrong panel. Fixed by giving `poll_device` a second,
independently-debounced signal alongside the USB check: `_output_healthy()`
asks the compositor whether the glasses connector is still enumerated and
still in side-by-side. A sustained failure (past the same `CONFIRM_UNPLUG`
3 s) parks with `reason="display-lost"` -- same path as an unplug: hand the
desktop back, ask on the laptop, don't iconify -- and recovery calls the
new `App.reassert_output()`, which re-issues the GLFW fullscreen request
against the glasses monitor by name (the compositor moved the window; GLFW
still thinks it is fullscreen, so nothing puts it back on its own). The
health check is conservative: any error querying the compositor, or a
headless / windowed app, returns "healthy" so a transient D-Bus hiccup
can't tear down a working session. **Not yet confirmed on real hardware** --
the logic is unit-tested (`tests/selftest.py::test_unplug_handoff`, the
`_output_healthy` monkeypatch block) but only a live cable-wiggle in Desk
will prove the detection fires and the re-place actually lands.

**Rearranging the desktop's monitor layout can silently kill the live mirror
of the laptop screen.** If Desk's screens get rearranged while the mirror is
running, the mirror can freeze rather than error out. Desk works around this
by arranging the monitors first and only starting the mirror after, and
restarts the mirror if anything is rearranged later.

**A dragged window can end up hidden behind the glasses' own display if the
desktop layout isn't planned carefully.** Because GNOME won't allow gaps in
a monitor layout, the glasses' own screen has to be placed on a separate
row, below the three Desk monitors, so a normal sideways drag between
screens can never accidentally land on it.

**"Is the device asleep or worn?" can't be answered on this hardware.** There
is no sensor exposed for detecting whether the glasses are actually on
someone's face — this was tested directly by putting the glasses on and off
several times while logging every possible signal, and nothing came through.
The nearest available substitute is **detecting when the USB cable is
unplugged**, which reliably triggers an automatic "park" (hand the desktop
back). Putting the glasses down without unplugging them is not detectable
and has to be handled manually (a hotkey or menu action).

**A privacy feature dims the laptop's own screen rather than turning it
off**, deliberately. The middle Desk screen is a live mirror of the laptop
panel, so switching that panel off would also break what you're looking at
in the glasses. Dimming it instead keeps the mirror working while the room
can't see your screen, and the physical brightness keys always still work
as a manual way back — so a crash never leaves someone stuck looking at a
permanently blank laptop.

**Reading the mouse pointer's position on screen is more particular than it
looks.** Testing showed that a naive way of checking "is the pointer over
this tile" was biased — measurements between four supposedly identical tiles
came back noticeably uneven, because the way a 3D scene gets projected onto
a flat screen isn't a simple straight-line stretch near the edges. The fix
projects a tile's actual shape rather than approximating it with a single
number, and this is checked automatically so it doesn't regress silently.

---

## Roadmap

**Done:**
1. Renamed and restructured the project from an earlier prototype.
2. Built the shared rendering/head-tracking core.
3. Home screen launcher.
4. HUD menu (settings, quick-switching, quitting).
5. Refract Desk (three virtual monitors).
6. Display Handoff (park the desktop / resume it), plus the driver-conflict
   check described above.
7. Vehicle motion compensation for Desk ("Cancel vehicle motion" in Desk's
   settings, off by default) — cancels a vehicle's own turning so it isn't
   mistaken for a head turn, using the laptop's built-in motion sensor. See
   "Prototype: cancelling out vehicle motion in Desk" above. **Not yet
   confirmed on an actual moving vehicle** — the compensation direction
   (`VEHICLE_YAW_SIGN`) may need flipping once someone actually rides with
   it on; the section above says exactly how to tell and what to change.

**Next up:**
8. **Temple-tap gestures** — three quick taps on the right eyeglass temple
   toggle the HUD, three on the left recenter. `TempleTap` in
   `refract/core/gesture.py` is built and tested (synthetic + real
   capture), wired into `render.py` behind the `global.temple_tap`
   setting, **on by default** (head-bob also still on). Remaining: on-head
   confirmation across a few sessions and a second data capture, then
   decide whether to retire the head-bob. See "Direction being tried
   (2026-08-30): temple taps" above.
9. **360° video** — porting an existing prototype video player into a proper
   scene, with a way to pick a file while wearing the glasses.
10. **Play** — a simple launcher for games and 3D videos. Deliberately kept
    small in scope.
11. **TAK** — a 3D tactical map. This is a large undertaking with its own
    separate planning document; won't start until Desk, 360 and Handoff are
    solid.
12. **AeroTrace** *(future idea)* — showing real aircraft, satellites and
    drones in 3D, positioned where they actually are relative to you, using
    live flight-tracking and satellite-tracking data. Two genuinely hard
    problems stand between this and being useful: the glasses have no
    compass, so there's no reliable way to know which real-world direction
    is "north" without an extra reference step; and aircraft (a few
    kilometres up) and satellites (hundreds of kilometres up) don't fit
    naturally on the same simple 3D scale. Both are solvable, just not
    started.

---

## Working with this project

A few practical habits that have proven worth keeping:

- **A clean run isn't proof that something rendered correctly.** Anything
  visual should be checked by actually taking a screenshot
  (`--capture out.png --capture-after N`) and looking at it — a bug has
  previously slipped past both a clean exit and a quick glance through the
  window.
- **Some things can only be judged by actually wearing the glasses** —
  comfort, whether a gesture is easy to trigger by accident, whether a menu
  reads clearly. Automated tests can get everything else right and still
  miss these; they need a real person trying it on.
- **Ask before deleting anything you didn't just create**, and before making
  any change that affects the wearer's actual desktop or display settings
  outside of what was asked for.
- New code belongs under `refract/`. The vendor SDK folder and the
  historical 2D→3D research folder (`i3d/`) are left alone except when a
  phase specifically calls for porting something out of them.
- Whatever changes state that the wearer needs to know about should show up
  **in the headset**, not just printed to a terminal — the terminal isn't
  visible while wearing the glasses.

---

## Known risks

- **The laptop this was built on is a modest, several-years-old machine.**
  Performance headroom is real but not huge — three video captures running
  at once plus a busy desktop is close to the ceiling on that hardware.
- **GNOME's desktop APIs can fail silently** rather than raising a clear
  error, which makes some bugs quiet and easy to miss without careful
  logging.
- **Display Handoff is the trickiest part of the system** — it involves the
  glasses reconnecting, exclusive USB access, and background processes that
  don't always shut down cleanly. It's also the single most important
  feature, so it deserves real testing time, not just a quick check.
- **The home screen is the easiest place for scope creep to happen.** The
  settings-in-the-HUD-only rule exists specifically so nobody is tempted to
  add "just one more toggle" to the launcher.
