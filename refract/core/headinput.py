"""Head gestures as shell input: temple taps and the triple nod.

The detectors are pure classes in refract.core.gesture; this is the wiring:

  * on_sample()  runs on the SDK's IMU thread for every sample (~200 Hz) --
    taps are ~20-40 ms pulses that a vsync'd render loop would alias away.
    It only records a verdict; it never touches the HUD or GL.
  * poll()       runs on the main thread once per frame and ACTS: right
    temple (or a triple nod) toggles the HUD, left temple recenters.

Which tap detector runs is decided per sample: AccelTap whenever the
glasses' extended report delivers accelerometer data, the yaw-pulse
TempleTap otherwise.
"""

import math

import numpy as np

from refract.core.gesture import AccelTap, HeadBob, TempleTap

_FORWARD = np.array([0.0, 0.0, -1.0], dtype="f4")


class HeadInput:
    def __init__(self, config, log_tap=False):
        self.config = config
        self.log_tap = log_tap
        try:
            needed = int(config.get("global", {}).get("temple_tap_count", 3))
        except (TypeError, ValueError):
            needed = 3
        needed = max(2, min(4, needed))
        # three quick nods toggle the HUD (GNOME swallows key combos)
        self.bob = HeadBob()
        self.tap = TempleTap(needed=needed)
        self.accel_tap = AccelTap(needed=needed)
        self.pending = 0              # +1 right / -1 left, left for poll()
        # --log-tap heartbeat
        self._n = 0
        self._hb_n = 0
        self._hb_ts = None
        self._hb_pk = [0.0, 0.0, 0.0]  # |dyaw| |droll| |dpitch| (yaw detector)
        self._hb_acc = 0.0             # peak accel jolt, g (accel detector)
        if log_tap:
            def log(ev, kw):
                print("  tap[%s] %s" % (ev, " ".join(
                    "%s=%.3f" % (k, v) if isinstance(v, float)
                    else "%s=%s" % (k, v) for k, v in kw.items())),
                    flush=True)
            self.tap.debug = self.accel_tap.debug = log

    def taps_enabled(self):
        return self.config.get("global", {}).get("temple_tap", True)

    # -- IMU thread ---------------------------------------------------------

    def on_sample(self, euler, quat, ts, aux=None):
        """Head.on_sample hook. `ts` is SDK milliseconds; the detectors only
        need consistent deltas."""
        if not euler:
            return
        enabled = self.taps_enabled()
        if enabled:
            if aux:
                hit = self.accel_tap.update(ts / 1000.0, aux[1])
            else:
                roll, pitch, yaw = euler
                hit = self.tap.update(ts / 1000.0, roll, pitch, yaw)
            if hit:
                self.pending = hit
        if self.log_tap:
            self._heartbeat(ts, aux, enabled)

    def _heartbeat(self, ts, aux, enabled):
        """Every ~2 s of samples: rate, and how close the running detector
        came to firing -- on whichever detector is actually running (the
        yaw one is not fed while the extended report is on, and its
        deviations would read a misleading 0.00)."""
        self._n += 1
        if aux:
            self._hb_acc = max(self._hb_acc, self.accel_tap.prev_mag)
        else:
            droll, dpitch, dyaw = self.tap.last_dev
            pk = self._hb_pk
            pk[0] = max(pk[0], abs(dyaw))
            pk[1] = max(pk[1], abs(droll))
            pk[2] = max(pk[2], abs(dpitch))
        if self._hb_ts is None:
            self._hb_ts = ts
            return
        if ts - self._hb_ts < 2000:
            return
        secs = (ts - self._hb_ts) / 1000.0
        rate = (self._n - self._hb_n) / secs
        if aux:
            what = ("accel tap: peak jolt %.2f g (tap gate %.2f g)"
                    % (self._hb_acc, self.accel_tap.MIN_G))
        else:
            what = ("yaw tap: peak dev  yaw %.2f  roll %.2f  pitch %.2f"
                    % tuple(self._hb_pk))
        print("  tap: %d imu samples, ~%.0f Hz, enabled=%s | over %.0fs, %s"
              % (self._n, rate, enabled, secs, what), flush=True)
        self._hb_ts = ts
        self._hb_n = self._n
        self._hb_pk = [0.0, 0.0, 0.0]
        self._hb_acc = 0.0

    # -- main thread --------------------------------------------------------

    def poll(self, app, now):
        """Act on whatever the IMU thread detected, and run the nod
        detector (it needs only frame-rate pitch). Main thread only."""
        if self.pending:
            hit, self.pending = self.pending, 0
            if hit > 0:
                print("  temple tap (right) -> hud", flush=True)
                app.hud.toggle()
            else:
                print("  temple tap (left) -> recenter", flush=True)
                app.recenter()
        if app.head and self.config.get("global", {}).get("head_bob", True):
            fwd = app.head_rot() @ _FORWARD
            pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[1])))))
            if self.bob.update(now, pitch):
                print("  head bob -> hud", flush=True)
                app.hud.toggle()
