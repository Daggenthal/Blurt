#!/usr/bin/env bash
# Builds Blurt.app, a standalone macOS app (its own Python, Qt and audio driver inside),
# and a .dmg to share it.
#
#   bash macos/build_app.sh                 dist/Blurt.app and dist/Blurt-<version>-<arch>.dmg
#   bash macos/build_app.sh --native-only   only the audio driver and library (into
#                                           macos/build), for running Blurt from source:
#                                           python3 blurt.py
#
# Needs the Xcode command line tools (xcode-select --install). Downloads uv (if missing),
# a Python build and the Python packages, about 250 MB the first time.
#
# Signing: ad-hoc by default, which is fine on the Mac that built it. To share the app
# without Gatekeeper warnings, sign with a Developer ID and notarize:
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=<notarytool keychain profile> bash macos/build_app.sh
# The GitHub build (.github/workflows/mac-app.yml) signs with a self-signed certificate
# instead (SIGN_IDENTITY = its SHA-1, SIGN_KEYCHAIN = the keychain holding it): macOS then
# keeps Blurt's permissions across updates, because every build has the same signer.
#
# BLURT_COMMIT (default: the git commit) is stored in the app, so it knows its version
# when it checks for updates.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD="$HERE/build"
DIST="${DIST:-$ROOT/dist}"
VERSION="${VERSION:-1.0.0}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PYSIDE_VERSION="${PYSIDE_VERSION:-6.12.0}"
NUMPY_VERSION="${NUMPY_VERSION:-2.5.5}"
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
SIGN_KEYCHAIN="${SIGN_KEYCHAIN:-}"
BLURT_COMMIT="${BLURT_COMMIT:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}"
ARCH="$(uname -m)"

step() { echo; echo "== $* =="; }
NATIVE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --native-only) NATIVE_ONLY=1 ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
command -v clang >/dev/null || { echo "Install the Xcode command line tools: xcode-select --install"; exit 1; }
mkdir -p "$BUILD"

# ---- native parts ---------------------------------------------------------------------
step "Building the Blurt-Mic audio driver"
bash "$HERE/driver/build.sh" "$BUILD"

step "Building the audio library"
clang -dynamiclib -fobjc-arc -O2 -Wall -Wextra -Wno-unused-parameter \
    -mmacosx-version-min=12.0 -arch arm64 -arch x86_64 \
    -framework Foundation -framework AudioToolbox -framework CoreAudio \
    -framework ApplicationServices -install_name @rpath/libblurtaudio.dylib \
    -o "$BUILD/libblurtaudio.dylib" "$HERE/src/blurtaudio.m"
codesign --force --sign - "$BUILD/libblurtaudio.dylib"

if [ "$NATIVE_ONLY" = 1 ]; then
    echo
    echo "Done. Run Blurt from source with a Python that has PySide6 and numpy:"
    echo "    python3 blurt.py"
    exit 0
fi

# ---- Python ---------------------------------------------------------------------------
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    UV="$BUILD/uv/uv"
    if [ ! -x "$UV" ]; then
        step "Downloading uv"
        case "$ARCH" in arm64) T=aarch64-apple-darwin ;; *) T=x86_64-apple-darwin ;; esac
        mkdir -p "$BUILD/uv"
        curl -fL --progress-bar "https://github.com/astral-sh/uv/releases/latest/download/uv-$T.tar.gz" \
            | tar -xz -C "$BUILD/uv" --strip-components 1
    fi
fi
export UV_PYTHON_INSTALL_DIR="$BUILD/python-dist"
export UV_PYTHON_PREFERENCE=only-managed
step "Getting Python $PYTHON_VERSION"
"$UV" python install --no-bin "$PYTHON_VERSION"
PYSRC="$(find "$UV_PYTHON_INSTALL_DIR" -maxdepth 1 -type d -name "cpython-$PYTHON_VERSION.*-macos-*" | sort -V | tail -1)"
[ -n "$PYSRC" ] || { echo "Python $PYTHON_VERSION was not found in $UV_PYTHON_INSTALL_DIR"; exit 1; }
PYV="python$PYTHON_VERSION"

APP="$DIST/Blurt.app"
C="$APP/Contents"
R="$C/Resources"
step "Assembling $APP"
rm -rf "$APP"
mkdir -p "$C/MacOS" "$R"
ditto "$PYSRC" "$R/python"
PY="$R/python/bin/$PYV"
SITE="$R/python/lib/$PYV/site-packages"
rm -f "$R/python/lib/$PYV/EXTERNALLY-MANAGED"   # this Python belongs to Blurt alone
# Tk and the test suite are not used
rm -rf "$R/python/lib/"{tcl,tk,itcl,thread}* "$R/python/lib/"libtcl* "$R/python/lib/"libtk* \
       "$R/python/lib/$PYV/"{test,idlelib,tkinter,turtledemo} \
       "$R/python/lib/$PYV/lib-dynload/"_tkinter* "$R/python/share"
