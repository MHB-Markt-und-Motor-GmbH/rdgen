"""MHB-Anpassungen am RustDesk-Quellcode, für alle Plattformen.

Läuft nach den rdgen-Schritten und vor dem Bauen:
    python mhb/patch_source.py <rustdesk-quellordner> <marke>

1. Hinweis auf RustDesk im Fenster (AGPL, sichtbare Rechtshinweise): rdgen ersetzt
   „Powered by RustDesk“ durch den App- oder Firmennamen. Die Zeile oben im Fenster lautet
   danach „<Firma> · basiert auf RustDesk“ (englisch „based on RustDesk“).
2. macOS: eigene Bundle-Kennung statt com.carriez.rustdesk, damit Freigaben (Bildschirmaufnahme,
   Bedienungshilfen) nicht mit einem installierten RustDesk oder der anderen Marke kollidieren.
   RustDesk liest die Kennung zur Laufzeit (src/platform/macos.rs).
"""
import json
import re
import sys
from pathlib import Path


def main():
    src, brand = Path(sys.argv[1]), sys.argv[2]
    if not re.fullmatch(r"[a-z]+", brand):
        raise SystemExit(f"invalid brand: {brand!r}")
    cfg = json.loads((Path(__file__).resolve().parent / brand / "config.json").read_text(encoding="utf-8"))
    company = cfg["compname"].replace("\\&", "&")
    appname = cfg["appname"]

    # 1. Hinweis in allen Sprachdateien
    n = 0
    for f in sorted((src / "src" / "lang").glob("*.rs")):
        t = f.read_text(encoding="utf-8")
        text = f"{company} · basiert auf RustDesk" if f.stem == "de" else f"{company} · based on RustDesk"
        t2, k = re.subn(r'\("powered_by_me", "[^"]*"\)', lambda m: f'("powered_by_me", "{text}")', t)
        if k:
            f.write_text(t2, encoding="utf-8")
            n += 1
    if n < 2:
        raise SystemExit(f"powered_by_me nur in {n} Sprachdateien gefunden")
    print(f"Hinweis auf RustDesk in {n} Sprachdateien")

    # 2. Bundle-Kennung (nur macOS-Projekt)
    pbx = src / "flutter" / "macos" / "Runner.xcodeproj" / "project.pbxproj"
    if pbx.exists():
        bundle_id = f"com.mhbmarktundmotor.{appname.lower()}"
        t = pbx.read_text(encoding="utf-8")
        t2, k = re.subn(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]+;", f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id};", t)
        if k == 0:
            raise SystemExit("PRODUCT_BUNDLE_IDENTIFIER nicht gefunden")
        pbx.write_text(t2, encoding="utf-8")
        xc = src / "flutter" / "macos" / "Runner" / "Configs" / "AppInfo.xcconfig"
        if xc.exists():
            xc.write_text(re.sub(r"PRODUCT_BUNDLE_IDENTIFIER = .*", f"PRODUCT_BUNDLE_IDENTIFIER = {bundle_id}",
                                 xc.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"Bundle-Kennung {bundle_id} ({k} Stellen)")


if __name__ == "__main__":
    main()
