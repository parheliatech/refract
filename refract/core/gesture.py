"""Head gestures -- input that needs no keyboard.

The HUD's key combination turned out to be unreachable: GNOME swallows both
Ctrl+Super+R and Ctrl+Alt+R before a fullscreen GLFW window ever sees them.
A gesture sidesteps the compositor entirely, and a wearer already has their
head on; asking them to find a key combo they cannot see is the sort of
thing this product exists to avoid.

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
