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


def _set_imu_aux(app, on):
    if app.head and app.head.v:
        app.head.set_aux(bool(on))


def _set_predict(app, ms):
    if app.head:
        app.head.predict_s = max(0.0, float(ms or 0)) / 1000.0


def _set_recenter_after(app, secs):
    app.recenter_after = float(secs)


def _recenter_now(app):
    app.recenter()


def global_schema(app):
    has_imu = bool(app.head and app.head.v)
    hud_keys = ", ".join(app.hud.combo_names) if app.hud else "-"
    return [
        Setting("IMU rate", ENUM, key="imu_rate", default=240,
                options=((60, "60 Hz"), (90, "90 Hz"), (120, "120 Hz"),
                         (240, "240 Hz")),
                on_change=_set_imu_rate, available=has_imu),
        # the glasses' extended report (msgId 0x53): raw accelerometer +
        # gyro, which the reliable temple-tap detector needs. Head tracking
        # is the same either way.
        Setting("Accelerometer stream", BOOL, key="imu_aux", default=False,
                on_change=_set_imu_aux, available=has_imu),
        # render where the head will be when the frame is seen; ~30 ms is
        # typical (one or two vsyncs), too much overshoots a stop
        Setting("Motion prediction", ENUM, key="predict_ms", default=0,
                options=((0, "off"), (15, "15 ms"), (30, "30 ms"),
                         (45, "45 ms")),
                on_change=_set_predict, available=has_imu),
        Setting("Recenter countdown", FLOAT, key="recenter_after",
                default=4.0, lo=3.0, hi=60.0, step=1.0, unit=" s",
                on_change=_set_recenter_after),
        Setting("Recenter now", ACTION, run=_recenter_now, available=has_imu),
        Setting("HUD key", INFO, text=hud_keys),
        Setting("Triple head-bob opens HUD", BOOL, key="head_bob",
                default=True, available=has_imu),
        # right temple -> HUD, left temple -> recenter
        Setting("Temple-tap (R: HUD, L: recenter)", BOOL,
                key="temple_tap", default=True, available=has_imu),
        # 3 is harder to trigger by accident. Applied at startup.
        Setting("Taps per temple gesture", ENUM, key="temple_tap_count",
                default=3, options=((2, "2 taps"), (3, "3 taps")),
                available=has_imu),
        # Read-only: libglasses can be opened next to the running IMU, but
        # every USB command then fails (initialize() still reports success).
        # The way in is the public SDK's mcu_with_rsp, on the client that
        # already owns the device.
        Setting("Brightness", INFO,
                text="blocked while head tracking runs"),
        Setting("Volume", INFO, text="blocked while head tracking runs"),
        Setting("Display Handoff", INFO, text="refract.ctl handoff"),
    ]
