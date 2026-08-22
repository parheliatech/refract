"""Refract -- shell application for the VITURE Pro XR glasses on Linux.

One persistent GL process owns the glasses (IMU, hardware control, SBS mode)
and hosts sub-experiences as in-process scenes: Desk, 360, Play, TAK.
See DEVELOPMENT_PLAN.md for the architecture decisions and phase plan.
"""

__version__ = "0.1.2"
