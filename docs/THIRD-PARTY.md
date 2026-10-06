# Third-party software and licences

Refract itself is MIT-licensed (`LICENSE`). It loads or bundles the
components below. The Refract HUD's About page (Global Settings → About)
summarises this list.

"Status" says how sure we are of the terms, because some of VITURE's
components have no separately published licence.

## VITURE components

| Component | Where | Terms | Status |
|---|---|---|---|
| **VITURE One Linux SDK 1.0.7** (`libviture_one_sdk.so`) | downloaded by `install.sh` from `static.viture.dev` into `sdk/libs/`, pinned by SHA-256; not in the repository | [VITURE SDK License Agreement](https://www.viture.com/viture-sdk-license-agreement) (effective Sept 2025): object-code use and distribution only as part of an application, VITURE's notices retained, no reverse engineering of the SDK, no publishing it for others to copy | Terms read from the published agreement. Whether the 1.0.7 download (June 2024) was issued under that exact text is not known |
| **libglasses.so** (`xr_device_provider_*`: brightness, volume, film, screen size) | `sdk/libglasses/`, in the repository | VITURE's library; shipped inside XRLinuxDriver. No separate public download or licence found | **Unresolved** -- check before distributing outside this repository |
| **libcarina_vio.so** | `sdk/libglasses/`, in the repository | VITURE's library that `libglasses.so` links against | **Unresolved**, as above |

Strings inside those binaries suggest they embed hidapi, libusb, flatbuffers
and zlib (and a TLS/crypto library in `libcarina_vio.so`); their notices
would travel with the binaries if VITURE publishes them.

## OpenCV 4.2

`sdk/libglasses/libopencv_*.so.4.2` -- the OpenCV libraries `libglasses.so`
needs. OpenCV 4.2 is under the BSD 3-Clause licence (<https://opencv.org/license/>),
which requires its copyright notice and licence text to accompany
redistributed binaries. **That text is not yet included here.**

## System and Python dependencies (not bundled)

| Component | Licence |
|---|---|
| GTK 4, PyGObject, GStreamer, PipeWire client libraries | LGPL-2.1+ |
| moderngl, glcontext | MIT |
| NumPy | BSD-3-Clause |
| Pillow | HPND |

## Not shipped

`i3d/shaders/` and `i3d/model/` are VITURE's, recovered from the SpaceWalker
app; they are gitignored and not redistributed (see `.gitignore`).
