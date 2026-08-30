#!/usr/bin/env bash
# Grant this machine's users USB access to the VITURE glasses, so Refract's
# head-tracking SDK and hardware controls (brightness/volume/film/SBS
# switch) work without needing XRLinuxDriver or Breezy Desktop installed.
#
# install.sh is deliberately root-free (see its header comment) -- this is
# the one piece of setup that genuinely needs it, so it stays a separate,
# explicit step rather than a surprise sudo prompt in the middle of install.sh.
#
#   tools/install-udev-rule.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULE_SRC="$REPO/udev/99-refract-xr.rules"
RULE_DEST="/etc/udev/rules.d/99-refract-xr.rules"

if [ ! -f "$RULE_SRC" ]; then
  echo "  missing $RULE_SRC -- run this from a Refract checkout" >&2
  exit 1
fi

echo "  installing $RULE_DEST (needs sudo)"
sudo install -Dm644 "$RULE_SRC" "$RULE_DEST"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb
echo "  done. Unplug and replug the glasses' USB cable for it to take effect"
echo "  on the device that's already attached (new attaches pick it up"
echo "  immediately)."
