"""Head gestures -- input that needs no keyboard.

Two detectors live here, same shape: a pure class fed a stream of
orientation samples that says yes/no, so it can be tested against
synthetic motion without anyone wearing the glasses.

  * `HeadBob` -- a quick nod (pitch dips and returns). Three toggles the
    HUD. This was the first gesture; it turned out to be awkward to
    perform on purpose.
  * `TempleTap` -- three quick finger-taps on an eyeglass temple. A tap is
    a sharp, small YAW impulse in the IMU (the tapped side swings back,
    the nose toward it, then it snaps back); RIGHT temple reads as +yaw,
    LEFT as -yaw. Roll and pitch barely move -- a "tap" that arrives with
    real pitch or roll is a nod or a jostle and is rejected. Three taps of
    the SAME sign fire once: +1 for the right temple (open/close the HUD),
    -1 for the left (recenter). Thresholds come from a worn capture --
    see `tools/temple-tap-probe.py` and DEVELOPMENT_PLAN.md.

A "bob" is a quick nod: pitch dips away from where you were holding it and
comes back. Three in quick succession toggles the HUD.

The two failure modes to design against are opposite, and both are worse
than a missed gesture:

  * firing while someone reads. Looking down a page is a big, SLOW pitch
    change, so the detector works on deviation from a moving baseline and
    requires each dip to complete quickly.
  * firing on ordinary head jitter. A single dip means nothing; three
    within a short window is not something a head does by accident.

Written as a pure class fed (time, pitch) so it can be tested against
synthetic nods -- there is no way to unit-test "a human nodded".
"""


class HeadBob:
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
    max over the window would put the "peak" on the slow sway and blow the
    rise time past the gate.
    """

    AMP_MIN = 0.36      # deg of yaw deviation that counts as a tap. Worn
                        # taps run 0.4-0.9 when done with intent; incidental
                        # head yaw and pre-tap fidget reach ~0.3, so the
                        # threshold sits above that at the cost of missing a
                        # feather-light tap (tap a little firmer -- but
                        # SHARP, not hard: a hard tap sways the whole head).
    AMP_MAX = 2.5       # deg; a bigger first peak is the head, not a finger
    RUNAWAY = 4.0      # deg; if yaw keeps climbing past this after the
                        # first peak, the "tap" was really a head turn
    MAX_RISE = 0.220    # s from crossing AMP_MIN to the first local peak.
                        # Was 0.080 -- but a --log-tap session showed gentle
                        # worn taps ramp the yaw to 0.8-1.3 deg over ~200 ms
                        # (roll/pitch staying < 1 deg, so they ARE taps), and
                        # an 80 ms gate rejected every one of them: "had to
                        # hit too hard". The retreat requirement below, not
                        # the rise time, is what separates a tap from a turn.
    MAX_WIDTH = 0.45    # s hard stop with no retreat seen -> not a tap. Was
                        # 0.20; a slow-return worn tap needs longer to cross
                        # the RELEASE line. A monotonic head drift still fails
                        # (`retreated` stays False), so widening this cannot
                        # pass one -- it only gives a real tap room to fall.
    RELEASE = 0.65     # |yaw| must fall back under this x first-peak within
                        # MAX_WIDTH. A tap retreats; a turn times out here.
    PEAK_DROP = 0.08   # deg; |yaw| dropping this far below its running max
                        # locks the first local peak (noise dead-band). A bit
                        # wider than 0.05 so a noisy climb locks its peak
                        # instead of dragging tap_peak_t out to the timeout.
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
                         # only during a pulse (and the refractory after) --
                         # NOT for the whole sequence: the head drifts in
                         # the gaps between taps, and a frozen reference
                         # turned tap 3 into a slow ramp off a stale zero.
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
