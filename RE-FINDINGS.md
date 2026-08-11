# VITURE Pro XR — hardware interface notes

Reference notes on how the Pro XR glasses are driven, gathered while working
out how to make them function on Linux/x86_64. Kept because they explain
the wire protocol, the device's capabilities and its limits.

## Stack layering

```
SpaceWalker Java/Kotlin app  (12,222 classes, Hilt DI, GeckoView UI)
        │  JNI
viture.glasses.jni.GlassesBridge      ← complete API contract, see below
        │
libglasses-jni.so      (59 KB — thin JNI shim, clean unmangled C ABI)
        │
libglasses-internal.so (2 MB — protocol; STATICALLY LINKS libusb + hidapi)
        │                         talks /dev/bus/usb + HID
        ├── libcarina_vio.so   (14.7 MB — `carina_a1088_viture_*` VIO engine)
        ├── libSlam.so         (4.2 MB) + libslam_pose.so (`VitureSlam*`)
        └── libcloud_protocol.so (13.9 MB)
```

## The single most important finding

`libglasses-internal.so` statically links **libusb** (with hotplug) and **hidapi**,
and drives the device through **`/dev/bus/usb`** — usbfs. Confirmed by strings:

```
/dev/bus/usb            /dev/bus/usb/%03u
libusb_claim_interface  libusb_control_transfer
libusb_detach_kernel_driver     LIBUSB_ERROR_ACCESS ...
hid_init                AccessoryDeviceProvider: failed to open HID device (product_id=0x%04X)
```

Android and Ubuntu expose **the identical kernel interface** here. The transport
layer is not Android-specific at all. The only real difference is fd acquisition:
Android needs `UsbManager` permission to hand over an fd; on Linux you open
`/dev/bus/usb` directly with a udev rule. **The device-control protocol is
directly portable.**

## Device identity

- Vendor ID: **0x35CA** (13770) — VITURE
- Camera module VID **0x0C45** (3141, Sonix) — explicitly skipped by GlassManager
- Microphone: 0x35CA:**0x1102**
- Glasses PID family observed:
  `0x1000, 0x1011, 0x1013, 0x1015, 0x1017, 0x1019, 0x101B, 0x101D,
   0x1101, 0x1104, 0x1121, 0x1131, 0x1141, 0x1151, 0x1201, 0x1211, 0x1301, 0x4000`
- Feature probes in `GlassManager`: `hasEnhancedUIGlassConnected`,
  `hasUltraWideGlassConnected`, `hasR6GlassConnected` — PID-gated capability tiers.

## Three hardware generations

`viture.glasses.type.DeviceType`:

| Const | Value | Notes |
|---|---|---|
| `VITURE_GEN1` | 0 | 3DoF IMU |
| `VITURE_GEN2` | 1 | 3DoF IMU |
| `VITURE_CARINA` | 2 | **6DoF**, stereo cameras, VIO/SLAM |

`CarinaDeviceProvider initialized (6DOF=%s)` — 6DoF is Carina-only.
Carina camera callback is **stereo**:
`Carina.Camera.onCamera(byte[] left, byte[] right, double timestamp, int w, int h)`

## The master key: generic USB passthrough

```java
public static native int    nativeExecuteUsbCommand(int, byte[], int);
public static native byte[] nativeExecuteUsbCommandWithResponse(int, byte[], int);
```

Every high-level operation is built on this one primitive. Recovering the opcode
table for it documents the entire protocol — this is the highest-value RE target.

## Control surface (`xr_device_provider_*`, all clean C ABI)

Display: `get/set_display_mode`, `native_get/set_display_mode`,
`native_get/set_display_distance`, `native_get/set_display_size`,
`native_get/set_side_mode`, `get/set_duty_cycle`, `get/set_film_mode`,
`switch_dimension`, `set_default_display_mode`

Legacy display modes (`GlassesConstantsLegacy`):
`MODE_3840_1080_60HZ_120HZ = 50`, `MODE_1920_1080_60HZ_120HZ = 54`,
`MODE_ULTRAWIDE_60HZ_120HZ = 81`, `MODE_SIDEMODE_60HZ_120HZ = 97`

Tracking: `open/close_imu`, `native_get/set_dof`, `native_recenter_dof`,
`get_gl_pose_carina`, `get_imu_pose_carina`, `reset_pose_carina`,
`reset_origin_carina`, `register_imu_raw_callback`, `register_imu_pose_callback`

Device: `get/set_brightness_level`, `get/set_volume_level`, `get_wear_status`,
`get_device_type`, `get_market_name`, `get_glasses_version`, `get_dp_version`,
`get_osd_version`, `get_button_data`, `get_calibration_data_internal`,
`set_led_mode`, `set_oled`, `get/set_host_timestamp`, `set_host_device_type`

