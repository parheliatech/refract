"""Vehicle motion compensation -- PROTOTYPE, not yet wired into Desk.

The glasses' IMU cannot tell "I turned my head" from "the vehicle I'm
sitting in turned under me" -- both are just a rotation to it. Riding a bus
that turns a corner reads exactly like a head turn, and Desk pans with it.

GPS is not the fix: heading derived from position deltas needs continuous
forward motion to mean anything, updates at only 1-10 Hz with real lag, and
says nothing while turning at low speed or stopped. What actually works is a
SECOND rotation source that moves with the vehicle but not with your head --
something sitting on your lap or a tray, not on your face -- whose rotation
gets subtracted from the glasses' rotation before Desk renders.

No extra hardware needed for a first version: most laptops from the last
decade, this one included, have their own built-in accelerometer and
gyroscope (Intel calls theirs the "Integrated Sensor Hub"), exposed by Linux
through the IIO subsystem at /sys/bus/iio/devices -- world-readable, no
udev rules, no root. Confirmed present and live on the dev machine
(2026-08-22): reading .../gyro_3d/in_anglvel_z_raw twice a beat apart
returns a different number each time while the laptop just sits on a desk.
If the laptop rides securely on your lap or a tray (not wobbling on its
own), its own gyro IS the vehicle-motion signal.

WHAT THIS COMPENSATES: YAW ONLY -- turning left/right. That's the reported
problem (a bus turning a corner), and it's also all `desk.yaw_only` already
lets through from the glasses, so the two line up cleanly. Compensating
pitch and roll too (the bus tilting into a turn, going up a hill) would need
knowing which way the laptop is resting at all times, which needs a real
sensor-fusion filter (accelerometer + gyro, blended) -- out of scope for a
first prototype.

WHY THIS DRIFTS, AND WHY THAT'S FINE HERE. A gyroscope reports ANGULAR
VELOCITY, not an angle -- recovering an angle means summing (velocity x
time) every sample, and any small zero-rate error the sensor has (all MEMS
gyros have one) accumulates into a slow false rotation over time.
`LaptopIMU.calibrate()` measures and removes that error while the laptop
sits still, which mostly fixes it, but not perfectly forever -- so this is
built to be RECENTERED periodically (alongside the headset, say), not
trusted as an all-day absolute reference.

Split in two, the same shape as `gesture.HeadBob`: `YawIntegrator` is pure
math fed (time, angular-rate) samples, so it is regression-tested without
any hardware; `LaptopIMU` is the thin layer that actually reads sysfs.
"""

import glob
import math
import os
import time

AXES = ("x", "y", "z")


class YawIntegrator:
    """Pure math: turns a stream of angular-rate readings (radians/second)
    into an accumulated angle (degrees). No hardware, no I/O -- feed it
    samples, read `.yaw_deg`, exactly the shape `tests/selftest.py` drives
    with synthetic data.
    """

    def __init__(self):
        self.bias = 0.0        # rad/s -- the sensor's own rest-state error
        self.yaw_deg = 0.0
        self.last_t = None

    def calibrate(self, rate_samples):
        """rate_samples: rad/s readings taken while the sensor was still.
        Their mean becomes the bias subtracted from every future sample.
        False (does nothing) if given no samples."""
        samples = list(rate_samples)
        if not samples:
            return False
        self.bias = sum(samples) / len(samples)
        self.reset()
        return True

    def reset(self):
        self.yaw_deg = 0.0
        self.last_t = None

    def update(self, t, rate_rad_s):
        """Feed one (time, rate) sample; returns the running total."""
        if self.last_t is not None:
            dt = max(0.0, t - self.last_t)
            self.yaw_deg += math.degrees((rate_rad_s - self.bias) * dt)
        self.last_t = t
        return self.yaw_deg


def _read(path):
    with open(path) as f:
        return f.read().strip()


def _find_device(kind):
    """First IIO device whose `name` file matches, e.g. "gyro_3d" or
    "accel_3d". None if this machine has no such sensor -- most laptops do,
    but not all, and a virtual machine or desktop tower certainly won't."""
    for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        try:
            if _read(os.path.join(d, "name")) == kind:
                return d
        except OSError:
            continue
    return None


