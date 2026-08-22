#!/usr/bin/env bash
# Import libglasses.so (and the libraries it needs) into a location Refract
# owns, so the hardware controls do not depend on another project's install
# directory being present.
#
#   tools/import-glasses-sdk.sh [SOURCE_DIR]
#
# SOURCE_DIR defaults to XRLinuxDriver's install, which is the only Linux
# distribution channel for this library:
#
#   ~/.local/share/xr_driver/lib
#
# WHY THIS IS NOT IN THE REPO. libglasses.so is VITURE's proprietary
# library, shipped inside someone else's installer. Redistributing it is not
# ours to do -- the same reason the i3d vendor assets are gitignored -- so it
# is copied per-machine instead of committed. It also drags in the bundled
# OpenCV 4.2 sonames it NEEDs, which is why the whole directory comes across
# rather than one file.
#
# What actually depends on this: brightness, volume, electrochromic film,
# duty cycle and the SBS dimension switch (refract/core/hardware.py and
# viture-hw.py). Head tracking does NOT -- that runs on the public VITURE
# SDK vendored in sdk/. Phase 5 step 4b aims to remove this dependency
# entirely by driving those controls through the public SDK client.
set -euo pipefail

SRC="${1:-$HOME/.local/share/xr_driver/lib}"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/refract/sdk"

if [ ! -f "$SRC/libglasses.so" ]; then
  echo "  no libglasses.so in: $SRC" >&2
  echo "  Pass the directory that holds it, e.g. an XRLinuxDriver install:" >&2
  echo "      tools/import-glasses-sdk.sh ~/.local/share/xr_driver/lib" >&2
  exit 1
fi

mkdir -p "$DEST"
cp -a "$SRC/." "$DEST/"
echo "  imported $(du -sh "$DEST" | cut -f1) into $DEST"
echo "  libglasses.so $(sha256sum "$DEST/libglasses.so" | cut -c1-16)..."
echo
echo "  Refract now finds it without XRLinuxDriver installed. Verify with:"
echo "      .venv/bin/python viture-hw.py --status"
