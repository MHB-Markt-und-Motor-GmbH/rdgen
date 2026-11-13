#!/bin/bash
# Einmalige Einrichtung der Bauumgebung auf einem Mac mit Apple Silicon.
# Versionen wie in .github/workflows/generator-macos.yml. Nichts am Shell-Profil,
# alles unter $RDBUILD (Standard ~/rdbuild), Homebrew nativ unter /opt/homebrew
# (eine vorhandene Intel-Installation in /usr/local bleibt unberührt).
set -euo pipefail

ROOT="${RDBUILD:-$HOME/rdbuild}"
FLUTTER_VERSION=3.24.5
RUST_VERSION=1.81
VCPKG_COMMIT=9e593bb18ea69cc5095e012465dcd675a822ed0d
NASM_VERSION=2.16.03

[ "$(uname -m)" = arm64 ] || { echo "Nur für Apple Silicon." >&2; exit 1; }
xcodebuild -version >/dev/null || { echo "Xcode fehlt." >&2; exit 1; }
mkdir -p "$ROOT/tools"
step() { printf '\n=== %s (%s)\n' "$1" "$(date '+%H:%M:%S')"; }

step "Homebrew (arm64)"
if [ ! -x /opt/homebrew/bin/brew ]; then
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
export HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_ENV_HINTS=1
brew install llvm create-dmg pkg-config cmake ninja cocoapods imagemagick potrace wget python@3.12
/usr/bin/python3 -m pip install --user --quiet --disable-pip-version-check pyyaml

step "NASM $NASM_VERSION (2.x, nicht 3.x aus Homebrew)"
if [ ! -x "$ROOT/tools/nasm/nasm" ]; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/nasm.zip" "https://www.nasm.us/pub/nasm/releasebuilds/$NASM_VERSION/macosx/nasm-$NASM_VERSION-macosx.zip"
  unzip -q "$tmp/nasm.zip" -d "$tmp"
  mkdir -p "$ROOT/tools/nasm" && cp "$tmp/nasm-$NASM_VERSION/nasm" "$ROOT/tools/nasm/"
  rm -rf "$tmp"
fi
"$ROOT/tools/nasm/nasm" -v

step "Rust $RUST_VERSION mit beiden Zielen"
if [ ! -x "$HOME/.cargo/bin/rustup" ]; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --default-toolchain none
fi
"$HOME/.cargo/bin/rustup" toolchain install "$RUST_VERSION" --profile minimal -c rustfmt \
  -t aarch64-apple-darwin -t x86_64-apple-darwin

step "Flutter $FLUTTER_VERSION"
if [ ! -d "$ROOT/tools/flutter" ]; then
  git clone --depth 1 -b "$FLUTTER_VERSION" https://github.com/flutter/flutter.git "$ROOT/tools/flutter"
fi
export PATH="$ROOT/tools/flutter/bin:$PATH"
flutter --disable-analytics >/dev/null 2>&1 || true
flutter config --no-analytics --no-cli-animations >/dev/null
flutter precache --macos

step "vcpkg $VCPKG_COMMIT"
if [ ! -d "$ROOT/tools/vcpkg/.git" ]; then
  git clone https://github.com/microsoft/vcpkg.git "$ROOT/tools/vcpkg"
fi
git -C "$ROOT/tools/vcpkg" fetch -q origin
git -C "$ROOT/tools/vcpkg" checkout -q "$VCPKG_COMMIT"
"$ROOT/tools/vcpkg/bootstrap-vcpkg.sh" -disableMetrics

step "Fertig"
df -h "$ROOT" | tail -1