def read_vec(devdir, kind):
    """kind is "anglvel" (rad/s) or "accel" (m/s^2). Returns (x, y, z) in
    physical units -- IIO reports raw integers plus a scale factor, and the
    conversion is the same shape for both channel types."""
    scale = float(_read(os.path.join(devdir, "in_%s_scale" % kind)))
    return tuple(
        int(_read(os.path.join(devdir, "in_%s_%s_raw" % (kind, ax)))) * scale
        for ax in AXES)


class LaptopIMU:
    """The laptop's own built-in motion sensor, read through Linux's IIO
    subsystem. `.available` is False on a machine with no such sensor --
    every method is then a harmless no-op, so code using this does not need
    a separate "do we have one" branch everywhere.
    """

    def __init__(self):
        self.gyro_dir = _find_device("gyro_3d")
        self.accel_dir = _find_device("accel_3d")
        self.available = bool(self.gyro_dir)
        self.axis = 2           # which raw gyro axis is "up"; found below
        self.sign = 1.0
        self.integrator = YawIntegrator()
        self.calibrating = False
        self.calibrated = False
        self._cal_samples = []
        self._cal_until = 0.0

    def _pick_vertical_axis(self):
        """Reads the accelerometer ONCE to find which raw gyro axis is
        closest to vertical -- the one gravity is mostly pointing along
        right now. Only meaningful while the laptop is still and not
        accelerating, which is also when calibration calls this."""
        if not self.accel_dir:
            return
        vec = read_vec(self.accel_dir, "accel")
        self.axis = max(range(3), key=lambda i: abs(vec[i]))
        self.sign = 1.0 if vec[self.axis] >= 0 else -1.0

    def begin_calibrate(self, seconds=2.0):
        """Start a NON-BLOCKING calibration: call poll_calibrate() once a
        frame until it returns True. Safe to call from a render loop, unlike
        calibrate() below -- it never sleeps."""
        if not self.available:
            return False
        self._pick_vertical_axis()
        self._cal_samples = []
        self._cal_until = time.time() + seconds
        self.calibrating = True
        self.calibrated = False
        return True

    def poll_calibrate(self, log=print):
        """Call once per frame while `calibrating` is True. Returns True the
        frame calibration finishes (check `.calibrated` for success), False
        while still collecting -- a caller can just do
        `if self.calibrating and imu.poll_calibrate(): ...`."""
        if not self.calibrating:
            return False
        self._cal_samples.append(
            read_vec(self.gyro_dir, "anglvel")[self.axis] * self.sign)
        if time.time() < self._cal_until:
            return False
        self.calibrating = False
        self.calibrated = self.integrator.calibrate(self._cal_samples)
        if self.calibrated:
            log("  vehicle imu  : calibrated on axis %s, bias %.4f rad/s "
                "(%d samples)"
                % (AXES[self.axis], self.integrator.bias,
                   len(self._cal_samples)))
        return True

    def calibrate(self, seconds=2.0, log=print):
        """Blocking convenience wrapper for standalone scripts ONLY -- it
        sleeps for `seconds`. Never call this from inside Desk's render
        loop; use begin_calibrate()/poll_calibrate() there instead."""
        if not self.begin_calibrate(seconds):
            return False
        while not self.poll_calibrate(log=log):
            time.sleep(0.02)
        return self.calibrated

    def recenter(self):
        self.integrator.reset()

    def update(self, t=None):
        """Call every frame (or every render tick). Returns the vehicle's
        accumulated yaw in degrees since the last calibration/recenter().
        A no-op (returns the last value) while calibrating -- calibration
        reads its own samples so the two do not interleave."""
        if not self.available or self.calibrating:
            return self.integrator.yaw_deg
        t = time.time() if t is None else t
        rate = read_vec(self.gyro_dir, "anglvel")[self.axis] * self.sign
        return self.integrator.update(t, rate)
