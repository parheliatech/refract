"""Head gestures -- input that needs no keyboard.

Each detector is a pure class fed a stream of samples that says yes/no, so
it can be tested against synthetic or recorded motion without anyone
wearing the glasses:

  * `HeadBob` -- three quick nods (pitch dips and returns): toggles the HUD.
  * `AccelTap` -- three taps on one temple, from the accelerometer (the
    glasses' extended report): right = HUD, left = recenter.
  * `TempleTap` -- the same gesture from orientation alone (a small yaw
    pulse), used when no accelerometer data is available. Its left/right
    sign is unreliable in real wear.

Thresholds come from worn captures -- tools/temple-tap-probe.py records
one, tools/tap-replay.py scores a capture against the detectors.
"""

import math


class HeadBob:
    """A quick nod: pitch dips away from where you were holding it and comes
    back. It must not fire while someone reads (looking down a page is a
    big but SLOW pitch change -- hence a moving baseline and a time limit
    per dip) or on ordinary jitter (hence three within a window).
    """

    AMPLITUDE = 6.0        # degrees of dip that counts as a bob
    RELEASE = 0.35         # fraction of AMPLITUDE the pitch must return past
    MAX_BOB = 0.9          # seconds; slower than this is a look, not a bob
    WINDOW = 2.2           # seconds the three bobs must fall within
    COOLDOWN = 1.2         # seconds of quiet after firing
    BASELINE_TAU = 1.2     # seconds; how fast "where you were holding it"
                           # follows you. Must be slow next to a bob and
                           # fast next to a deliberate look up or down.
    NEEDED = 3

    def __init__(self, needed=None):
        self.needed = needed if needed is not None else self.NEEDED
        self.baseline = None
        self.bobs = []          # completed bob times
        self.in_dip = False
        self.dip_started = 0.0
        self.last_fire = -1e9
        self.last_t = None

    def reset(self):
        self.bobs = []
        self.in_dip = False
        self.baseline = None
        self.last_t = None

    def update(self, t, pitch_deg):
        """Feed one sample. True exactly once per completed triple bob."""
        if self.baseline is None:
            self.baseline = pitch_deg
            self.last_t = t
            return False
        dt = max(0.0, t - (self.last_t if self.last_t is not None else t))
        self.last_t = t

        deviation = pitch_deg - self.baseline
        # advance the baseline BEFORE testing, but freeze it during a dip so
        # a slow drift cannot chase the nod and swallow it
        if not self.in_dip and self.BASELINE_TAU > 0.0:
            alpha = min(1.0, dt / self.BASELINE_TAU)
            self.baseline += (pitch_deg - self.baseline) * alpha

        if t - self.last_fire < self.COOLDOWN:
            return False

        if not self.in_dip:
            if deviation <= -self.AMPLITUDE:
                self.in_dip = True
                self.dip_started = t
        else:
            if t - self.dip_started > self.MAX_BOB:
                # too slow: this was a deliberate look down, not a bob. Take
                # the new pose as the baseline so returning is not a bob
                # either.
                self.in_dip = False
                self.baseline = pitch_deg
            elif deviation >= -self.AMPLITUDE * self.RELEASE:
                self.in_dip = False
                self.bobs.append(t)
                self.bobs = [b for b in self.bobs if t - b <= self.WINDOW]
                if len(self.bobs) >= self.needed:
                    self.bobs = []
                    self.last_fire = t
                    return True
        return False


def _wrap(d):
    """Smallest signed angle equivalent to d, in degrees."""
    while d > 180.0:
        d -= 360.0
    while d < -180.0:
        d += 360.0
    return d