Camera: `xr_camera_provider_{create,start,stop,destroy,is_streaming,
is_valid_camera,get_camera_vid,get_camera_pid}`
Exposure: `set_auto_exposure_carina`, `set_manual_exposure_carina`

Accessory (neckband/dock): `xr_accessory_provider_{create,initialize,start,stop,
shutdown,destroy,get_mcu_version,get_soc,get_voltage,get_current,get_temperature,
enter_standby}`

SLAM (`libslam_pose.so`, clean C ABI):
`VitureSlamCreateHandler/Initialize/SetMode/Start/ProcessImuData/GetPose/
Recenter/Stop/Terminate/DestroyHandler` + callback registration for pose,
static gyro bias, Carina config, device info, custom file load/save.

## Portability assessment per subsystem

| Subsystem | Portable to x86 Linux? | Notes |
|---|---|---|
| USB/HID transport | **Yes, directly** | Same usbfs/hidraw; needs udev rule only |
| Device control (brightness, display modes, IMU enable, side/ultrawide) | **Yes** | Pure protocol; reimplement over libusb. Highest value/effort ratio |
| 3DoF IMU pose | **Yes** | Protocol + sensor fusion; XRLinuxDriver already does VITURE 3DoF |
| Depth model (`depth_anything_fp16_640_360.tflite`) | **Yes, as-is** | Standard TFLite, runs on x86 via LiteRT/ONNX. **Not** NPU-locked |
| Qualcomm QNN (`libQnn*`) | **No** | Hexagon DSP silicon required; no `libQnnCpu.so` bundled. But it is only an *accelerator* for the TFLite model above — the model itself is the portable path |
| Carina 6DoF VIO/SLAM | **Not by reuse** | arm64 blobs, bionic-linked. Must reimplement or substitute |
| Chaquopy Python (yt-dlp) | **Trivial** | `pip install yt-dlp` |
| GeckoView UI | N/A | Replace with any Linux browser/compositor surface |

Note `libSlam.so` needs only `liblog/libdl/libm/libc` — the thinnest Android
coupling of any blob. `libcarina_vio.so` additionally pulls `libmediandk` and
`libcloud_protocol`, so it is far more entangled.

## Why not just run the .so files

They are `arm64-v8a` ELF linked against **bionic**, not glibc. Host is x86_64.
That needs qemu-user *plus* a bionic loader — fragile, and it would sit in the
hot path of a 60–120 Hz tracking loop. Reimplementing the protocol (which is
plain libusb) is both easier and faster than emulating it.

## LIVE HARDWARE (confirmed 2026-08-10)

