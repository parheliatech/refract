#!/usr/bin/env python3
"""Record the VITURE IMU while you tap the eyeglass temples -- DATA ONLY.

    .venv/bin/python tools/temple-tap-probe.py [--out FILE] [--rate 240]

The plan is to replace the head-bob HUD gesture with THREE QUICK TAPS on
the RIGHT temple, and add THREE QUICK TAPS on the LEFT temple as a
recenter gesture.

First capture (2026-08-30, worn, one wearer) established:

  * The payload carries orientation ONLY -- euler + quaternion, raw len 36,
    nothing accel-like. A tap has to be found as an angular pulse.
  * A tap is a small YAW pulse: ~0.3-0.8 deg, half-width ~30-70 ms, snaps
    back in ~25-50 ms. RIGHT temple -> +yaw, LEFT temple -> -yaw. Roll and
    pitch barely move (< ~0.3 deg) -- that stillness is the tell.
  * "Three quick taps" came out ~320-400 ms apart, groups ~2.5 s apart.
  * With gates {|amp| 0.3-2 deg, half-width < 75 ms, coincident
    |pitch|,|roll| < 0.6 deg} and a "3 same-sign within 1.3 s" rule, EVERY
    control phase (nods, head turns, talking/chewing, pushing the glasses
    up the nose, walking) produced 0-1 tap-like events -- no false triple.
    `adjust` (nose-push) was the closest: tap-sized on every axis, only
    the half-width and the triple-pattern separated it.

Since 2026-10-04 it records the EXTENDED report by default (msgId 0x53:
raw accelerometer + gyro, which the stock report does not carry) -- each
sample then also has "accel" (g) and "gyro" (rad/s). --no-aux for the old
orientation-only capture.

This tool only RECORDS. It changes nothing -- not the glasses' display
mode, not Refract's config. WEAR THE GLASSES: a tap through the worn
frame is not the same as one on a headset held in the hand.

It walks through a fixed script; a terminal bell + printed banner marks
each phase change. You cannot see this terminal with the glasses on, so
read the whole script first, or have someone call the phases.

Output: JSON with every raw sample (hex payload + parsed euler/quat +
SDK timestamp), grouped by phase, plus a summary printed to the terminal.
Timing/analysis uses the SDK `ts` field (milliseconds), not wall clock --
the SDK delivers samples in bursts, so wall-clock deltas are unusable.
"""

import argparse
import json
import math
import os
import shutil
import statistics as st
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.viture_sdk import (FQ, Viture, parse_aux,   # noqa: E402
                                     parse_imu)

LEAD = 12.0      # long enough to put the glasses on after pressing Enter
# (name, seconds, prompt). Control phases are negative material -- motions a
# detector must NOT mistake for a tap burst. `adjust` proved the nastiest.
PHASES = [
    ("baseline",     10.0, "sit STILL, look straight ahead"),
    ("rest_floor",    6.0, "STILL again -- this one is the true noise floor"),
    ("right_x3",     22.0, "RIGHT temple: 3 quick taps, pause ~2s, repeat"),
    ("left_x3",      22.0, "LEFT temple: 3 quick taps, pause ~2s, repeat"),
    ("right_single", 16.0, "RIGHT temple: ONE tap, ~2s apart, several times"),
    ("left_single",  16.0, "LEFT temple: ONE tap, ~2s apart, several times"),
    ("head_turns",   12.0, "look left, right, up, down -- normal big moves"),
    ("nods",         12.0, "NOD like the current HUD gesture, a few times"),
    ("talk_chew",    10.0, "talk out loud, pretend to chew"),
    ("adjust",       14.0, "push glasses up your nose, adjust fit, scratch brow"),
    ("on_off",       12.0, "take the glasses OFF and put them back ON, twice"),
    ("walk",         10.0, "march in place / walk if you can, else sway"),
]

# What gets SAID at each phase -- the wearer cannot read a terminal with
# the glasses on, and nobody memorises twelve phases. Short on purpose:
# a sentence that runs into the phase steals its first seconds.
SPOKEN = {
    "baseline":     "Sit still. Look straight ahead.",
    "rest_floor":   "Still again.",
    "right_x3":     "Right temple. Three quick taps, pause, repeat.",
    "left_x3":      "Left temple. Three quick taps, pause, repeat.",
    "right_single": "Right temple. One tap at a time.",
    "left_single":  "Left temple. One tap at a time.",
    "head_turns":   "Look around. Left, right, up, down.",
    "nods":         "Nod a few times.",
    "talk_chew":    "Talk out loud, and pretend to chew.",
    "adjust":       "Push the glasses up your nose. Adjust them. Scratch "
                    "your brow.",
    "on_off":       "Take the glasses off, and put them back on. Twice.",
    "walk":         "March in place.",
}
WARN_BEFORE = 2.5    # seconds before a phase ends: "and... stop" cue


