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
head movement to trigger other frequent actions the same way — recentring
being the obvious first candidate, since right now it needs a key press, the
glasses' own button, or the control CLI, none of which are guaranteed to be
reachable for the same reasons the HUD key combo wasn't.

Recentring is also a good *first* gesture to add precisely because getting
it wrong costs nothing — worst case, it recentres when you didn't mean it
to, and you recentre again. That makes it a much safer place to experiment
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

**Next up:**
7. **Gesture-triggered recentring** (and possibly other actions) — using a
   second head movement, distinct from the HUD's three-nod gesture, to
   trigger frequent actions like recentring without a keyboard. See "To
   investigate: other actions triggered by head movement" above; not
   started, needs a design pass before it's built.
8. **360° video** — porting an existing prototype video player into a proper
   scene, with a way to pick a file while wearing the glasses.
9. **Play** — a simple launcher for games and 3D videos. Deliberately kept
   small in scope.
10. **TAK** — a 3D tactical map. This is a large undertaking with its own
    separate planning document; won't start until Desk, 360 and Handoff are
    solid.
11. **AeroTrace** *(future idea)* — showing real aircraft, satellites and
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
