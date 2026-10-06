#!/usr/bin/env bash
# Build the refract .deb from the COMMITTED tree (so a release is
# reproducible from a commit, not from whatever is in the working folder).
#
#   packaging/build-deb.sh            -> dist/refract_<version>_amd64.deb
#   FORCE=1 packaging/build-deb.sh    build even with uncommitted changes
#
# Needs: git, gcc + libgl-dev (the C capture fast path is compiled here),
# python3, curl or wget, dpkg-deb, fakeroot.
#
# Layout it installs (REPO in the code == /usr/lib/refract, so the package
# finds its SDK and fast-path library exactly where a checkout would):
#   /usr/bin/refract, refract-ctl
#   /usr/lib/refract/refract/...                 the Python package
#   /usr/lib/refract/refract/core/librefract_blit.so
#   /usr/lib/refract/sdk/libs/libviture_one_sdk.so   VITURE's runtime library
#   /usr/lib/udev/rules.d/99-refract-xr.rules    USB access for the glasses
#   /usr/share/{applications,icons,doc}/...
#
# Deliberately NOT included: libglasses.so, libcarina_vio.so and OpenCV --
# nothing in the shell loads them (only tools/viture-hw.py), and their
# licence is unresolved (docs/THIRD-PARTY.md).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
. packaging/sdk-pin.sh

die() { printf 'build-deb: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' refract/__init__.py)
[ -n "$VERSION" ] || die "cannot read the version from refract/__init__.py"
ARCH=amd64          # the VITURE SDK binary is x86_64
PKG="refract_${VERSION}_${ARCH}"
DIST="$REPO/dist"
STAGE="$DIST/stage/$PKG"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/refract-build"

for t in git gcc python3 dpkg-deb fakeroot sha256sum tar; do
  command -v "$t" >/dev/null || die "need $t"
done

if [ -n "$(git status --porcelain --untracked-files=no)" ] && [ "${FORCE:-0}" != 1 ]; then
  git status --short --untracked-files=no >&2
  die "uncommitted changes: commit them, or FORCE=1 to build from HEAD anyway"
fi
COMMIT=$(git rev-parse --short HEAD)
say "refract $VERSION  (commit $COMMIT)"

rm -rf "$STAGE"
mkdir -p "$STAGE/usr/lib/refract" "$STAGE/DEBIAN"

# ---- the code, from the commit ------------------------------------------
git archive HEAD refract assets udev | tar -x -C "$STAGE/usr/lib/refract"
# only the package belongs under /usr/lib/refract; move the rest to /usr/share
mkdir -p "$STAGE/usr/lib/udev/rules.d" "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/refract" "$STAGE/usr/bin"
mv "$STAGE/usr/lib/refract/udev/99-refract-xr.rules" "$STAGE/usr/lib/udev/rules.d/"
for s in 16 24 32 48 64 128 256 512; do
  install -Dm644 "$STAGE/usr/lib/refract/assets/icons/refract-$s.png" \
    "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/refract.png"
done
install -m644 "$STAGE/usr/lib/refract/assets/refract.svg" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps/refract.svg"
rm -rf "$STAGE/usr/lib/refract/assets" "$STAGE/usr/lib/refract/udev"

# ---- VITURE's runtime library, fetched and verified ----------------------
mkdir -p "$CACHE"
TARBALL="$CACHE/viture_linux_sdk_v${SDK_VERSION}.tar.xz"
if ! { [ -s "$TARBALL" ] && echo "$SDK_SHA256  $TARBALL" | sha256sum --quiet -c - 2>/dev/null; }; then
  say "downloading the VITURE SDK $SDK_VERSION"
  if command -v curl >/dev/null; then curl -fsSL --retry 2 -o "$TARBALL" "$SDK_URL"
  elif command -v wget >/dev/null; then wget -q -O "$TARBALL" "$SDK_URL"
  else die "need curl or wget"; fi
  echo "$SDK_SHA256  $TARBALL" | sha256sum --quiet -c - \
    || { rm -f "$TARBALL"; die "SDK download does not match the pinned hash"; }
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
tar -xJf "$TARBALL" -C "$tmp"
install -Dm755 "$tmp/viture_one_linux_sdk_${SDK_VERSION}/libs/libviture_one_sdk.so" \
  "$STAGE/usr/lib/refract/sdk/libs/libviture_one_sdk.so"

# ---- the C capture fast path ---------------------------------------------
say "compiling the capture fast path"
git show HEAD:csrc/refract_blit.c > "$tmp/refract_blit.c"
cc -O2 -fPIC -shared -Wall -o "$STAGE/usr/lib/refract/refract/core/librefract_blit.so" \
   "$tmp/refract_blit.c" -lGL
chmod 755 "$STAGE/usr/lib/refract/refract/core/librefract_blit.so"

# ---- bytecode (any other Python just compiles its own) --------------------
python3 -m compileall -q -d /usr/lib/refract/refract \
  --invalidation-mode unchecked-hash "$STAGE/usr/lib/refract/refract"

# ---- launchers, desktop entry, docs --------------------------------------
install -m755 packaging/deb/refract packaging/deb/refract-ctl "$STAGE/usr/bin/"
install -m644 packaging/deb/refract.desktop "$STAGE/usr/share/applications/"
install -m644 packaging/deb/copyright "$STAGE/usr/share/doc/refract/copyright"
install -m644 README.md LICENSE docs/THIRD-PARTY.md "$STAGE/usr/share/doc/refract/"
gzip -9n -c packaging/deb/changelog > "$STAGE/usr/share/doc/refract/changelog.gz"
chmod 644 "$STAGE/usr/share/doc/refract/changelog.gz"
install -m755 packaging/deb/postinst packaging/deb/postrm "$STAGE/DEBIAN/"

# ---- control --------------------------------------------------------------
SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: refract
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE
Maintainer: Parhelia Technology <60525114+parheliatech@users.noreply.github.com>
Homepage: https://github.com/parheliatech/refract
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-gst-plugins-base-1.0, gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-pipewire, python3-moderngl, python3-numpy, python3-pil, libgl1, libegl1, zlib1g, libc6 (>= 2.34)
Recommends: gnome-shell, pipewire, libnotify-bin, zenity
Description: XR shell for VITURE Pro XR glasses on Linux
 Refract puts a home screen on VITURE Pro XR glasses and hosts experiences
 inside it. Refract Desk gives three virtual monitors (the middle one
 mirrors the laptop screen) with a pointer that crosses all of them;
 Display Handoff hands the desktop back to the laptop and takes it again.
 Head tracking, three-nod and temple-tap gestures, and a HUD menu included.
 .
 Needs GNOME on Wayland (it drives Mutter's virtual monitors) and the
 glasses in DisplayPort mode. Bundles VITURE's closed-source runtime
 library, under the VITURE SDK License Agreement (see the copyright file).
EOF
(cd "$STAGE" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)

# ---- permissions and build ------------------------------------------------
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr/lib/refract" -type f ! -name '*.so' -exec chmod 644 {} +
chmod 644 "$STAGE/usr/lib/udev/rules.d/99-refract-xr.rules"
mkdir -p "$DIST"
fakeroot dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DIST/$PKG.deb" >/dev/null
say "built $DIST/$PKG.deb"
say "$(du -h "$DIST/$PKG.deb" | cut -f1), sha256 $(sha256sum "$DIST/$PKG.deb" | cut -c1-16)..."