class TempleTap:
    """Three quick taps on one eyeglass temple. Fed raw euler degrees.

    `update(t, roll, pitch, yaw)` returns:
        0   nothing
       +1   three right-temple taps  (tapped side -> +yaw)
       -1   three left-temple taps   (tapped side -> -yaw)

    The two failure modes to design against, both worse than a missed tap:

      * firing on a nod, a head turn, talking, or pushing the glasses up
        your nose. A tap is a SMALL yaw pulse (a fraction of a degree) that
        completes FAST and leaves roll and pitch alone; every one of those
        motions violates at least one of those -- a nod carries degrees of
        pitch, a turn is large and slow, a nose-push is wide.
      * firing on a single stray twitch. One pulse means nothing; three of
        the SAME sign inside ~1.8 s is not something a head does by
        accident.

    The pulse test is: yaw crosses AMP_MIN, reaches its FIRST local peak
    within MAX_RISE seconds (a finger snaps; a neck climbs), then **falls
    back** below RELEASE x that peak before MAX_WIDTH (a tap retreats; a
    head turn just keeps going and times out). Roll and pitch stay small
    next to the yaw at that peak (CROSS_RATIO) and never swing hard on the
    way up (CROSS_ABS). The first-local-peak rule matters: a firm tap
    spikes the yaw and then the head sways after it, and taking the global
    max over the window would put the "peak" on the slow sway.
    """

    AMP_MIN = 0.36      # deg of yaw deviation that counts as a tap (worn
                        # taps 0.4-0.9; incidental head yaw reaches ~0.3)
    AMP_MAX = 2.5       # deg; a bigger first peak is the head, not a finger
    RUNAWAY = 4.0      # deg; if yaw keeps climbing past this after the
                        # first peak, the "tap" was really a head turn
    MAX_RISE = 0.220    # s from crossing AMP_MIN to the first local peak
                        # (gentle worn taps take ~200 ms to peak)
    MAX_WIDTH = 0.45    # s with no retreat seen -> not a tap. A monotonic
                        # head drift never retreats, so it always fails.
    RELEASE = 0.65     # |yaw| must fall back under this x first-peak within
                        # MAX_WIDTH. A tap retreats; a turn times out here.
    PEAK_DROP = 0.08   # deg; |yaw| dropping this far below its running max
                        # locks the first local peak (noise dead-band)
    CROSS_ABS = 3.0     # deg; a |roll| or |pitch| swing this large before
                        # the peak means the whole head moved -- not a tap
    CROSS_RATIO = 0.60  # at the first peak, |yaw dev| must be >= this * the
                        # roll/pitch dev there. A nod's yaw is a tenth of
                        # its pitch; a worn tap's yaw at least matches the
                        # roll it drags along.
    REFRACTORY = 0.14   # s fully dead after a pulse -- guards against
                        # double-detecting the same one
    SAME_SIGN_HOLD = 0.40  # s after a counted tap, an OPPOSITE-sign pulse
                        # is the impulse's rebound/ring -- ignore it, do not
                        # count it and do not reset the sequence
    GAP_MAX = 0.90       # s; taps further apart than this are not one gesture
    WINDOW = 1.8         # s the three taps must fall within
    COOLDOWN = 1.0       # s of quiet after firing
    BASELINE_TAU = 0.18  # s; how fast the neutral pose follows you. Frozen
                         # only during a pulse, not between taps -- the head
                         # drifts in the gaps.
    NEEDED = 3

    def __init__(self, needed=None):
        self.needed = needed if needed is not None else self.NEEDED
        self.base = None            # [roll, pitch, yaw] moving neutral
        self.last_t = None
        self.in_tap = False
        self.tap_start = 0.0
        self.tap_peak = 0.0         # signed yaw dev at the FIRST local peak
        self.tap_peak_t = 0.0
        self.peak_locked = False   # first local peak found, stop extending it
        self.run_max = 0.0         # running |yaw| max (for RUNAWAY / lock)
        self.tap_cross_pk = 0.0    # |roll|/|pitch| dev at the first-peak sample
        self.tap_cross_rise = 0.0  # worst |roll|/|pitch| dev up to the peak
        self.refract_until = -1e9
        self.taps = []             # (t, sign) of completed taps this sequence
        self.last_fire = -1e9
        # diagnostics: last (droll, dpitch, dyaw), and an optional
        # debug(event, dict) sink -- render's --log-tap wires it up
        self.last_dev = (0.0, 0.0, 0.0)
        self.debug = None

    def _dbg(self, ev, **kw):
        if self.debug is not None:
            try:
                self.debug(ev, kw)
            except Exception:                            # noqa: BLE001
                pass

    def reset(self):
        self.base = None
        self.last_t = None
        self.in_tap = False
        self.taps = []

    def update(self, t, roll, pitch, yaw):
        cur = (roll, pitch, yaw)
        if self.base is None:
            self.base = list(cur)
            self.last_t = t
            return 0
        dt = max(0.0, t - (self.last_t if self.last_t is not None else t))
        self.last_t = t

        droll = _wrap(roll - self.base[0])
        dpitch = _wrap(pitch - self.base[1])
        dyaw = _wrap(yaw - self.base[2])
        self.last_dev = (droll, dpitch, dyaw)

        # advance the neutral pose whenever a pulse is NOT in progress (the
        # refractory after one counts as in-progress -- it covers the
        # rebound). Between taps of a sequence the head is briefly at rest
        # and the baseline SHOULD track it, or a later tap enters off a
        # stale zero and reads as a slow ramp instead of a snap.
        idle = (not self.in_tap and t >= self.refract_until)
        if idle and self.BASELINE_TAU > 0.0:
            alpha = min(1.0, dt / self.BASELINE_TAU)
            for i in range(3):
                self.base[i] += _wrap(cur[i] - self.base[i]) * alpha

        if t - self.last_fire < self.COOLDOWN:
            return 0

        # drop a stale sequence -- a tap came too late, or the whole run
        # has dragged on past the window
        if self.taps and (t - self.taps[-1][0] > self.GAP_MAX
                          or t - self.taps[0][0] > self.WINDOW):
            self.taps = []

        if not self.in_tap:
            if t < self.refract_until:
                return 0
            if abs(dyaw) >= self.AMP_MIN:
                self.in_tap = True
                self.tap_start = t
                self.tap_peak = dyaw
                self.tap_peak_t = t
                self.peak_locked = False
                self.run_max = abs(dyaw)
                self.tap_cross_pk = max(abs(droll), abs(dpitch))
                self.tap_cross_rise = self.tap_cross_pk
                self._dbg("enter", dyaw=dyaw, droll=droll, dpitch=dpitch)
            return 0

        # -- inside a candidate pulse --
        mag = abs(dyaw)
        same_sign = (dyaw > 0.0) == (self.tap_peak > 0.0)
        self.run_max = max(self.run_max, mag)
        if not self.peak_locked:
            if same_sign and mag >= abs(self.tap_peak):
                self.tap_peak = dyaw
                self.tap_peak_t = t
                self.tap_cross_pk = max(abs(droll), abs(dpitch))
            elif mag <= self.run_max - self.PEAK_DROP:
                self.peak_locked = True     # first local peak is in
            # cross-axis only matters on the way UP to the first peak
            self.tap_cross_rise = max(self.tap_cross_rise,
                                      abs(droll), abs(dpitch))

        retreated = mag <= abs(self.tap_peak) * self.RELEASE
        over = retreated or (t - self.tap_start >= self.MAX_WIDTH)
        if not over:
            return 0

        self.in_tap = False
        self.refract_until = t + self.REFRACTORY
        rise = self.tap_peak_t - self.tap_start
        pk = abs(self.tap_peak)
        good = (self.AMP_MIN <= pk <= self.AMP_MAX
                and rise <= self.MAX_RISE
                and retreated                       # a tap falls back; a
                and self.run_max <= self.RUNAWAY    # turn keeps going / times out
                and self.tap_cross_rise <= self.CROSS_ABS
                and pk >= self.CROSS_RATIO * self.tap_cross_pk)
        sign = 1 if self.tap_peak > 0.0 else -1
        self._dbg("tap" if good else "rejected", peak=self.tap_peak,
                  rise_ms=rise * 1000.0, cross_pk=self.tap_cross_pk,
                  cross_rise=self.tap_cross_rise, run_max=self.run_max,
                  retreated=retreated)
        if not good:
            # a slow drift or a head turn wandered in -- re-anchor the
            # neutral pose to where the head is now, so its tail is not read
            # as a tap and a later real tap starts from zero
            if not retreated or rise > self.MAX_RISE:
                self.base = list(cur)
            return 0

        if self.taps and self.taps[-1][1] != sign:
            if t - self.taps[-1][0] < self.SAME_SIGN_HOLD:
                return 0               # opposite-sign pulse right after a
                                       # tap == its rebound/ring: ignore it,
                                       # keep the sequence intact
            self.taps = []             # a real, later sign flip: start over
        self.taps.append((t, sign))
        self._dbg("count", n=len(self.taps), sign=sign)
        if len(self.taps) >= self.needed:
            span = self.taps[-1][0] - self.taps[-self.needed][0]
            if span <= self.WINDOW:
                self.taps = []
                self.last_fire = t
                self._dbg("FIRE", sign=sign)
                return sign
            # slide: keep the trailing taps so a steady drum roll still
            # resolves once three land close enough together
            self.taps = self.taps[-(self.needed - 1):]
        return 0


