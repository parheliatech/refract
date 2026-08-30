#!/usr/bin/env python3
"""Try out the laptop's own motion sensor as a vehicle-motion reference.

    .venv/bin/python tools/vehicle-imu-probe.py

Does not touch the glasses or Refract's config -- it only reads the
laptop's built-in gyroscope/accelerometer (see refract/core/vehicle.py for
what these are and why) and prints a running yaw number, so you can see for
yourself whether it tracks rotation cleanly before it's wired into Desk.

Try this: run it, keep the laptop still through the calibration countdown,
then either spin your desk chair with the laptop on your lap, or -- the
real test -- take it for a ride and watch the number move as the vehicle
turns corners.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from refract.core.vehicle import LaptopIMU                # noqa: E402


def main():
    print(__doc__.split("\n\n")[0])

    imu = LaptopIMU()
    if not imu.available:
        sys.exit("  no built-in gyroscope found on this machine (checked "
                 "/sys/bus/iio/devices for a 'gyro_3d' sensor) -- this "
                 "prototype needs one, or a USB IMU wired in the same way")

    print("\n  keep the laptop PERFECTLY STILL for 2 seconds -- calibrating...")
    if not imu.calibrate(seconds=2.0):
        sys.exit("  calibration got no samples -- is the sensor really there?")

    print("\n  tracking yaw. Turn the laptop (or your chair, or the car) "
          "and watch it move.")
    print("  Ctrl-C to stop, 'r' + Enter has no effect here -- just quit and "
          "rerun to recalibrate.\n")

    try:
        while True:
            yaw = imu.update()
            print("\r  vehicle yaw: %+7.1f deg" % yaw, end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
