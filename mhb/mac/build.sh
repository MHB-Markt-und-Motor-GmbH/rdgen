#!/bin/bash
# Universal-Build für macOS: ./build.sh pas|carspider [--version 1.5.1]
# Arbeitsordner (Quellcode, vcpkg, Zwischenstände, Ergebnis): $RDBUILD_WORK,
# Standard /Volumes/SSD/rdbuild. Werkzeuge: setup.sh.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="${RDBUILD_WORK:-/Volumes/SSD/rdbuild}"
[ -d "$work" ] || { echo "Arbeitsordner $work fehlt (externe SSD angeschlossen?)" >&2; exit 1; }
if [ ! -d "$work/vcpkg/.git" ]; then
  git clone -q "$HOME/rdbuild/tools/vcpkg" "$work/vcpkg"
  git -C "$work/vcpkg" checkout -q "$(git -C "$HOME/rdbuild/tools/vcpkg" rev-parse HEAD)"
  cp "$HOME/rdbuild/tools/vcpkg/vcpkg" "$work/vcpkg/vcpkg"
fi
chmod +x "$here"/shims/*
exec caffeinate -i /usr/bin/python3 "$here/build.py" "$@"