class AccelTap:
    """Temple taps from the ACCELEROMETER (the glasses' extended report).

    `update(t, accel)` -- t in seconds, accel (x, y, z) in g -- returns
    0 nothing, +1 for `needed` taps on the RIGHT temple, -1 for the LEFT.

    A tap is a sharp jolt along the glasses' sideways axis: the IMU's +Y for
    the right temple, -Y for the left. Worn taps peak at 0.83-1.7 g of
    deviation from the running baseline, 98-99 % along Y. The biggest
    non-tap jolt seen (marching, 0.78 g) was 90 % off Y, and the only
    Y-aligned one (taking the glasses off) was 0.43 g -- so the gates are a
    magnitude (MIN_G) AND a direction (MIN_Y_SHARE), each with margin.
    """

    MIN_G = 0.6          # g of deviation; taps 0.83+, non-taps <= 0.78
    MIN_Y_SHARE = 0.8    # |dev_y| / |dev|; taps 0.98+, big non-taps <= 0.41
    BASELINE_TAU = 0.1   # s; follows posture and gravity, not a 20 ms jolt
    REFRACTORY = 0.15    # s; one jolt rings for a few samples -- one tap
    GAP_MAX = 0.9        # s between taps of one gesture (captured: ~0.33)
    WINDOW = 1.8         # s for the whole gesture
    COOLDOWN = 1.0       # s of quiet after firing
    NEEDED = 3

    def __init__(self, needed=None):
        self.needed = needed if needed is not None else self.NEEDED
        self.base = None
        self.last_t = None
        self.prev_mag = 0.0
        self.prev_dev = None
        self.prev_t = None
        self.rising = False
        self.refract_until = -1e9
        self.taps = []                  # (t, sign)
        self.last_fire = -1e9
        self.debug = None               # optional debug(event, dict) sink

    def reset(self):
        self.base = None
        self.last_t = None
        self.prev_mag = 0.0
        self.prev_dev = None
        self.rising = False
        self.taps = []

    def _dbg(self, ev, **kw):
        if self.debug is not None:
            try:
                self.debug(ev, kw)
            except Exception:                            # noqa: BLE001
                pass

    def update(self, t, accel):
        if self.base is None:
            self.base = list(accel)
            self.last_t = t
            return 0
        dt = max(0.0, t - self.last_t)
        self.last_t = t
        # deviation from the baseline as it stood BEFORE this sample, then
        # move the baseline -- otherwise the jolt partly cancels itself
        dev = [accel[i] - self.base[i] for i in range(3)]
        alpha = min(1.0, dt / self.BASELINE_TAU) if self.BASELINE_TAU else 1.0
        for i in range(3):
            self.base[i] += (accel[i] - self.base[i]) * alpha
        mag = math.sqrt(dev[0] * dev[0] + dev[1] * dev[1] + dev[2] * dev[2])

        # a peak is the sample BEFORE the first fall: judge it one sample late
        hit = 0
        if self.prev_dev is not None and self.rising and mag < self.prev_mag:
            hit = self._judge(self.prev_t, self.prev_mag, self.prev_dev)
        self.rising = mag >= self.prev_mag
        self.prev_mag, self.prev_dev, self.prev_t = mag, dev, t
        return hit

    def _judge(self, t, mag, dev):
        if mag < self.MIN_G or t < self.refract_until:
            return 0
        self.refract_until = t + self.REFRACTORY
        share = abs(dev[1]) / mag
        if share < self.MIN_Y_SHARE:
            self._dbg("rejected", g=mag, y_share=share)
            return 0
        if t - self.last_fire < self.COOLDOWN:
            return 0
        sign = 1 if dev[1] > 0.0 else -1
        if self.taps and (t - self.taps[-1][0] > self.GAP_MAX
                          or self.taps[-1][1] != sign):
            self.taps = []
        self.taps.append((t, sign))
        self.taps = [x for x in self.taps if t - x[0] <= self.WINDOW]
        self._dbg("tap", g=mag, y_share=share, sign=sign, n=len(self.taps))
        if len(self.taps) >= self.needed:
            self.taps = []
            self.last_fire = t
            self._dbg("FIRE", sign=sign)
            return sign
        return 0
