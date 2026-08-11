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
else
  warn "no VITURE glasses on USB right now (fine for installing)"
fi

if pgrep -x xrDriver >/dev/null 2>&1; then
  warn "xrDriver is RUNNING and holds the glasses exclusively."
  warn "Refract cannot talk to them until you stop it:"
  warn "    systemctl --user stop xr-driver"
fi

# ------------------------------------------------------------------ venv
if [ ! -x "$PY" ]; then
  say "creating venv (with system site packages, for gi/Gst)"
  python3 -m venv --system-site-packages "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
say "installing python dependencies"
"$PY" -m pip install --quiet moderngl glfw glcontext numpy pillow \
  || die "pip install failed"
ok "moderngl, glfw, glcontext, numpy, pillow"
if [ "$WITH_I3D" = "1" ]; then
  "$PY" -m pip install --quiet ai-edge-litert tqdm || \
    warn "i3d extras failed (2D->3D research only; the shell is unaffected)"
  ok "i3d extras"
fi

"$PY" -c "import refract, sys; sys.path.insert(0,'$REPO')" 2>/dev/null || true
(cd "$REPO" && "$PY" -c "import refract; print('  ok   refract %s' % refract.__version__)") \
  || die "the refract package would not import"

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
