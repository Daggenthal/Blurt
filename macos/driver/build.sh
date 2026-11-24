#!/usr/bin/env bash
# Builds the Blurt virtual audio driver:  bash macos/driver/build.sh [out_dir]
# Produces <out_dir>/Blurt.driver (universal arm64 + x86_64, ad-hoc signed).
# TEST=1 also builds and runs test_driver against it (no installation needed).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/../build}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
BUNDLE="$OUT/Blurt.driver"
CFLAGS=(-O2 -Wall -Wextra -Werror -std=c11 -mmacosx-version-min=12.0)

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS"
cp "$HERE/Info.plist" "$BUNDLE/Contents/Info.plist"
clang "${CFLAGS[@]}" -arch arm64 -arch x86_64 -bundle -fvisibility=hidden \
    -framework CoreAudio -framework CoreFoundation \
    -o "$BUNDLE/Contents/MacOS/Blurt" "$HERE/BlurtDriver.c"
codesign --force --sign - --timestamp=none "$BUNDLE"
echo "Built $BUNDLE"

if [ "${TEST:-0}" = 1 ]; then
    clang "${CFLAGS[@]}" -framework CoreAudio -framework CoreFoundation \
        -o "$OUT/test_driver" "$HERE/test_driver.c"
    "$OUT/test_driver" "$BUNDLE/Contents/MacOS/Blurt"
fi
