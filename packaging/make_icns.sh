#!/bin/bash
# Build a macOS .icns from a 1024x1024 PNG. Needs macOS (sips, iconutil).
# Usage: packaging/make_icns.sh packaging/icon.png build/icon.icns
set -euo pipefail
SRC="$1"; OUT="$2"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SET="$WORK/icon.iconset"
mkdir -p "$SET" "$(dirname "$OUT")"
for s in 16 32 128 256 512; do
  sips -z $s $s "$SRC" --out "$SET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$SRC" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$OUT"
echo "icon written: $OUT"