**VITURE Pro XR Glasses — `35ca:101d`** (PID 0x101D matches the vendor's own table).
Serial 206E30534742, bcdDevice 0200, USB 2.01 @ 12 Mbps, sysfs `1-9`.
This is a **GEN1/GEN2 class device: 3DoF IMU, no cameras** → the Carina 6DoF
path is not applicable to this hardware.

USB interface layout:

| If | Class | Endpoints | Driver | Node |
|----|-------|-----------|--------|------|
| 00 | HID (0x03) | ep_01 OUT, ep_81 IN | usbhid | `/dev/hidraw2` |
| 01 | HID (0x03) | ep_02 OUT, ep_82 IN | usbhid | `/dev/hidraw3` |
| 02 | CDC-ACM (0x02) | ep_86 | cdc_acm | `/dev/ttyACM0` |
| 03 | CDC-data (0x0a) | ep_03, ep_85 | cdc_acm | `/dev/ttyACM0` |

**Both HID interfaces expose an identical vendor-defined descriptor:**

```
06 00 ff    Usage Page (Vendor 0xFF00)
09 01       Usage (0x01)
a1 01       Collection (Application)
09 01  15 00  26 ff 00  95 40  75 08  81 02    Input  (64 bytes, 8-bit)
09 01  15 00  26 ff 00  95 40  75 08  91 02    Output (64 bytes, 8-bit)
c0          End Collection
```

64-byte in/out vendor pipes on both — the classic control-channel + IMU-stream
pair. This maps directly onto `nativeExecuteUsbCommand(int channel, byte[], int)`:
the leading `int` is almost certainly the channel/interface selector.

**Permissions are already fine** — `/dev/bus/usb/001/010`, `/dev/hidraw2` and
`/dev/hidraw3` are all rw for the login user via logind `uaccess`. **No udev rule
is needed** on this system. (`/dev/ttyACM0` is `root:dialout` and would need
group membership, but the HID path is the one that matters.)

Display: `card1-DP-1 connected` — glasses attached as a DisplayPort output
alongside `eDP-1`.

**Passive listen result:** 0 reports on both hidraw nodes over 4 s. The IMU is
silent until explicitly enabled, confirming that `open_imu` must be issued
before any pose data flows.

### Safety note for Phase 1

Do **not** brute-force opcodes against the device. Recover the command bytes
statically from `libglasses-internal.so` (inspect around
`xr_device_provider_open_imu`, `..._set_brightness_level`,
`..._native_set_display_mode`) and only then transmit known-correct frames.
Blind writes to a 64-byte vendor HID pipe risk hitting firmware-update or
calibration paths.

## ⚠️ PLAN CHANGE: an official VITURE x86_64 Linux SDK exists

Most of the above turned out to be **unnecessary**. VITURE ships a native
Linux x86_64 SDK, already vendored inside XRLinuxDriver:

```
XRLinuxDriver/lib/x86_64/viture/libglasses.so     467 KB, ELF x86-64
XRLinuxDriver/lib/x86_64/viture/libcarina_vio.so   58 MB, ELF x86-64
XRLinuxDriver/include/sdks/viture_protocol.h      documented, "© 2025 VITURE Inc."
```

**Verified: it dlopens natively on this laptop** with every needed entry point
present (`set_display_mode`, `switch_dimension`, `set_brightness_level`,
`set_volume_level`, `set_display_size`, `set_film_mode`,
`execute_usb_command`, `open_imu`, …). Requires `LD_LIBRARY_PATH` pointing at
the bundled OpenCV 4.2 set.

Earlier note said VITURE support looked aarch64-only — wrong; `lib/x86_64/viture/`
does exist. CMake gates on `if(EXISTS ${VITURE_LIB_DIR})` and it is satisfied
on x86_64.

### The RE cross-validated the SDK exactly

Values inferred from the ARM libraries, before the official header surfaced:

| Inferred | Official header | Match |
|---|---|---|
| mode `0x31` = 2D | `MODE_1920_1080_60HZ = 0x31` | ✅ |
| mode `0x32` = 3D/SBS | `MODE_3840_1080_60HZ = 0x32` | ✅ |
| valid mask `0x1f001f` → `0x31–0x35`, `0x41–0x45` | documented `0x31–0x36`, `0x41–0x46` | ✅ |
| PID `0x101D` → legacy command id `8` (vs `0x141`) | — (internal) | RE-only |

So the RE remains useful as validation and as the fallback for anything the
public SDK does not expose.

### Official constants (from `viture_protocol.h`)

DisplayMode: `0x31` 1920×1080@60 · `0x32` **3840×1080@60 (SBS 3D)** · `0x33`
1920×1080@90 · `0x34` 1920×1080@120 · `0x35` 3840×1080@90 · `0x36`
1920×1080 60→120 interpolated · `0x41`–`0x46` the same set at 1920×1200 ·
`0x51` ultrawide · `0x61` side-by-side@60

DisplaySize: SMALL 0 · MEDIUM 1 · LARGE 2 · EXTRA 3 · ULTRA 4
NativeDOF: 0 none · 1 native 3DoF · 2 smooth-follow
DutyCycle: H 98 · M 42 · L 30
IMU mode: RAW 0 · POSE 1;  frequency: 60/90/120/240/500 Hz = 0/1/2/3/4
State callbacks: BRIGHTNESS 0 · VOLUME 1 · DISPLAY_MODE 2 ·
ELECTROCHROMIC_FILM 3 · NATIVE_DOF 4

**Viture Pro ranges:** brightness [0,6], volume [0,8], electrochromic film [0,1].

### What XRLinuxDriver leaves on the table

`src/devices/viture.c` (754 lines) calls only ~15 of the 38 exported
`xr_device_provider_*` functions. It **never calls**:

`set_brightness_level` · `get_brightness_level` · `set_volume_level` ·
`get_volume_level` · `set_display_size` · `get_display_size` ·
`set_display_distance` · `get_display_distance` · `set_duty_cycle` ·
`set_film_mode` (electrochromic dimming) · `execute_usb_command`

That is precisely the gap you noticed between Breezy and SpaceWalker. **These
features are not missing from Linux — they are unexposed.** The work is a thin
control layer over an SDK that already ships them, not a protocol reimplementation.

## Host constraints

Intel i7-7600U (Kaby Lake, 2-core/4-thread, 2017), no NPU. Device control and
3DoF are trivially within budget. Running a full VIO plus a depth model at frame
rate on this CPU is the real performance risk, independent of any RE difficulty.
