#!/usr/bin/env bash
# Refract installer -- user-level, no root, no system files touched.
#
#   ./install.sh                 install (or refresh) everything
#   ./install.sh --uninstall     remove the launcher, icons and desktop entry
#   ./install.sh --with-i3d      also install the 2D->3D research extras
#
# Installs into ~/.local: a `refract` launcher, a desktop entry with a
# prism icon, and the icon in the hicolor theme. The repo stays where it is
# and the venv lives inside it, so moving the repo means re-running this.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor"
DESKTOP="$APPS/refract.desktop"
LAUNCHER="$BIN/refract"
VENV="$REPO/.venv"
PY="$VENV/bin/python"
WITH_I3D=0

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m!!\033[0m   %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

uninstall() {
  echo; say "removing Refract from ~/.local (the repo and venv stay put)"
  rm -f "$DESKTOP" "$LAUNCHER"
  rm -f "$ICONS"/scalable/apps/refract.svg
  for s in 16 24 32 48 64 128 256 512; do
    rm -f "$ICONS/${s}x${s}/apps/refract.png"
  done
  command -v update-desktop-database >/dev/null && \
    update-desktop-database "$APPS" 2>/dev/null || true
  command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  ok "removed. To delete everything: rm -rf $REPO"
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall ;;
    --with-i3d)  WITH_I3D=1 ;;
    -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

echo
say "Refract installer"
say "repo: $REPO"
echo

# ---------------------------------------------------------------- checks
command -v python3 >/dev/null || die "python3 not found"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'EOF' || die "Python 3.10+ required (found $PYVER)"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
ok "python $PYVER"

# PyGObject and GStreamer come from the SYSTEM, not pip: the venv is built
# with --system-site-packages precisely so they are visible. Missing them is
# the most common reason Desk comes up black.
python3 - <<'EOF' 2>/dev/null || die "PyGObject/GStreamer missing. Install: sudo apt install python3-gi gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-good gstreamer1.0-pipewire"
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, Gst          # noqa: F401
EOF
ok "PyGObject + GStreamer"

# The window is GTK 4 (a Gtk.GLArea), also from the system.
python3 - <<'EOF' 2>/dev/null || die "GTK 4 bindings missing. Install: sudo apt install gir1.2-gtk-4.0"
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk               # noqa: F401
EOF
ok "GTK 4"

# ------------------------------------------------------------ VITURE SDK
# Head tracking and the 2D/3D switch run on VITURE's public Linux SDK. It is
# VITURE's to distribute, not ours, so it is fetched from VITURE rather
# than carried in this repo, and pinned by hash: anything else is refused.
# Using it means accepting VITURE's SDK License Agreement.
. "$REPO/packaging/sdk-pin.sh"      # SDK_VERSION, SDK_URL, SDK_SHA256
SDK_LIB="$REPO/sdk/libs/libviture_one_sdk.so"
if [ -f "$SDK_LIB" ]; then
  ok "VITURE SDK 1.0.7 (already downloaded)"
else
  say "downloading the VITURE Linux SDK 1.0.7 from viture.dev"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  if command -v curl >/dev/null; then
    curl -fsSL --retry 2 -o "$tmp/sdk.tar.xz" "$SDK_URL" || true
  elif command -v wget >/dev/null; then
    wget -q -O "$tmp/sdk.tar.xz" "$SDK_URL" || true
  else
    die "need curl or wget to download the VITURE SDK"
  fi
  [ -s "$tmp/sdk.tar.xz" ] || die "could not download $SDK_URL -- are you online?"
  echo "$SDK_SHA256  $tmp/sdk.tar.xz" | sha256sum --quiet -c - \
    || die "the downloaded SDK does not match the expected hash -- refusing it"
  tar -xJf "$tmp/sdk.tar.xz" -C "$tmp"
  src="$tmp/viture_one_linux_sdk_1.0.7"
  install -Dm755 "$src/libs/libviture_one_sdk.so" "$SDK_LIB"
  install -Dm644 "$src/include/viture.h" "$REPO/sdk/include/viture.h"
  rm -rf "$tmp"; trap - EXIT
  ok "VITURE SDK 1.0.7 -> sdk/libs/ (VITURE's licence:"
  say "     https://www.viture.com/viture-sdk-license-agreement)"
fi

if [ "${XDG_SESSION_TYPE:-}" != "wayland" ]; then
  warn "session is '${XDG_SESSION_TYPE:-unknown}', not wayland -- virtual monitors need GNOME/Mutter on Wayland"
else
  ok "wayland session"
fi

if ! python3 -c 'import sys;sys.exit(0)' 2>/dev/null; then die "python broken"; fi
if command -v gnome-shell >/dev/null; then
  ok "GNOME $(gnome-shell --version 2>/dev/null | awk '{print $3}')"
else
  warn "gnome-shell not found -- Refract drives Mutter directly and needs GNOME"
fi

if lsusb 2>/dev/null | grep -qi '35ca:'; then
  ok "VITURE glasses detected on USB"
  # Seeing the device (lsusb only needs read access) is not the same as
  # being able to USE it -- the SDK claims the interface, which needs write
  # access too. A bare kernel default (root:root) sees the device but fails
  # every init() with a generic "are the glasses plugged in?", which is
  # confusing when they plainly are. This used to come for free from
  # XRLinuxDriver's own udev rule; Refract now ships its own instead of
  # depending on that.
  USB_NODE=$(for d in /sys/bus/usb/devices/*/; do
    [ -f "${d}idVendor" ] && [ "$(cat "${d}idVendor" 2>/dev/null)" = "35ca" ] || continue
    printf '/dev/bus/usb/%03d/%03d' \
      "$(cat "${d}busnum" 2>/dev/null)" "$(cat "${d}devnum" 2>/dev/null)"
    break
  done)
  if [ -n "$USB_NODE" ] && [ ! -w "$USB_NODE" ]; then
    warn "no write access to $USB_NODE -- the SDK will fail to init"
    warn "fix once (needs sudo): tools/install-udev-rule.sh"
  fi
else
  warn "no VITURE glasses on USB right now (fine for installing)"
fi

# Another XR driver holding the glasses is the commonest reason Refract comes
# up with no head tracking. Refract asks about this itself at every start-up
# (refract/core/conflicts.py); this is only the early warning, so it calls the
# same code rather than keeping a second copy of the detection that drifts.
# It exits non-zero only when something is holding the device NOW -- a
# dormant install is not worth a line in the installer's output.
CONFLICT_CHECK="python3 -m refract.core.conflicts"
if ! CONFLICTS="$(cd "$REPO" && $CONFLICT_CHECK 2>/dev/null)"; then
  echo
  warn "Something else on this machine is holding the glasses:"
  printf '%s\n' "$CONFLICTS"
  warn "USB access is exclusive. Refract offers to stop it -- or uninstall"
  warn "it -- every time it starts, so this can wait until then."
  echo
fi

# ------------------------------------------------------------------ venv
# The venv's python is a symlink to /usr/bin/python3, so a distro upgrade
# that bumps the system Python (3.12 -> 3.14 happened) silently strands every
# installed package under the old lib/pythonX.Y -- and Refract dies on
# "No module named moderngl". Rebuild instead of installing into a mismatch.
if [ -f "$VENV/pyvenv.cfg" ]; then
  VENVVER=$(sed -n 's/^version *= *\([0-9]*\.[0-9]*\).*/\1/p' "$VENV/pyvenv.cfg")
  if [ -n "$VENVVER" ] && [ "$VENVVER" != "$PYVER" ]; then
    warn "venv was built for python $VENVVER, system is now $PYVER -- rebuilding"
    rm -rf "$VENV"
  fi
fi
if [ ! -x "$PY" ]; then
  say "creating venv (with system site packages, for gi/Gst)"
  python3 -m venv --system-site-packages "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
say "installing python dependencies"
"$PY" -m pip install --quiet moderngl glcontext numpy pillow \
  || die "pip install failed"
ok "moderngl, glcontext, numpy, pillow"
if [ "$WITH_I3D" = "1" ]; then
  "$PY" -m pip install --quiet ai-edge-litert tqdm glfw || \
    warn "i3d extras failed (2D->3D research only; the shell is unaffected)"
  ok "i3d extras"
fi

"$PY" -c "import refract, sys; sys.path.insert(0,'$REPO')" 2>/dev/null || true
(cd "$REPO" && "$PY" -c "import refract; print('  ok   refract %s' % refract.__version__)") \
  || die "the refract package would not import"

# ------------------------------------------------------------- fast blit
# Optional C fast path for capture -> GL texture. Desk works without it,
# just slower, so a compiler failure is a warning and never fatal.
say "building the capture fast path"
if (cd "$REPO" && "$PY" -m refract.core.fastblit --build >/dev/null 2>&1); then
  ok "fast blit built (Desk uploads frames without copying them in Python)"
else
  warn "fast blit did not build -- Desk falls back to the slower Python"
  warn "path. For the faster one: sudo apt install build-essential libgl-dev"
fi

# ----------------------------------------------------------------- icons
say "rendering icons"
(cd "$REPO" && "$PY" tools/make-icon.py >/dev/null) || die "icon render failed"
for s in 16 24 32 48 64 128 256 512; do
  install -Dm644 "$REPO/assets/icons/refract-$s.png" \
    "$ICONS/${s}x${s}/apps/refract.png"
done
install -Dm644 "$REPO/assets/refract.svg" "$ICONS/scalable/apps/refract.svg"
ok "icons installed into the hicolor theme"

# -------------------------------------------------------------- launcher
mkdir -p "$BIN"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Refract launcher (generated by install.sh)
cd "$REPO" || exit 1
if ! "$PY" -c 'import moderngl, gi; gi.require_version("Gtk", "4.0")' 2>/dev/null; then
  msg="Refract's Python environment is broken (often a system Python upgrade). Re-run: $REPO/install.sh"
  echo "\$msg" >&2
  command -v notify-send >/dev/null && notify-send -i refract Refract "\$msg"
  exit 1
fi
exec "$PY" -m refract "\$@"
EOF
chmod +x "$LAUNCHER"
ok "launcher: $LAUNCHER"

# ---------------------------------------------------------- desktop entry
mkdir -p "$APPS"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Refract
GenericName=XR Shell
Comment=Virtual monitors and immersive video on VITURE Pro XR glasses
Exec=$LAUNCHER
TryExec=$LAUNCHER
Icon=refract
Terminal=false
StartupNotify=true
Categories=Utility;
StartupWMClass=refract
Keywords=XR;VR;AR;glasses;VITURE;monitor;desktop;
Actions=Desk;Handoff;

[Desktop Action Desk]
Name=Open Refract Desk
Exec=$LAUNCHER --scene desk

[Desktop Action Handoff]
Name=Display Handoff (park / resume)
Exec=$PY -m refract.ctl handoff
EOF
chmod +x "$DESKTOP"
ok "desktop entry: $DESKTOP"

command -v update-desktop-database >/dev/null && \
  update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

# ------------------------------------------------------------------ done
echo
ok "installed"
echo
say "Launch it from your app grid (search 'Refract'), or:"
say "    refract               the home screen"
say "    refract --scene desk  straight into the virtual monitors"
echo
say "Strongly recommended -- bind Display Handoff to a key, so you can hand"
say "the desktop back without hunting for a terminal:"
say "    Settings > Keyboard > Custom Shortcuts > +"
say "    Name:    Refract handoff"
say "    Command: $PY -m refract.ctl handoff"
echo
if ! echo ":$PATH:" | grep -q ":$BIN:"; then
  warn "$BIN is not on your PATH; use the app grid, or add it to ~/.profile"
fi
say "Uninstall any time with:  ./install.sh --uninstall"
echo
