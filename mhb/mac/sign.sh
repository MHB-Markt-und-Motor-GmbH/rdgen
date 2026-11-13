#!/bin/bash
# Signieren mit Developer ID (Hardened Runtime), Notarisieren, Heften, DMG.
#   ./sign.sh pas|carspider
# Voraussetzungen auf dem Mac (siehe docs im privaten Projekt):
#   - Zertifikat „Developer ID Application: MHB Markt und Motor GmbH (26V8EE3C6P)“ im Anmelde-Schlüsselbund
#   - Notarisierungsprofil: xcrun notarytool store-credentials MHB-Notary ...
#   - per SSH: Schlüsselbund vorher entsperren (security unlock-keychain)
# Ergebnis: $RDBUILD_WORK/out/<marke>/<Dateiname>.dmg
set -euo pipefail

brand="${1:?Marke fehlt (pas oder carspider)}"
here="$(cd "$(dirname "$0")" && pwd)"
rdgen="$(cd "$here/../.." && pwd)"
work="${RDBUILD_WORK:-/Volumes/SSD/rdbuild}"
profile="${NOTARY_PROFILE:-MHB-Notary}"
team="26V8EE3C6P"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cfg="$rdgen/mhb/$brand/config.json"
val() { /usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$cfg" "$1"; }
appname="$(val appname)"
filename="$(val filename)"
out="$work/out/$brand"
app="$out/$appname.app"
ent="$out/Release.entitlements"
[ -d "$app" ] || { echo "App fehlt: $app (zuerst build.sh)" >&2; exit 1; }

# Per SSH ist der Anmelde-Schlüsselbund gesperrt (eigene Sitzung). Dann das Mac-Passwort über stdin:
#   ssh mac 'cd …/mhb/mac && ./sign.sh pas' < passwortdatei
kc="$HOME/Library/Keychains/login.keychain-db"
if ! security show-keychain-info "$kc" >/dev/null 2>&1; then
  [ -t 0 ] && { echo "Schlüsselbund gesperrt: am Mac ausführen oder Passwort über stdin" >&2; exit 1; }
  IFS= read -r kcpw || true
  security unlock-keychain -p "$kcpw" "$kc"
  unset kcpw
fi

id="$(security find-identity -v -p codesigning | awk -v t="($team)" '/Developer ID Application: MHB Markt und Motor GmbH/ && index($0, t) {print $2; exit}')"
[ -n "$id" ] || { echo "Developer ID der MHB nicht im Schlüsselbund (oder gesperrt)" >&2; exit 1; }
step() { printf '\n=== %s (%s)\n' "$1" "$(date '+%H:%M:%S')"; }
sign() { codesign --force --timestamp --options runtime --sign "$id" "$@"; }

if codesign --verify --deep --strict "$app" 2>/dev/null && xcrun stapler validate -q "$app" 2>/dev/null \
   && codesign -dvv "$app" 2>&1 | grep -q "Authority=Developer ID Application: MHB Markt und Motor GmbH"; then
  step "App ist schon signiert und notarisiert, weiter mit der DMG"
else
step "Signieren $appname"
xattr -cr "$app"
# von innen nach außen: Bibliotheken, Frameworks, Hilfsprogramm, App
find "$app/Contents/Frameworks" -name "*.dylib" -type f -print0 | while IFS= read -r -d '' f; do sign "$f"; done
find "$app/Contents/Frameworks" -name "*.framework" -maxdepth 1 -type d -print0 | while IFS= read -r -d '' f; do sign "$f"; done
sign "$app/Contents/MacOS/service"
sign --entitlements "$ent" "$app"
codesign --verify --deep --strict --verbose=2 "$app"

step "Notarisieren der App"
zip="$out/$appname.zip"
rm -f "$zip"
ditto -c -k --keepParent "$app" "$zip"
xcrun notarytool submit "$zip" --keychain-profile "$profile" --wait
xcrun stapler staple "$app"
rm -f "$zip"
fi

step "DMG"
dmg="$out/$filename.dmg"
rm -f "$dmg" "$out"/rw.*.dmg
# --skip-jenkins: ohne Finder-Fensterlayout (AppleScript an den Finder geht über SSH nicht)
create-dmg --volname "$appname" --skip-jenkins --app-drop-link 600 185 "$dmg" "$app"
codesign --force --timestamp --sign "$id" "$dmg"

step "Notarisieren der DMG"
xcrun notarytool submit "$dmg" --keychain-profile "$profile" --wait
xcrun stapler staple "$dmg"

step "Prüfen"
spctl -a -vvv -t exec "$app"
spctl -a -vvv -t open --context context:primary-signature "$dmg"
shasum -a 256 "$dmg"
ls -lh "$dmg"
