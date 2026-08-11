<h1 align="center">
  <img src="assets/icons/refract-128.png" width="96" alt=""><br>
  Refract
</h1>
<p align="center"><em>An XR shell for the VITURE Pro XR glasses on Linux.</em></p>

---

> ### ⚠️ This is very experimental software
>
> Refract drives your glasses and your display configuration directly. It
> creates and destroys monitors, rearranges your desktop layout and switches
> the glasses between 2D and side-by-side. It is under active development,
> it has rough edges, and it may misbehave on hardware that is not the one
> laptop it was built on. Nothing it does survives a logout — display changes
> use Mutter's *temporary* method deliberately — but treat it as a workbench,
> not an appliance.
>
> If it is useful to you, you can encourage the work at
> **[buymeacoffee.com/kendel](https://www.buymeacoffee.com/kendel)**.
>
> **Forks and contributions are very welcome.** The architecture decisions,
> the hardware findings and the reasoning behind every non-obvious line are
> written down in [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) so that
> someone else can pick this up without repeating the debugging.
>
> Bugs and patches are best raised as issues and pull requests here. For
> anything else — including licensing questions — contact Parhelia Technology
> through **[www.parheliatech.com](https://www.parheliatech.com)**.

---

Refract is a persistent home layer for the glasses, hosting **sub-experiences**:

| | |
|---|---|
| **Desk** | three virtual monitors — a live mirror of your laptop screen between two real extended displays |
| **360** | 360°/immersive video *(planned)* |
| **Play** | games and 3D films *(planned)* |
| **TAK** | 3D tactical map *(planned)* |
| **AeroTrace** | the live air picture over 3D terrain — aircraft, satellites and drones drawn where they actually are *(planned)* |

It talks to the glasses through the vendor SDK directly, so it does not need
— and will not work alongside — Breezy Desktop's `xr-driver`.

## Minimum hardware

| | |
|---|---|
| **Glasses** | VITURE Pro XR (USB `35ca:101d`). Other VITURE models may work; only the Pro XR is tested. 3DoF only — the Pro XR has no cameras, so 6DoF is impossible |
| **Connection** | USB-C DisplayPort alt-mode, so the glasses appear as a real display **and** a USB device. A video-only adapter will not do |
| **OS** | Ubuntu 24.04-class, **GNOME on Wayland**. Virtual monitors are created through Mutter's D-Bus API; there is no X11 path |
| **CPU/GPU** | anything from ~2017 on. Developed on an i7-7600U with Intel HD 620, which runs Desk's three captured screens at ~40–55 fps. Nothing needs a discrete GPU |
| **RAM** | 8 GB comfortably |
| **Python** | 3.10+, with system PyGObject and GStreamer (`python3-gi`, `gir1.2-gst-plugins-base-1.0`, `gstreamer1.0-plugins-good`, `gstreamer1.0-pipewire`) |

No root, and no udev rules — logind's `uaccess` already grants your login
user the USB and hidraw access.

## Quick start

```bash
git clone <your fork>  ~/Refract     # or use the directory you already have
cd ~/Refract
./install.sh
```

The installer checks your prerequisites, builds a venv, renders the icon and
adds **Refract** to your app grid. It touches nothing outside `~/.local` and
needs no root. `./install.sh --uninstall` reverses it.

**Before launching, stop Breezy's driver if you have it** — USB access to the
glasses is exclusive and only one program can hold them:

```bash
systemctl --user stop xr-driver
```

Then launch Refract from the app grid, or:

```bash
refract                  # the home screen
refract --scene desk     # straight into the virtual monitors
```

Put the glasses on and **look straight ahead** while it counts down — that
pose becomes "forward" for the session. Add `--recenter-after 6` to shorten
the wait.

### Bind the handoff key (strongly recommended)

*Settings → Keyboard → Custom Shortcuts → +*

| | |
|---|---|
| Name | `Refract handoff` |
| Command | `~/Refract/.venv/bin/python -m refract.ctl handoff` |

One press hands the desktop back to your laptop screen; another takes it
again. Without it you have to reach for a terminal, which rather defeats the
point of a handoff.

## Using it

Everything is reachable **without a keyboard**, because a fullscreen window
on the glasses rarely holds keyboard focus and GNOME intercepts most key
combinations before they reach it.

### The HUD — nod three times

**Three quick nods** open the Heads-Up navigator over whatever is running.
Three more close it. Nodding is deliberately hard to do by accident: each dip
must complete quickly and three must land inside a couple of seconds, so
reading a page or looking around does not trigger it.

From the HUD you can:

- **switch sub-experience** — the row of chips at the top
- **change the current container's settings** — right there on the first
  page, so nothing is more than two steps from anywhere
- **open Global Settings** — IMU rate, recentre, calibration, handoff
- **quit** — bottom row, and it asks once before doing it

Settings apply **live** as you change them. There is no OK button.

If Refract has the keyboard, use the arrow keys and Enter, or the mouse. If
it does not, the HUD says so and switches to **look at a row, hold about a
second to choose** — a bar fills under your selection so you can see it
coming.

`h` also opens the HUD when the window does have focus.

### Refract Desk

| key | |
|---|---|
| `1` `2` `3` | bring the left / centre / right monitor round to face you |
| `,` `.` | step to the previous / next monitor |
| `[` `]` | move the screens closer / further |
| `-` `=` | make them smaller / larger (leaves auto-fill mode) |
| `c` | flat or curved |
| `f` | follow-my-head on/off |
| `r` | recentre |
| `Esc` | back to the home screen |

Two defaults worth knowing:

- **Yaw only** — the screens follow you left and right but ignore head pitch
  and roll, so text sits still instead of swimming with every small movement.
- **Match desktop layout** — Refract rearranges your monitors' logical
  positions so the pointer crosses screens in the order you *see* them, and
  parks the glasses output out of the way so a dragged window cannot land in
  front of your eyes. Restored when you leave Desk.

Each screen fills your view at 1:1 pixels, which is what makes text
readable — so the neighbours sit about 76° away and you use `1`/`2`/`3`
rather than turning your head that far.

### Anything, from anywhere

The control CLI works no matter what has focus:

```bash
python -m refract.ctl handoff     # park / resume  (bind this to a key)
python -m refract.ctl recenter    # fix a bad reference pose
python -m refract.ctl hud         # open the HUD
python -m refract.ctl quit        # graceful exit
python -m refract.ctl left|centre|right
```

Quitting restores your monitor layout, removes the virtual monitors, saves
your settings and puts the glasses back to 2D.

## Troubleshooting

| symptom | cause |
|---|---|
| "SDK init() failed" | something else holds the glasses — `systemctl --user stop xr-driver`, and check no other Refract is running |
| Desk screens are black | PyGObject/GStreamer missing from the system, or the session is not Wayland |
| head tracking feels wrong | it should not any more; if it does, run with `--log-axis` and report what axis your movements produce |
| the HUD opens but keys do nothing | the compositor kept the keyboard; the footer will say so — use head pointing, or click the glasses' display once |
| glasses stuck in side-by-side | `./viture-hw.py 3d off` |

## Tests

```bash
.venv/bin/python tests/run.py          # quick set, ~9 s
.venv/bin/python tests/run.py --all    # + Desk, which makes real monitors
```

175 checks. Rendering is verified separately by capture (`--capture out.png
--capture-after N`), because a clean exit proves nothing about what was drawn.

## Files

```
refract/            the application (python -m refract)
  core/             head tracking, stereo renderer, capture, settings, handoff
  shell/            home launcher, HUD, calibration
  desk/             Refract Desk
tools/              icon renderer, IMU probes, MCU logger
tests/run.py        the test entry point
i3d/                2D->3D conversion + VR/360 playback (feeds Refract 360)
sdk/                official VITURE Linux SDK v1.0.7 (x86_64)
assets/             icon sources
install.sh          user-level installer
viture-probe.py · viture-ctl.py · viture-hw.py   standalone hardware CLIs
DEVELOPMENT_PLAN.md architecture, phase plan, and every hard-won finding
```

---

# Hardware reference

Everything below was paid for in debugging. Read it before touching the SDK
or display code.

## ⚠️ USB access is EXCLUSIVE

Only one process can hold the glasses. **`xr_driver_cli --disable` is not
enough** — it stops the driver *processing* but the `xrDriver` process keeps
the USB interface claimed:

```bash
systemctl --user stop xr-driver     # release the device
systemctl --user start xr-driver    # give it back
```

Two SDK clients racing caused a **segfault in
`VitureDeviceProvider::ImuReadThread`**. Refract refuses to start a second
instance for the same reason.

Measured: libglasses *can* be opened while the public SDK streams IMU without
crashing, but every USB command then fails — and `initialize()` still reports
**success** on a device it cannot talk to. Never treat a successful init as
proof the device is usable.

## ⚠️ The IMU quaternion is W, X, Y, Z

`sdk/sample/src/main.c` is explicit: offset 20 is the **scalar**.

```c
quaternionW = makeFloat(data + 20);   quaternionX = makeFloat(data + 24);
quaternionY = makeFloat(data + 28);   quaternionZ = makeFloat(data + 32);
```

Reading those four floats straight into `(x, y, z, w)` was *the*
head-tracking bug: it normalises fine and yields a valid rotation matrix,
just of the wrong rotation — a 50° rotation about X becomes 180° about a
degenerate axis. It inverts and cross-couples the axes, and **no calibration
can undo it**, which is why three rounds of axis calibration never agreed.

The euler angles at offsets 0/4/8 are roll/pitch/yaw **in the glasses' own
frame** — VITURE already corrects for how the chip is mounted — so head
tracking needs **no calibration at all**. The one hardware constant is that
the vendor reports **pitch positive nose-down**.

## Wear detection is not possible

`get_wear_status` does not exist in the x86_64 `libglasses.so` (it was in the
*Android* teardown, a different library), and putting the glasses on and off
produces **no MCU events at all** — verified three times with every event
logged. Display Handoff is therefore manual, plus an automatic park when the
cable is unplugged.

Decoded so far: **MCU `0x030d` is a display-mode event** — `data=04` for
side-by-side, `data=01` for 2D.

## Mutter (GNOME) notes

- Under Wayland **xrandr is read-only**; mode and position changes go through
  `org.gnome.Mutter.DisplayConfig`. Refract uses the TEMPORARY method so a
  crash cannot outlive the session.
- **Logical monitors must be adjacent** — Mutter rejects layouts with gaps,
  so an output can only be moved to another row, never banished.
- **Rearranging the layout kills a `RecordMonitor` stream** (105 frames in
  3 s → 0, never recovering). `RecordVirtual` streams survive. Arrange first,
  start mirrors after.
- Virtual monitors need **three** conditions and every failure is silent —
  see `refract/core/vdisplay.py`.
- `cursor-mode` is `0 hidden / 1 embedded / 2 metadata`; only **1** draws the
  pointer into the frame.
- The glasses output carries a **3× scale**: 3840×1080 of pixels is 1280×360
  logical, and GLFW lies about the framebuffer size — take it from
  `ctx.screen.size`.

## Two SDKs exist

**1. Public SDK v1.0.7** (bundled in `sdk/`): `init`, `deinit`, `set_imu`,
`get_imu_state`, `set_3d`, `get_3d_state`, `set_imu_fq`, `get_imu_fq`,
`open_log`, plus two undocumented exports — `init_fd` and **`mcu_with_rsp`**,
the escape hatch to everything the public API omits. Recovered from the
unstripped binary:

```c
int mcu_with_rsp(uint16_t msgid, uint8_t *data, uint16_t len,
                 uint8_t **rsp, uint16_t *rsp_len);
```

**2. `libglasses.so`** (vendored in
[XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver)) — 38
`xr_device_provider_*` functions including brightness, volume, duty cycle and
film mode. Not on the public download page; check licensing before
redistributing.

Brightness and volume live in SDK #2, which cannot reach the device while the
IMU holds it — so the route for a running Refract is `mcu_with_rsp`. **Do not
guess opcodes against the device**: blind writes to a 64-byte vendor HID pipe
risk firmware-update and calibration paths. Recover them statically.

## Gotchas

- **`deinit()` hangs** in SDK 1.0.7 — exit with `os._exit()`.
- IMU floats are **big-endian**; byte-swap before unpacking.
- The glasses **re-enumerate** on a dimension change, so the readback right
  after `switch_dimension` times out even on success. Trust `rc=0`.
- libglasses floods stdout unless you call `set_log_level(1)`.

## Provenance

This began as "SpaceWalker on Linux" — reproducing the feature set of
VITURE's SpaceWalker Android app on Ubuntu. The reverse engineering turned
out to be mostly unnecessary once the official Linux SDK surfaced, but it
remains the reference for the wire protocol and for what the hardware can and
cannot do. "SpaceWalker" in `i3d/DESIGN.md` means that vendor application,
not this project.