install_name_tool -id "@rpath/libpython$PYTHON_VERSION.dylib" "$R/python/lib/libpython$PYTHON_VERSION.dylib"
codesign --force --sign - "$R/python/lib/libpython$PYTHON_VERSION.dylib"

step "Installing Qt (PySide6) and numpy"
"$UV" pip install --python "$PY" \
    "PySide6-Essentials==$PYSIDE_VERSION" "PySide6-Addons==$PYSIDE_VERSION" "numpy==$NUMPY_VERSION"
"$PY" "$HERE/prune_qt.py" "$SITE"
# the oldest macOS the app runs on is the one Qt was built for
MIN_MACOS="$(otool -l "$SITE/PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore" |
             awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
MIN_MACOS="${MIN_MACOS:-14.0}"
echo "Runs on macOS $MIN_MACOS and later"

step "Adding Blurt"
mkdir -p "$R/app"
cp "$ROOT/blurt.py" "$ROOT/blurt_update.py" "$ROOT/LICENSE" "$R/app/"
rsync -a --exclude __pycache__ --exclude '*.pyc' "$ROOT/blurt_voice" "$ROOT/blurt_mac" "$R/app/"
cp "$BUILD/libblurtaudio.dylib" "$R/app/blurt_mac/"
ditto "$BUILD/Blurt.driver" "$R/Blurt.driver"
"$PY" "$HERE/make_icon.py" "$R/Blurt.icns"

step "Building the launcher"
clang -O2 -Wall -Wextra -mmacosx-version-min=$MIN_MACOS -arch "$ARCH" \
    -I "$R/python/include/$PYV" -L "$R/python/lib" -l"$PYV" \
    -Wl,-rpath,@executable_path/../Resources/python/lib \
    -o "$C/MacOS/Blurt" "$HERE/src/launcher.c"
rm -rf "$R/python/include" "$R/python/lib/pkgconfig"

cat > "$C/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Blurt</string>
    <key>CFBundleDisplayName</key><string>Blurt</string>
    <key>CFBundleIdentifier</key><string>io.github.daggenthal.blurt</string>
    <key>CFBundleExecutable</key><string>Blurt</string>
    <key>CFBundleIconFile</key><string>Blurt</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>BlurtCommit</key><string>$BLURT_COMMIT</string>
    <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Blurt mixes your microphone with the sounds you play, so others hear both, and the voice changer and noise gate work on it.</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>Blurt keeps the last few seconds of what your Mac plays, so you can clip it, and can pass an app such as Discord on to Blurt-Friends.</string>
</dict>
</plist>
EOF

step "Precompiling Python files"
# The signed app must never change, so Python gets everything precompiled (and the
# launcher tells it not to write any .pyc files).
"$PY" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$R/python/lib/$PYV" "$R/app" >/dev/null || true

# ---- signing --------------------------------------------------------------------------
step "Signing (identity: $SIGN_IDENTITY)"
SIGN=(codesign --force --sign "$SIGN_IDENTITY" --timestamp=none)
[ -n "$SIGN_KEYCHAIN" ] && SIGN+=(--keychain "$SIGN_KEYCHAIN")
if [ -n "${NOTARY_PROFILE:-}" ]; then  # notarizing needs the hardened runtime and a timestamp
    SIGN=(codesign --force --sign "$SIGN_IDENTITY" --timestamp --options runtime)
fi
# every library and program inside, innermost first
find "$R" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 |
    while IFS= read -r -d '' f; do
        if file -b "$f" | grep -q "Mach-O"; then "${SIGN[@]}" "$f" >/dev/null 2>&1 || "${SIGN[@]}" "$f"; fi
    done
find "$R/python/lib/$PYV/site-packages/PySide6/Qt/lib" -maxdepth 1 -name '*.framework' -print0 |
    while IFS= read -r -d '' f; do "${SIGN[@]}" "$f" >/dev/null; done
# The driver is loaded by Core Audio, not by Blurt: keep it ad-hoc unless it is a
# Developer ID build, so macOS never has to judge a self-signed certificate there.
if [ -n "${NOTARY_PROFILE:-}" ]; then "${SIGN[@]}" "$R/Blurt.driver"; else
    codesign --force --sign - --timestamp=none "$R/Blurt.driver"; fi
"${SIGN[@]}" --entitlements "$HERE/entitlements.plist" "$APP"
codesign --verify --deep --strict "$APP"
echo "Signature OK"

# ---- disk image -----------------------------------------------------------------------
DMG="$DIST/Blurt-$VERSION-$ARCH.dmg"
step "Creating $DMG"
STAGE="$BUILD/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Blurt.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname Blurt -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
if [ -n "${NOTARY_PROFILE:-}" ]; then
    codesign --force --sign "$SIGN_IDENTITY" --timestamp "$DMG"
    step "Notarizing (this can take a few minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
fi

echo
echo "Done:"
echo "  $APP  ($(du -sh "$APP" | cut -f1))"
echo "  $DMG  ($(du -sh "$DMG" | cut -f1))"
