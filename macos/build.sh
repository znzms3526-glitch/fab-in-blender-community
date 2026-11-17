#!/bin/bash
# Run from a checkout on Apple Silicon with Xcode Command Line Tools installed.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(uname -s)" = Darwin
test "$(uname -m)" = arm64
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
pkg="$stage/fab_shelf"
mkdir -p "$pkg/runtime" dist
cp LICENSE COPYING.md "$pkg/"
for source in fab_shelf/*.py; do
  [[ "$(basename "$source")" == native.py ]] || cp "$source" "$pkg/"
done
cp -R fab_shelf/runtime/gallery "$pkg/runtime/"
cp fab_shelf/runtime/{owned.js,app.png} "$pkg/runtime/"
xcrun swiftc -swift-version 5 -O -target arm64-apple-macosx14.0 macos/main.swift -o "$pkg/runtime/FabInBlenderHost"
codesign --force --sign - "$pkg/runtime/FabInBlenderHost"
codesign --verify "$pkg/runtime/FabInBlenderHost"
out="$PWD/dist/Fab-in-Blender-macOS-arm64.zip"
(cd "$stage" && /usr/bin/zip -qr "$stage/plugin.zip" fab_shelf)
mv "$stage/plugin.zip" "$out"
echo "$out"
