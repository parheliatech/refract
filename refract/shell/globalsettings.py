"""Global Settings: device-wide preferences, in exactly ONE place.

Owned by the shell, not by any container -- these apply whichever
sub-experience is running, and the concept spec is explicit that they must
not be duplicated or scattered across containers.

Built fresh on each open so availability tracks reality (no IMU running
means the IMU rows are inert rather than lying).
"""

from refract.core.settings import (ACTION, BOOL, ENUM, FLOAT, INFO, Setting)
from refract.core.viture_sdk import FQ


def _set_imu_rate(app, hz):
    if app.head and app.head.v:
        try:
            app.head.v.lib.set_imu_fq(FQ[int(hz)])
        except Exception as e:                          # noqa: BLE001
            print("  imu rate failed: %s" % e, flush=True)


def _set_recenter_after(app, secs):
    app.recenter_after = float(secs)


def _recentre_now(app):
    app.recenter()


def _calibrate(app):
    from refract.shell.calibrate import CalibrationScene
    app.hud.close()
    app.push(CalibrationScene())


def global_schema(app):
    has_imu = bool(app.head and app.head.v)
    hud_keys = ", ".join(app.hud.combo_names) if app.hud else "-"
    return [
        Setting("IMU rate", ENUM, key="imu_rate", default=120,
                options=((60, "60 Hz"), (90, "90 Hz"), (120, "120 Hz"),
                         (240, "240 Hz")),
                on_change=_set_imu_rate, available=has_imu),
        Setting("Recentre countdown", FLOAT, key="recenter_after",
                default=25.0, lo=3.0, hi=60.0, step=1.0, unit=" s",
                on_change=_set_recenter_after),
        Setting("Recentre now", ACTION, run=_recentre_now, available=has_imu),
        Setting("Calibrate axes", ACTION, run=_calibrate, available=has_imu),
        Setting("HUD key", INFO, text=hud_keys),
        Setting("Triple head-bob opens HUD", BOOL, key="head_bob",
                default=True, available=has_imu),
        # Read-only, and now for a MEASURED reason (plan phase 3 step 5):
        # libglasses can be opened alongside a running IMU without crashing,
        # but every USB command then fails (-1 on send, -3/timeout back), so
        # it reads nothing and writes nothing. Note initialize() still
        # reports SUCCESS -- never trust it as proof the device is usable.
        # The remaining route is the public SDK's own mcu_with_rsp escape
        # hatch, on the client that already owns the device (phase 5).
        Setting("Brightness", INFO,
                text="blocked while head tracking runs"),
        Setting("Volume", INFO, text="blocked while head tracking runs"),
        Setting("Display Handoff", INFO, text="arrives in phase 5"),
    ]
