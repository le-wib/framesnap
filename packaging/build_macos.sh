#!/bin/bash
# Build FrameSnap.app and a .dmg on macOS (same script locally and in CI).
#
#   pip install -r requirements-dev.txt
#   bash packaging/build_macos.sh [label]       # label defaults to the CPU arch
#
# ffmpeg comes from the imageio-ffmpeg wheel, which ships a static build with
# x264 for the CPU of the machine running the build. The app is therefore
# built once per architecture (arm64 and x86_64).
set -euo pipefail
cd "$(dirname "$0")/.."

LABEL="${1:-$(uname -m)}"
VERSION="$(python -c 'import framesnap; print(framesnap.__version__)')"
APP="dist/FrameSnap.app"
LOG="$PWD/build/selftest.log"

rm -rf build dist
mkdir -p build/bin
FFMPEG_SRC="$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
cp "$FFMPEG_SRC" build/bin/ffmpeg
chmod +x build/bin/ffmpeg
codesign --force -s - build/bin/ffmpeg
build/bin/ffmpeg -hide_banner -version | head -1
file build/bin/ffmpeg
bash packaging/make_icns.sh packaging/icon.png build/icon.icns

# rife-ncnn-vulkan (MIT): universal binary (Apple Silicon and Intel), runs on the GPU through Metal
python packaging/fetch_rife.py macos --dest build/rife
chmod +x build/rife/rife-ncnn-vulkan
codesign --force -s - build/rife/rife-ncnn-vulkan

build_app() {  # $1 = binary | data : how ffmpeg is added to the bundle
  rm -rf dist build/FrameSnap
  local flag="--add-binary"
  [ "$1" = "data" ] && flag="--add-data"
  pyinstaller --noconfirm --clean --windowed --name FrameSnap \
    --icon build/icon.icns --osx-bundle-identifier com.framesnap.app \
    --add-data "framesnap/ui:framesnap/ui" \
    --add-data "build/rife:rife" \
    "$flag" "build/bin/ffmpeg:bin" \
    --collect-submodules webview --collect-all cv2 --exclude-module imageio_ffmpeg \
    run_app.py
  local plist="$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleShortVersionString" "$plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$plist"
  /usr/libexec/PlistBuddy -c "Delete :CFBundleVersion" "$plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$plist"
  codesign --force --deep --sign - "$APP"
  codesign --verify --deep --strict "$APP"
}

ok=0
for mode in binary data; do
  echo "=== building with ffmpeg added as: $mode"
  build_app "$mode"
  find "$APP" -name ffmpeg -type f -print
  rm -f "$LOG"
  if FRAMESNAP_SELFTEST_LOG="$LOG" "$APP/Contents/MacOS/FrameSnap" --selftest; then
    cat "$LOG"; ok=1; break
  fi
  echo "selftest failed in mode $mode"; cat "$LOG" 2>/dev/null || true
done
[ "$ok" = 1 ] || { echo "ERROR: packaged app failed its selftest"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="dist/FrameSnap-${VERSION}-${LABEL}.dmg"
hdiutil create -volname "FrameSnap" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "built: $DMG"
