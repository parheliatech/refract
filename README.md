<h1 align="center">
  <img src="assets/icons/refract-128.png" width="96" alt=""><br>
  Refract
</h1>
<p align="center"><em>A Linux desktop shell for VITURE XR glasses.</em></p>

<p align="center">
  <img src="assets/hud.png" width="720"
       alt="The Refract HUD open over the home screen: a menu for switching between sub-experiences, live settings, and quitting.">
</p>

Put the glasses on and Refract gives you a floating home screen you can look
around and point at with your head. **Refract Desk** gives you three virtual
monitors (the middle one mirrors your laptop screen), and **Display Handoff**
hands your desktop straight back to the laptop when someone walks up.

This is the user's manual. Contents:
[Quick start](#quick-start) ·
[Controlling Refract: temple taps](#controlling-refract-temple-taps) ·
[The HUD](#the-hud) ·
[Desk](#refract-desk) ·
[Display Handoff](#display-handoff) ·
[Global settings](#global-settings) ·
[Commands](#commands) ·
[Installing](#installing) ·
[Troubleshooting](#troubleshooting) ·
[Plugins](#adding-an-experience-plugins)

---

## Quick start

1. [Install](#installing) Refract.
2. Plug the glasses into your laptop's USB-C port and wait for them to light up.
3. Start **Refract** from the app grid, or run `refract` in a terminal.
4. Refract switches the glasses to side-by-side 3D and asks you to **look
   straight ahead** while it counts down (about 4 seconds). That direction
   becomes "forward" for the session.
5. You are on the home screen. **Tap three times on the right temple** of the
   glasses to open the menu ([how](#controlling-refract-temple-taps)).
6. Choose **Desk** to get your three screens.

When you quit, Refract puts your monitor layout back and returns the glasses
to normal 2D, so you are never left in a half-changed state.

---

## Controlling Refract: temple taps

Your hands are on a laptop and a fullscreen window on the glasses rarely
holds keyboard focus, so Refract is designed to be driven **by the glasses
themselves**. The glasses' accelerometer feels a finger tap on the frame:

| Gesture | What it does |
|---|---|
| **Three taps on the right temple** | Opens the HUD menu. The same gesture again closes it. |
| **Three taps on the left temple** | **Recenters** your view: whichever way you are looking becomes "forward". |

From the HUD you reach everything else: switching experiences, every
setting, [About](#about-this-version), and quitting
([the HUD](#the-hud)).

**How to tap.** Use a fingertip on the arm of the frame. Three crisp taps
about a third of a second apart, like knocking on a door, not a slow press. A tap that is too soft is ignored and a slow, firm
push is not a tap; if nothing happens, tap a little sharper and keep a steady
rhythm. Pushing the glasses up your nose, nodding, walking and talking do not
count: a tap has to be a short jolt along the temple, repeated three times on
the **same** side within about two seconds.

**The accelerometer stream.** Taps are detected from the glasses'
accelerometer, which Refract asks them to send at startup (**Global Settings →
Accelerometer stream**, on by default). Head tracking works identically
either way. If your glasses cannot send the stream, or you turn it off,
Refract falls back to a much less reliable detector, so taps will often be
missed.

**Other ways to open the HUD**, all on by default, for when taps are being
fussy:

- **Three quick head-nods.** A quick, deliberate dip of the chin, down and back
  up in well under a second, three times within about two seconds. Glancing
  down to read or looking around will not trigger it.
- **The `H` key** (also `Ctrl+Super+R` or `Ctrl+Alt+R`), when the Refract
  window has keyboard focus. GNOME swallows most key combos, so `H` on its own
  is the one to try.
- **The control command** from a terminal or a keyboard shortcut:
  `refract-ctl hud` (it always arrives, whatever has focus).

Each of these can be switched off in [Global settings](#global-settings), and
**Recenter** is also on `refract-ctl recenter` and the `R` key.

---

## The HUD

The HUD is a menu that floats in front of you over whatever is running. It
has two pages.

**Main page**
- **Switch to**: the experiences. *Desk* is the working one. *360* is a
  placeholder tile for now; *Play* and *TAK* are planned and greyed out
  ("phase 7 / phase 8").
- The **settings of the experience you are in** (for Desk, see
  [below](#desk-settings)).
- **Global Settings**: things that apply everywhere
  ([list](#global-settings)). Inside it: **About**, **Back**.
- **Quit Refract**: asks you to choose it a second time, so it cannot be hit
  by accident.

**Choosing things.** Use whichever suits the moment:
- **Look and hold.** Look at a row; a bar fills under it over about a second,
  then it fires. Looking down moves down the list; for the *Switch to* chips,
  look left and right.
- **Mouse.** Point and click; click outside the menu to dismiss it.
- **Keyboard** (when Refract has focus): `↑`/`↓` select, `←`/`→` change a
  value, `Enter` or `Space` activates, `Esc` goes back one level and then
  closes the menu.

**The home screen.** `←`/`→` move between tiles and `Enter` launches one.
`Esc` on the home screen quits Refract.

---

## Refract Desk

Desk puts three screens in front of you: two **virtual monitors** and a
**mirror of your laptop screen** in the middle. The two side screens are real
monitors as far as your desktop is concerned: drag windows onto them, type
into them, copy and paste across them, exactly like a second monitor. The
mouse pointer crosses all three in the order you see them.

### Moving around

The screens are fixed in the room: turn your head to look at one. Because a
screen that fills your view is wide, reaching a neighbour can mean a big head
turn. Ways to cut that:
- **Layout presets** (below), the quickest fix.
- **Bring a screen to you** with the keys `1` `2` `3` (left, middle, right)
  or `,` `.` (previous / next), or `refract-ctl left | centre | right`: the
  whole row swings so that screen is straight ahead.
- **Follow my head**: the screens stay put until you look more than a set
  angle away, then ease after you.

### Layout presets

**HUD → Layout preset** (the first Desk setting):

| Preset | What it is | Head turn to reach a side screen |
|---|---|---|
| **Wide** | Each screen fills your view. The default. | about 77° |
| **Tight** | Still filling the view, but overlapping, with the screen you face drawn in front. | about 40° |
| **Compact** | Smaller screens (about 50° wide) with small gaps. | about 52° |
| **Custom** | Shown once you change any layout setting by hand. | |

Picking a preset sets *Fill view*, *Screen angle*, *Spacing* and (for
Compact) *Screen size* for you. Change any of those afterwards and it
becomes **Custom**. Your settings are saved automatically.

### Desk settings

All in the HUD, live; each takes effect immediately.

| Setting | Meaning |
|---|---|
| Layout preset | Wide / Tight / Compact / Custom (above) |
| Distance | How far the screens are, 0.4 to 6 m. In *Fill view* they resize to keep filling your view. |
| Fill view | Size each screen to fill your field of view (one source pixel on about one panel pixel, which keeps text readable). Off lets you set the size. |
| Screen size | The width in metres when not filling. |
| Curve | 0 flat to 1 fully curved, in steps of 0.25. |
| Spacing | Gap between screens in degrees, in automatic spacing. |
| Screen angle (0 = auto) | Degrees between neighbouring screens. 0 spaces them edge to edge; a smaller number overlaps them. |
| Yaw only (steady) | Ignore head tilt and nod so text does not swim; only left/right turns move the view. On by default. |
| Cancel vehicle motion | *Experimental.* Tries to hold the screens steady when you are a passenger on a turning train, plane or car, using the laptop's own motion sensor. Off by default; only shown when the laptop has such a sensor, and not yet confirmed on a moving vehicle. |
| Follow my head | Screens stay put until you look more than *Follow threshold* away, then ease after you. |
| Follow threshold | Degrees of head turn before they follow, 2 to 45. |
| Blank laptop screen | Dims the laptop panel's backlight while Desk runs, so the screens are yours alone. The mirror keeps working and your brightness keys always still work. |
| Match desktop layout | Lines the desktop's monitors up in the order you see them so the pointer crosses screens correctly. On by default; it rearranges your desktop while Desk runs and puts it back when you leave. |
| Reset layout | Back to the defaults. |

### Desk keys

| Key | Does |
|---|---|
| `1` `2` `3` | Bring the left / middle / right screen to face you |
| `,` `.` | Previous / next screen |
| `[` `]` | Screens nearer / farther |
| `-` `=` | Smaller / bigger (switches off *Fill view*) |
| `c` | Flat / curved |
| `f` | Follow my head on / off |
| `r` | Recenter |
| `Esc` | Back to the home screen |

---

## Display Handoff

Someone walks up to your desk? **`refract-ctl handoff`** parks everything and
hands your desktop back to the laptop screen, and the same command resumes
exactly where you left off. **Bind it to a keyboard shortcut**: *Settings →
Keyboard → Custom Shortcuts → +*, name `Refract handoff`, command
`refract-ctl handoff`. (`refract-ctl park` and `refract-ctl resume` do each
half.)

Refract also protects you from cable trouble. If the glasses are unplugged
(or their display drops out) for more than about 3 seconds, it parks and asks
on the laptop whether to keep running or quit; plugging back in resumes by
itself. A brief wiggle shorter than that is ignored.

While parked, the glasses show your normal desktop and the Refract window is
blank.

---

## Global settings

**HUD → Global Settings.** Apply to everything.

| Setting | Meaning |
|---|---|
| IMU rate | How often the glasses report head movement: 60, 90, 120 or 240 Hz (about 200 is what the hardware delivers). |
| **Accelerometer stream** | The glasses' accelerometer. **On by default and needed for reliable temple taps.** Head tracking is the same either way. |
| Motion prediction | Draw where your head *will be* when the frame is seen: off, 15, 30 or 45 ms. Smooths fast turns; too much overshoots a stop. Off by default. |
| Recenter countdown | Seconds of "look straight ahead" at startup, 3 to 60 (default 4). |
| Recenter now | Do it now. |
| HUD key | Shows the keys that open the HUD. |
| Triple head-bob opens HUD | The three-nod gesture, on by default. |
| Temple-tap (R: HUD, L: recenter) | The temple-tap gestures, on by default. |
| Taps per temple gesture | 3 (default, hardest to trigger by accident) or 2. |
| Brightness / Volume | Shown read-only: they cannot be changed while head tracking is running. |
| Display Handoff | Reminds you of the command. |
| **About** | See below. |

### About this version

**Global Settings → About** shows the Refract version, the VITURE SDK version
it runs on, and the licence of each (Refract is MIT; the SDK is VITURE's). The
same is in `refract --version`.

---

## Commands

Everything works from a terminal, whatever has keyboard focus.

**`refract [options]`**

| Option | Does |
|---|---|
| `--scene desk` | Start straight in Desk instead of the home screen |
| `--windowed` | A normal window on the laptop instead of fullscreen on the glasses (for trying things without the glasses) |
| `--no-sbs` | Do not switch the glasses to side-by-side |
| `--no-imu` | Run without head tracking |
| `--monitor DP-1` | Which output the glasses are, if auto-detection picks wrong |
| `--conflicts ask\|stop\|uninstall\|ignore` | What to do if another XR driver (Breezy Desktop, XRLinuxDriver) is holding the glasses |
| `--log-tap` | Print what the tap detector sees and decides, to debug taps |
| `--version` | Versions of Refract and the VITURE SDK |

**`refract-ctl <command>`** (a bare `python -m refract.ctl` in a checkout)

`handoff` · `park` · `resume` · `recenter` · `hud` · `home` · `quit`, and for
Desk: `left` · `centre` · `right` · `nearer` · `farther` · `smaller` ·
`bigger` · `curve` · `follow` · `fill` · `save`. It exits non-zero and says so
if Refract is not running or does not know the command.

---

## Installing

### What you need

- **VITURE Pro XR glasses** (other models are untested), connected by USB-C in
  DisplayPort alt-mode: they must show up as both a display and a USB device,
  so a video-only adapter will not work.
- **3DoF only.** The Pro XR has no cameras, so head *position* is not tracked,
  only where you are looking.
- **Ubuntu 26.04 with GNOME 50 on Wayland.** That is what Refract is built and
  tested on. Virtual monitors come from GNOME's own APIs, so there is no X11
  path, and other GNOME versions are untested.
- A laptop from roughly the last decade. It does not need a discrete GPU; it
  was built on a mid-range 2017 ultrabook.

### From the .deb (recommended)

```bash
sudo apt install ./refract_0.1.4_amd64.deb
```

Download the `.deb` from the
[Releases page](https://github.com/parheliatech/refract/releases). `apt` pulls
in everything Refract needs (GTK 4, GStreamer and PipeWire, moderngl and so
on) and installs the permission rule that lets your user talk to the glasses,
so nothing else is needed. Start **Refract** from the app grid, or run
`refract`.

- **Upgrade:** the same command with the newer file. Your settings in
  `~/.config/refract` are kept.
- **Remove:** `sudo apt remove refract` (settings stay; delete
  `~/.config/refract` too if you want them gone).
- If you earlier installed from a checkout, run `./install.sh --uninstall`
  first, so its old launcher in `~/.local/bin` does not shadow the package.

The package carries VITURE's closed-source runtime library, unmodified, under
the [VITURE SDK License Agreement](https://www.viture.com/viture-sdk-license-agreement).
See `docs/THIRD-PARTY.md` (installed under `/usr/share/doc/refract`).

### From a checkout

```bash
git clone https://github.com/parheliatech/refract ~/Refract
cd ~/Refract
./install.sh
```

The installer checks what you have, downloads VITURE's Linux SDK from VITURE
(it is not ours to ship, and using it means accepting VITURE's licence
agreement above), builds a local Python environment, renders an icon and adds
**Refract** to your app grid. It touches only `~/.local` and the checkout:
nothing system-wide, no root. `./install.sh --uninstall` takes it back out.

If Refract cannot reach the glasses from a checkout, a one-time helper
installs the permission rule (the only part that needs `sudo`):
`sudo tools/install-udev-rule.sh`. To build the `.deb` yourself:
`packaging/build-deb.sh`.

---

## Where your settings are

Everything is in one file, `~/.config/refract/config.json`, grouped by
experience, saved automatically a second after you stop changing things.
To start over, delete it (or use Desk's *Reset layout*). Nothing Refract does
to your displays survives a logout.

---

## Troubleshooting

| Symptom | What to try |
|---|---|
| Refract says the glasses **would not switch to side-by-side** and stops | The glasses' firmware sometimes refuses the mode switch, especially after many quick switches. **Unplug the USB-C cable for about 15 seconds**, plug it back in, and start again. A quick replug often is not enough. |
| Refract says the **VITURE library could not load** | The package is incomplete. Reinstall it (`sudo apt install --reinstall refract`), or re-run `./install.sh` from a checkout. |
| Won't start, or "SDK init() failed" | Something else has the glasses. Refract checks for this itself and offers to stop or remove it (Breezy Desktop is the usual culprit); or run `refract --conflicts stop`. |
| Temple taps do nothing, or only sometimes | Check **Global Settings → Accelerometer stream** is **on** (the startup log says "extended report" when it is), tap three crisp times on the **same** side, and run `refract --log-tap` in a terminal to see what the detector sees. If your glasses refuse the stream, the log says so and taps fall back to the unreliable detector; use nods or the `H` key instead. |
| The HUD opens but keys do nothing | GNOME kept the keyboard focus. Use look-and-hold or the mouse, or click the glasses' display once. |
| Desk screens are black | Your session is not Wayland, or a dependency is missing: reinstall the package. |
| The pointer **freezes on a side screen** until the screen changes | A known GNOME 50 behaviour: a virtual monitor sends no new picture when only the pointer moves. Moving a window or typing refreshes it. |
| Side screens feel laggy | Fixed in 0.1.4. If you still see it after a Desk layout change, report it with `refract` run from a terminal. |
| The glasses are stuck in side-by-side after a crash | Unplug them for about 15 seconds; or start Refract and quit it normally, which returns them to 2D. |
| Head tracking feels off | Run `refract --log-axis` and open an issue saying which axis each movement turns about. |
| `refract` runs an old version after installing the `.deb` | An old launcher from `./install.sh` in `~/.local/bin` is shadowing it. Run `./install.sh --uninstall` from your checkout. |

---

## Adding an experience (plugins)

A sub-experience can be dropped in without touching the shell. Copy the
template, rename it, relaunch:

```
cp -r examples/plugin-template ~/refract-plugins/hello
REFRACT_PLUGIN_PATH=~/refract-plugins refract
```

The folder needs two things: an `experience.toml` manifest (title, subtitle,
accent, and `scene = "module:Class"`) and the scene module it points at, a
subclass of `refract.core.render.Scene`. The scene module is imported only
when the tile is launched. Deleting the folder removes the tile; there is
nothing else to unregister. See
[`examples/plugin-template/README.md`](examples/plugin-template/README.md)
for the full manifest reference and the rules that still apply (the home
screen stays a plain launcher; settings go through the HUD; scenes run
in-process).

---

## About this project

> ### ⚠️ An experiment, not a product
>
> Refract is a hobby project: partly to see how far an AI coding assistant
> could carry a real piece of hardware software end to end, partly to explore
> what a pair of 3DoF VITURE glasses is actually good for on Linux once you
> drive them directly instead of through someone else's app, and what kind of
> interface works when your hands are on a laptop and your head is the
> pointer. It works, on the one laptop it was built and tested on, and it will
> have rough edges elsewhere. Features marked *experimental* are still being
> figured out.
>
> It creates and destroys virtual monitors, rearranges your desktop layout and
> switches the glasses between 2D and side-by-side, so treat it as a
> workbench, not an appliance.

More experiences are sketched out and not built yet: 360° video (its tile is
a placeholder), casual games, a 3D tactical map, and a live air-traffic
picture over 3D terrain. The architecture is ready for them.

Refract talks to the glasses through VITURE's own SDK directly, rather than
through Breezy Desktop's driver, so the two cannot run at the same time.
Refract offers to stop or remove Breezy Desktop for you if it finds it running.

**Forks and contributions are very welcome.** If it is useful to you, you can
support the work at
**[buymeacoffee.com/kendel](https://www.buymeacoffee.com/kendel)**.

## Licences

Refract is MIT-licensed (`LICENSE`). It runs on VITURE's closed-source SDK
library, which is VITURE's and is not covered by that licence; the full
accounting of everything it uses is in
[`docs/THIRD-PARTY.md`](docs/THIRD-PARTY.md).

## Project layout

```
refract/            the application (python -m refract)
  core/             head tracking, stereo renderer, capture, settings, handoff
  shell/            home launcher, HUD, plugin discovery
  desk/             Refract Desk
examples/plugin-template/   drop-in sub-experience template (manifest + scene)
packaging/          the .deb build (build-deb.sh) and the SDK version pin
tools/              standalone hardware CLIs, IMU and tap probes, tap replay,
                    MCU logger, blit benchmark, icon renderer,
                    install-udev-rule.sh (the optional sudo permission helper)
udev/               the permission rule that helper installs
csrc/               the C capture fast path (built by install.sh / build-deb.sh)
tests/run.py        the test entry point
docs/               hardware notes and third-party notices
i3d/                2D->3D conversion + VR/360 playback (feeds a future Refract 360)
sdk/                VITURE's hardware library (the public SDK is downloaded)
assets/             icon sources
install.sh          user-level installer
DEVELOPMENT_PLAN.md architecture, phase plan, and every hard-won finding
```