def speaker(enabled):
    """say(text): speak without blocking the recording loop, or do
    nothing when speech is off / unavailable."""
    exe = shutil.which("spd-say") if enabled else None
    if not exe:
        return lambda text: None

    def say(text):
        try:
            # -C cancels anything still being said: a stale prompt must not
            # talk over the new one
            subprocess.run([exe, "-C"], timeout=2,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.Popen([exe, "-r", "15", text],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    return say


class GlassesPrompter:
    """Big text prompts on the glasses' own display.

    The wearer cannot read the terminal with the glasses on, and spoken
    prompts (spd-say) were not intelligible enough to follow. So: a
    fullscreen window on the glasses output showing the step, what to do,
    a countdown and what comes next. Driven from the recording loop on the
    main thread; the IMU callback runs on the SDK's own thread, so nothing
    here can cost samples.

    GTK 4, not GLFW: on this GNOME (50, laptop panel at 1.25x scale) a GLFW
    fullscreen window asked for the glasses output lands on the laptop
    panel instead -- with both GLFW backends, verified by screen capture --
    while GTK's fullscreen_on_monitor() honours the output.

    In side-by-side mode the same text is laid out once per eye. If no
    glasses display is found, `ok` is False and every call just paces the
    loop -- the terminal prompts still print.
    """

    CSS = """
        window { background: black; }
        .step  { color: #96a2b4; font-size: 40px; font-weight: bold; }
        .title { color: #ffac11; font-size: 96px; font-weight: bold; }
        .body  { color: white;   font-size: 56px; font-weight: bold; }
        .count { color: #2ebecd; font-size: 88px; font-weight: bold; }
        .next  { color: #96a2b4; font-size: 40px; font-weight: bold; }
    """

    def __init__(self):
        self.ok = False
        self._key = None
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk, GLib, Gtk
            from refract.core import displaymode
            conn = displaymode.glasses_connector()
            if not conn:
                print("  prompts      : terminal only (no glasses display)")
                return
            sbs = displaymode.is_sbs(conn)
            Gtk.init()
            disp = Gdk.Display.get_default()
            ms = disp.get_monitors()
            mon = next((ms.get_item(i) for i in range(ms.get_n_items())
                        if ms.get_item(i).get_connector() == conn), None)
            if mon is None:
                print("  prompts      : terminal only (GTK cannot see %s)"
                      % conn)
                return
            css = Gtk.CssProvider()
            css.load_from_string(self.CSS)
            Gtk.StyleContext.add_provider_for_display(
                disp, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.labels = []                 # one dict of labels per eye
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          homogeneous=True)
            for _eye in range(2 if sbs else 1):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=24, valign=Gtk.Align.CENTER)
                eye = {}
                for name in ("step", "title", "body", "count", "next"):
                    lab = Gtk.Label(justify=Gtk.Justification.CENTER)
                    lab.add_css_class(name)
                    col.append(lab)
                    eye[name] = lab
                row.append(col)
                self.labels.append(eye)
            self.win = Gtk.Window(title="tap probe")
            self.win.set_child(row)
            self.win.fullscreen_on_monitor(mon)
            self.win.present()
            self.ctx = GLib.MainContext.default()
            self.ok = True
            print("  prompts      : on the glasses (%s, %s)"
                  % (conn, "side-by-side" if sbs else "2D"))
        except Exception as e:                            # noqa: BLE001
            print("  prompts      : terminal only (%s)" % e)

    def show(self, step, title, body=(), secs_left=None, nxt=None):
        """Update the prompt and let GTK draw. Paces the caller at ~50 Hz."""
        if not self.ok:
            time.sleep(0.05)
            return
        count = "" if secs_left is None else "%d" % max(0, math.ceil(secs_left))
        key = (step, title, tuple(body), count, nxt)
        if key != self._key:
            self._key = key
            texts = {"step": step or "", "title": title,
                     "body": "\n".join(body), "count": count,
                     "next": ("next: " + nxt) if nxt else ""}
            for eye in self.labels:
                for name, lab in eye.items():
                    lab.set_text(texts[name])
        end = time.monotonic() + 0.02
        while time.monotonic() < end:
            while self.ctx.pending():
                self.ctx.iteration(False)
            time.sleep(0.004)

    def close(self):
        if self.ok:
            self.ok = False
            try:
                self.win.destroy()
                while self.ctx.pending():
                    self.ctx.iteration(False)
            except Exception:                             # noqa: BLE001
                pass


# What to show on the glasses for each phase: a short title and at most
# two lines of instruction, big enough to read through the optics.
SHOWN = {
    "baseline":     ("SIT STILL", ["look straight ahead"]),
    "rest_floor":   ("STILL AGAIN", ["don't move"]),
    "right_x3":     ("RIGHT TEMPLE", ["3 quick taps", "pause ~2 s, repeat"]),
    "left_x3":      ("LEFT TEMPLE", ["3 quick taps", "pause ~2 s, repeat"]),
    "right_single": ("RIGHT TEMPLE", ["ONE tap", "every ~2 s"]),
    "left_single":  ("LEFT TEMPLE", ["ONE tap", "every ~2 s"]),
    "head_turns":   ("LOOK AROUND", ["left, right, up, down"]),
    "nods":         ("NOD", ["a few times"]),
    "talk_chew":    ("TALK + CHEW", ["talk out loud", "pretend to chew"]),
    "adjust":       ("ADJUST", ["push glasses up your nose",
                                "adjust fit, scratch brow"]),
    "on_off":       ("OFF + ON", ["take the glasses off and",
                                  "put them back on -- twice"]),
    "walk":         ("MARCH", ["in place, or sway"]),
}


# Gates, from the first capture. Kept here so a re-run reports against the
# same rule the detector will use.
AMP_MIN, AMP_MAX = 0.30, 2.0     # deg of yaw-residual peak
HW_MAX_MS = 75.0                 # tap pulse is fast; nose-push/nod are ~100+
CROSS_MAX = 0.6                  # deg: coincident |pitch| and |roll| residual
REFRACTORY = 0.13               # s: fold an impulse's rebound into one tap
TRIPLE_WIN = 1.3                # s: three taps must fall within this


def wrap(x):
    while x > 180.0:
        x -= 360.0
    while x < -180.0:
        x += 360.0
    return x


def unwrap(seq):
    out = [seq[0]]
    for v in seq[1:]:
        out.append(out[-1] + wrap(v - out[-1]))
    return out


def load(phase):
    s = phase["samples"]
    if not s:
        return None
    t0 = s[0]["ts"]
    T = [(x["ts"] - t0) / 1000.0 for x in s]
    roll = [x["euler"][0] for x in s]
    pitch = [x["euler"][1] for x in s]
    yaw = [x["euler"][2] for x in s]
    return T, roll, pitch, yaw


def detrend(T, y, win=0.30):
    """y minus its centered moving average (half-width win/2 seconds)."""
    n = len(y)
    out = []
    j0 = j1 = 0
    for i in range(n):
        lo, hi = T[i] - win / 2, T[i] + win / 2
        while j0 < n and T[j0] < lo:
            j0 += 1
        while j1 < n and T[j1] <= hi:
            j1 += 1
        seg = y[j0:j1] or [y[i]]
        out.append(y[i] - st.fmean(seg))
    return out


def raw_events(T, resid, thr):
    """Local extrema of |resid| above thr. Returns dicts with amp, half-width."""
    n = len(resid)
    evs = []
    i = 1
    while i < n - 1:
        if (abs(resid[i]) >= thr and abs(resid[i]) >= abs(resid[i - 1])
                and abs(resid[i]) > abs(resid[i + 1])):
            amp = resid[i]
            a = b = i
            while a > 0 and abs(resid[a]) > abs(amp) / 2:
                a -= 1
            while b < n - 1 and abs(resid[b]) > abs(amp) / 2:
                b += 1
            evs.append({"t": T[i], "amp": amp, "sign": 1 if amp > 0 else -1,
                        "hw_ms": (T[b] - T[a]) * 1000.0})
            i = b + 1
        else:
            i += 1
    return evs


def taps(T, ry, rp, rr, thr):
    """raw_events filtered by the tap gates, with the rebound folded in."""
    keep = []
    for e in raw_events(T, ry, thr):
        if not (AMP_MIN <= abs(e["amp"]) <= AMP_MAX):
            continue
        if e["hw_ms"] > HW_MAX_MS:
            continue
        i = min(range(len(T)), key=lambda k: abs(T[k] - e["t"]))
        if abs(rp[i]) > CROSS_MAX or abs(rr[i]) > CROSS_MAX:
            continue
        if keep and e["t"] - keep[-1]["t"] < REFRACTORY:
            if abs(e["amp"]) > abs(keep[-1]["amp"]):
                keep[-1] = e
            continue
        keep.append(e)
    return keep


def max_same_sign(tl, sign, win=TRIPLE_WIN):
    best = 0
    xs = [e["t"] for e in tl if e["sign"] == sign]
    for i, t in enumerate(xs):
        best = max(best, sum(1 for x in xs[i:] if x < t + win))
    return best


def analyse(store):
    order = [p[0] for p in PHASES]
    prepared = {}
    for nm in order:
        ph = store.get(nm)
        got = load(ph) if ph else None
        if not got:
            prepared[nm] = None
            continue
        T, roll, pitch, yaw = got
        prepared[nm] = {
            "T": T,
            "ry": detrend(T, unwrap(yaw)),
            "rp": detrend(T, unwrap(pitch)),
            "rr": detrend(T, roll),
            "n": len(T),
        }

    floorsrc = prepared.get("rest_floor") or prepared.get("baseline")
    sd = st.pstdev(floorsrc["ry"]) if floorsrc else 0.05
    thr = max(6.0 * sd, AMP_MIN)
    print("  yaw-residual noise floor sd = %.4f deg   ->  tap threshold "
          "= %.3f deg\n" % (sd, thr))
    print("  gates: |amp| %.2f-%.1f deg, half-width < %.0f ms, coincident "
          "|pitch|,|roll| < %.1f deg" % (AMP_MIN, AMP_MAX, HW_MAX_MS, CROSS_MAX))
    print("  a gesture = 3 same-sign taps within %.1f s "
          "(+ = right = HUD,  - = left = recenter)\n" % TRIPLE_WIN)

    for nm in order:
        p = prepared[nm]
        if not p:
            print("  %-13s (no data)" % nm)
            continue
        T, ry, rp, rr = p["T"], p["ry"], p["rp"], p["rr"]
        dur = T[-1] - T[0] if len(T) > 1 else 0.0
        hz = p["n"] / dur if dur else 0.0
        tp = taps(T, ry, rp, rr, thr)
        pos = [e for e in tp if e["sign"] > 0]
        neg = [e for e in tp if e["sign"] < 0]
        gaps = [round((tp[i + 1]["t"] - tp[i]["t"]) * 1000)
                for i in range(len(tp) - 1)]
        intra = [g for g in gaps if g < 900]
        mss = max(max_same_sign(tp, 1), max_same_sign(tp, -1))
        flag = "  <-- TRIPLE" if mss >= 3 else ""
        print("  %-13s %5.0fHz  taps=%2d  (+%d / -%d)  max same-sign / %.1fs "
              "= %d%s" % (nm, hz, len(tp), len(pos), len(neg), TRIPLE_WIN,
                          mss, flag))
        if tp:
            amps = ", ".join("%+.2f" % e["amp"] for e in tp[:20])
            print("        amps: %s" % amps)
            if intra:
                print("        intra-group gaps ms: %s  (median %.0f)"
                      % (intra, st.median(intra)))
    print("\n  READ: every control phase should show 'max same-sign' <= 2.")
    print("  right_x3 / left_x3 should show clean groups of 3 with the")
    print("  expected sign; if a control phase hits 3, that motion is a")
    print("  false-positive risk and the gates need another axis.\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="temple-tap-probe.json")
    ap.add_argument("--rate", type=int, default=240, choices=sorted(FQ),
                    help="requested IMU rate; the glasses have capped at "
                         "~202 Hz regardless")
    ap.add_argument("--speak", action="store_true",
                    help="also read the phases out loud (spd-say)")
    ap.add_argument("--no-aux", dest="aux", action="store_false",
                    help="record the stock orientation-only report instead "
                         "of the extended one (raw accel + gyro, msgId 0x53)")
    ap.add_argument("--analyse", metavar="FILE",
                    help="skip recording; re-run the summary on an existing "
                         "JSON capture")
    a = ap.parse_args()

    if a.analyse:
        with open(a.analyse) as f:
            meta = json.load(f)
        analyse(meta["phases"])
        return

    print(__doc__)
    print("  Quit any running Refract first -- USB access is exclusive.\n")

    try:
        v = Viture(quiet=True)
    except RuntimeError as e:
        sys.exit("  %s\n  (is `python -m refract` still running?)" % e)

    phase = {"name": None}
    store = {}

    def on_raw(buf, ts, n):
        nm = phase["name"]
        if nm is None:
            return
        euler, quat = parse_imu(buf)
        sample = {
            "ts": int(ts),
            "n": int(n),
            "euler": [round(x, 4) for x in euler],
            "quat": [round(x, 5) for x in (quat or [])],
            "raw": list(buf),
        }
        aux = parse_aux(buf)
        if aux:
            sample["gyro"] = [round(x, 6) for x in aux[0]]
            sample["accel"] = [round(x, 6) for x in aux[1]]
        store[nm].append(sample)

    v.raw_handler = on_raw
    v.lib.set_imu_fq(FQ[a.rate])
    if v.lib.set_imu(True) != 0:
        print("  set_imu failed", flush=True)
        os._exit(1)      # SDK deinit() hangs; sys.exit would too
    if a.aux:
        rc = v.set_imu_aux(True)
        print("  extended report (accel + gyro): %s"
              % ("on" if rc == 0 else "FAILED rc=%s -- recording stock" % rc))
    time.sleep(0.6)
    if v.count == 0:
        print("  no IMU data arriving", flush=True)
        os._exit(1)      # SDK deinit() hangs; sys.exit would too

    print("  SCRIPT (read this now):")
    for nm, secs, prompt in PHASES:
        print("    %-13s %4.0fs  %s" % (nm, secs, prompt))
    say = speaker(a.speak)
    shown = GlassesPrompter()
    print("\n  starting in %.0fs -- put the glasses on.\n" % LEAD)
    say("Put the glasses on. Recording starts in %d seconds." % LEAD)
    first = SHOWN.get(PHASES[0][0], (PHASES[0][0], []))[0]
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < LEAD:
        shown.show("", "PUT THE GLASSES ON", ["recording starts in"],
                   LEAD - (time.perf_counter() - t0), first)

    for i, (nm, secs, prompt) in enumerate(PHASES):
        store[nm] = []
        sys.stdout.write("\a")
        print("  >> %-13s %s   (%.0fs)" % (nm.upper(), prompt, secs), flush=True)
        say("%s. %s" % ("Step %d of %d" % (i + 1, len(PHASES)),
                        SPOKEN.get(nm, prompt)))
        phase["name"] = nm
        tp = time.perf_counter()
        warned = False
        title, body = SHOWN.get(nm, (nm.upper(), [prompt]))
        nxt = (SHOWN.get(PHASES[i + 1][0], (PHASES[i + 1][0],))[0]
               if i + 1 < len(PHASES) else "done")
        while time.perf_counter() - tp < secs:
            left = secs - (time.perf_counter() - tp)
            if not warned and left < WARN_BEFORE:
                warned = True
                say("and stop." if i == len(PHASES) - 1 else "and, next")
            shown.show("step %d of %d" % (i + 1, len(PHASES)), title, body,
                       left, nxt)
        phase["name"] = None
    say("Done. You can take the glasses off.")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 4.0:
        shown.show("", "DONE", ["you can take the glasses off"])
    shown.close()

    v.lib.set_imu(False)

    meta = {"rate_requested": a.rate, "aux": bool(a.aux),
            "phases": {nm: {"prompt": p, "samples": store.get(nm, [])}
                       for nm, _s, p in PHASES}}
    with open(a.out, "w") as f:
        json.dump(meta, f)
    total = sum(len(store.get(nm, [])) for nm, _s, _p in PHASES)
    print("\n  wrote %s  (%d samples, %d phases)\n"
          % (a.out, total, len(PHASES)))

    print("  SUMMARY\n")
    analyse(meta["phases"])

    v.close()
    sys.stdout.flush()
    os._exit(0)      # SDK 1.0.7 deinit() hangs


if __name__ == "__main__":
    main()
