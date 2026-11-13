#!/usr/bin/env python3
"""Lokaler Universal-Build für macOS (Apple Silicon und Intel in einer App).

Arbeitet die `run:`-Schritte des Jobs `build-for-macos` aus
.github/workflows/generator-macos.yml der Reihe nach ab, mit den Werten aus
mhb/<marke>/config.json, so wie GitHub es für den Apple-Silicon-Runner täte.
Die `uses:`-Schritte (Checkout, Caches, Werkzeuge, Artefakte) ersetzt dieses
Skript durch lokale Entsprechungen. Ein neuer, unbekannter `uses:`-Schritt
nach einem Abgleich mit upstream bricht den Build ab, damit er auffällt.

Universal: Der Rust-Teil wird zusätzlich für x86_64 gebaut, mit `lipo`
zusammengeführt, und Xcode baut die App für arm64 und x86_64.

Aufruf (über build.sh): build.py pas|carspider [--version 1.5.1]
Ergebnis: $RDBUILD_WORK/out/<marke>/<App>.app (ad hoc signiert). Signieren,
Notarisieren und DMG macht sign.sh.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RDGEN = HERE.parent.parent
WF = RDGEN / ".github" / "workflows" / "generator-macos.yml"
JOB = "build-for-macos"
ARM_TARGET = "aarch64-apple-darwin"
X86_TARGET = "x86_64-apple-darwin"

BRIDGE_FILES = [
    "src/bridge_generated.rs",
    "src/bridge_generated.io.rs",
    "flutter/lib/generated_bridge.dart",
    "flutter/lib/generated_bridge.freezed.dart",
    "flutter/macos/Runner/bridge_generated.h",
    "flutter/ios/Runner/bridge_generated.h",
]

# Schritte, die lokal anders oder gar nicht laufen (Name aus dem Workflow)
SKIP = {
    "Checkout Repository",                       # wir arbeiten im lokalen Fork
    "Load brand config",                         # schon geladen
    "Export GitHub Actions cache environment variables",
    "Install imagemagick and potrace and nasm and and",  # setup.sh
    "Install build runtime",                     # setup.sh
    "Install NASM",                              # setup.sh (2.16 ohne sudo)
    "Install flutter",                           # setup.sh
    "Install Rust toolchain",                    # setup.sh
    "Restore vcpkg installed cache",
    "Setup vcpkg",                               # VCPKG_ROOT
    "Save vcpkg installed cache",
    "Install rcodesign tool",                    # signiert wird mit codesign auf dem Mac
    "Sign macOS app bundle",
    "Create DMG",                                # sign.sh
    "Rename rustdesk",
    "Upload artifact",
    "send file to rdgen server",
    "send file to api server",
}
SKIP_USES = {"Swatinem/rust-cache"}


def log(msg):
    print(f"\n=== {msg} ({time.strftime('%H:%M:%S')})", flush=True)


def run(cmd, cwd, env, check=True):
    print(f"$ {cmd}", flush=True)
    r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", cmd], cwd=cwd, env=env)
    if check and r.returncode:
        raise SystemExit(f"Fehlgeschlagen ({r.returncode}): {cmd[:200]}")
    return r.returncode


class Ctx:
    def __init__(self, env, inputs, matrix, workspace):
        self.env, self.inputs, self.matrix, self.workspace = env, inputs, matrix, workspace

    def ref(self, name):
        name = name.strip()
        if name.startswith("env."):
            return self.env.get(name[4:])
        if name.startswith("inputs."):
            return self.inputs.get(name[7:])
        if name.startswith("matrix.job."):
            return self.matrix.get(name[11:])
        if name.startswith("secrets."):
            return ""
        if name == "github.workspace":
            return str(self.workspace)
        if name.startswith("github."):
            return ""
        raise SystemExit(f"Unbekannter Ausdruck: {name}")

    def expand(self, text):
        def repl(m):
            v = self.ref(m.group(1))
            return "" if v is None else str(v)
        return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", repl, str(text))

    def cond(self, expr):
        if expr is None:
            return True
        if isinstance(expr, bool):
            return expr
        e = str(expr).strip()
        m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", e, re.S)
        if m:
            e = m.group(1)
        py = re.sub(r"\b((?:env|inputs|secrets|github|matrix\.job)\.[\w-]+)",
                    lambda m: repr(self.ref(m.group(1))), e)
        py = py.replace("||", " or ").replace("&&", " and ")
        py = re.sub(r"\bnull\b", "None", py)
        py = re.sub(r"\btrue\b", "True", py)
        py = re.sub(r"\bfalse\b", "False", py)
        return bool(eval(py, {"__builtins__": {}}, {}))


def load_brand(brand):
    if not re.fullmatch(r"[a-z]+", brand):
        raise SystemExit(f"Ungültige Marke: {brand}")
    cfg = json.loads((RDGEN / "mhb" / brand / "config.json").read_text(encoding="utf-8"))
    custom = cfg.pop("custom")
    cfg["custom"] = base64.b64encode(json.dumps(custom).encode("ascii")).decode("ascii")
    return {k: str(v) for k, v in cfg.items()}


def base_env(work):
    home = Path.home()
    tools = home / "rdbuild" / "tools"
    path = [
        str(HERE / "shims"),
        str(tools / "nasm"),
        str(home / ".cargo" / "bin"),
        str(tools / "flutter" / "bin"),
        "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]  # bewusst ohne /usr/local/bin (alte Intel-Homebrew)
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM")}
    env.update({
        "PATH": ":".join(path),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "RDGEN": str(RDGEN),
        "VCPKG_ROOT": str(work / "vcpkg"),
        "VCPKG_BINARY_SOURCES": "clear",  # kein Binär-Cache auf der internen Platte
        "VCPKG_DISABLE_METRICS": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "FLUTTER_SUPPRESS_ANALYTICS": "true",
        "CI": "true",
    })
    return env


def prepare_source(src, version, env):
    log(f"Quellcode RustDesk {version}")
    if not (src / ".git").exists():
        run(f"git clone -q --depth 1 -b {version} --recurse-submodules --shallow-submodules "
            f"https://github.com/rustdesk/rustdesk.git '{src}'", src.parent, env)
    run("git reset -q --hard && git clean -fdq && "
        "git submodule foreach -q --recursive 'git reset -q --hard && git clean -fdq'", src, env)
    # Reste des letzten Builds, die cargo und Xcode sonst übernehmen würden
    for p in ("flutter/build/macos/Build/Products/Release",):
        shutil.rmtree(src / p, ignore_errors=True)
    for f in ("liblibrustdesk.dylib", "librustdesk.dylib", "service"):
        (src / "target" / "release" / f).unlink(missing_ok=True)


def bridge(src, version, work, env):
    cache = work / "bridge" / version
    if all((cache / f).exists() for f in BRIDGE_FILES):
        return cache
    log("Flutter-Rust-Bridge erzeugen (einmal je Version)")
    cargo_bin = Path.home() / ".cargo" / "bin"
    if not (cargo_bin / "cargo-expand").exists():
        run("cargo install cargo-expand --version 1.0.95 --locked", src, env)
    if not (cargo_bin / "flutter_rust_bridge_codegen").exists():
        run("cargo install flutter_rust_bridge_codegen --version 1.80.1 --features uuid --locked", src, env)
    run("sed -i '' -e 's/extended_text: 14.0.0/extended_text: 13.0.0/g' pubspec.yaml && flutter pub get",
        src / "flutter", env)
    run("flutter_rust_bridge_codegen --rust-input ./src/flutter_ffi.rs "
        "--dart-output ./flutter/lib/generated_bridge.dart "
        "--c-output ./flutter/macos/Runner/bridge_generated.h "
        "--llvm-path /opt/homebrew/opt/llvm", src, env)
    shutil.copy(src / "flutter/macos/Runner/bridge_generated.h", src / "flutter/ios/Runner/bridge_generated.h")
    for f in BRIDGE_FILES:
        (cache / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src / f, cache / f)
    return cache


def vcpkg_install(src, env):
    """Je Architektur ein eigenes Install-Verzeichnis, verlinkt nach $VCPKG_ROOT/installed/<triplet>.

    - host = Ziel: RustDesks vcpkg.json holt ffmpeg als host-Abhängigkeit, sonst landet es nicht im Ziel.
    - getrennte Wurzeln: Im Manifest-Modus entfernt vcpkg alles, was der aktuelle Lauf nicht braucht,
      ein gemeinsames installed/ würde die jeweils andere Architektur wieder abräumen.
    - Fehlt eine Bibliothek, nimmt clang still die Header der alten Intel-Homebrew aus /usr/local/include.
      Deshalb die Prüfung unten.
    """
    root = Path(env["VCPKG_ROOT"])
    linkdir = root / "installed"
    if linkdir.exists() and not linkdir.is_dir():
        linkdir.unlink()
    if (linkdir / "vcpkg").exists():  # alte gemeinsame Wurzel aus früheren Läufen
        shutil.rmtree(linkdir)
    linkdir.mkdir(exist_ok=True)
    for triplet in ("arm64-osx", "x64-osx"):
        log(f"vcpkg {triplet}")
        own = root / f"installed-{triplet}"
        run(f"'{root}/vcpkg' install --triplet {triplet} --host-triplet {triplet} --x-install-root='{own}' "
            f"--x-buildtrees-root='{root}/buildtrees' --downloads-root='{root}/downloads' "
            f"--x-packages-root='{root}/packages' --clean-after-build", src, env)
        link = linkdir / triplet
        if link.is_symlink() or link.exists():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(own / triplet)
    for triplet in ("arm64-osx", "x64-osx"):
        for lib in ("libavcodec", "vpx", "opus", "aom", "libyuv.h"):
            if not (linkdir / triplet / "include" / lib).exists():
                raise SystemExit(f"vcpkg {triplet}: {lib} fehlt")


def build_x86(src, env, min_macos):
    log("Rust-Teil für x86_64")
    feats = subprocess.run(["python3", "build.py", "--flutter", "--hwcodec", "--unix-file-copy-paste",
                            "--print-features"], cwd=src, env=env, capture_output=True, text=True)
    # stdout: kommagetrennte Liste (die Zeile "features: [...]" geht nach stderr)
    lines = [l.strip() for l in feats.stdout.splitlines() if l.strip()]
    features = lines[-1] if lines else ""
    if feats.returncode or not re.fullmatch(r"[\w-]+(,[\w-]+)*", features):
        raise SystemExit("build.py --print-features: keine Features gefunden\n" + feats.stdout + feats.stderr)
    if "screencapturekit" in features:
        raise SystemExit("x86_64 darf screencapturekit nicht enthalten")
    run(f"MACOSX_DEPLOYMENT_TARGET={min_macos} cargo build --locked --features {features} "
        f"--release --target {X86_TARGET}", src, env)


def patch_universal(src):
    """build.py: nach dem arm64-cargo-Build beide Architekturen zusammenführen, Xcode universal."""
    p = src / "build.py"
    t = p.read_text(encoding="utf-8")
    old_flutter = "f'FLUTTER_XCODE_ARCHS={mac_arch} FLUTTER_XCODE_ONLY_ACTIVE_ARCH=YES flutter build macos --release'"
    new_flutter = "'FLUTTER_XCODE_ARCHS=\"arm64 x86_64\" FLUTTER_XCODE_ONLY_ACTIVE_ARCH=NO flutter build macos --release'"
    old_copy = '    # copy dylib\n'
    lipo = (
        "    # MHB: Universal aus arm64 (target/release) und x86_64\n"
        "    for _f in ('liblibrustdesk.dylib', 'service'):\n"
        f"        system2(f'lipo -create target/release/{{_f}} target/{X86_TARGET}/release/{{_f}} "
        "-output target/release/{_f}.universal && mv target/release/{_f}.universal target/release/{_f}')\n"
    )
    for old in (old_flutter, old_copy):
        if t.count(old) != 1:
            raise SystemExit(f"build.py hat sich geändert, Stelle nicht eindeutig: {old.strip()[:80]}")
    t = t.replace(old_flutter, new_flutter).replace(old_copy, lipo + old_copy)
    p.write_text(t, encoding="utf-8")


def collect(src, work, brand, env):
    rel = src / "flutter/build/macos/Build/Products/Release"
    apps = [a for a in rel.glob("*.app")]
    if len(apps) != 1:
        raise SystemExit(f"Erwartet genau eine App in {rel}, gefunden: {apps}")
    app = apps[0]
    exe = app / "Contents/MacOS" / env["appname"]
    for binary in (exe, app / "Contents/MacOS/service", app / "Contents/Frameworks/liblibrustdesk.dylib"):
        if not binary.exists():
            raise SystemExit(f"Fehlt: {binary}")
        archs = subprocess.run(["lipo", "-archs", str(binary)], capture_output=True, text=True).stdout.split()
        print(f"{binary.relative_to(rel)}: {' '.join(archs)}")
        if sorted(archs) != ["arm64", "x86_64"]:
            raise SystemExit(f"Nicht universal: {binary}")
    bid = subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", str(app / "Contents/Info.plist")],
                         capture_output=True, text=True).stdout.strip()
    if not bid.startswith("com.mhbmarktundmotor."):
        raise SystemExit(f"Bundle-Kennung nicht gesetzt: {bid}")
    out = work / "out" / brand
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    run(f"ditto '{app}' '{out / app.name}'", rel, env)
    # Berechtigungen der App für das Signieren mit Developer ID (sign.sh)
    shutil.copy(src / "flutter/macos/Runner/Release.entitlements", out / "Release.entitlements")
    return out / app.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brand", choices=["pas", "carspider"])
    ap.add_argument("--version", default="1.5.1")
    ap.add_argument("--work", default=os.environ.get("RDBUILD_WORK", "/Volumes/SSD/rdbuild"))
    a = ap.parse_args()

    work = Path(a.work)
    if not work.is_dir():
        raise SystemExit(f"Arbeitsordner fehlt: {work} (externe SSD angeschlossen?)")
    src = work / f"src-{a.version}"
    env = base_env(work)

    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    job = wf["jobs"][JOB]
    matrix = next(j for j in job["strategy"]["matrix"]["job"] if j["target"] == ARM_TARGET)
    inputs = {"version": a.version, "brand": a.brand, "app_version": "", "upload-tag": ""}
    ctx = Ctx({}, inputs, matrix, src)
    for k, v in (wf.get("env") or {}).items():
        ctx.env[k] = ctx.expand(v)
    for k, v in (job.get("env") or {}).items():
        ctx.env[k] = ctx.expand(v)
    ctx.env.update(load_brand(a.brand))
    min_macos = "12.3"  # wie der Workflow für arm64; gilt für die ganze Universal-App

    gh_env = Path(tempfile.mkstemp(prefix="gh_env_")[1])
    gh_path = Path(tempfile.mkstemp(prefix="gh_path_")[1])

    t0 = time.time()
    prepared = False
    for i, step in enumerate(job["steps"], 1):
        name = step.get("name") or step.get("uses", f"Schritt {i}")
        uses = step.get("uses", "")
        if not ctx.cond(step.get("if")):
            print(f"--- übersprungen (if): {name}")
            continue
        if name in SKIP or any(uses.startswith(u) for u in SKIP_USES):
            print(f"--- lokal ersetzt: {name}")
            continue
        if name == "Checkout source code":
            if not prepared:
                prepare_source(src, a.version, env)
                cache = bridge(src, a.version, work, env)
                prepare_source(src, a.version, env)
                prepared = True
            continue
        if name == "Restore bridge files":
            for f in BRIDGE_FILES:
                shutil.copy(cache / f, src / f)
            print("--- Bridge-Dateien aus", cache)
            continue
        if name == "Install vcpkg dependencies":
            vcpkg_install(src, env)
            continue
        if uses and "run" not in step and not (isinstance(step.get("with"), dict) and "command" in step["with"]):
            raise SystemExit(f"Unbekannter uses-Schritt im Workflow: {name} ({uses}). build.py anpassen.")

        script = step.get("run") or step["with"]["command"]
        script = ctx.expand(script)
        if name == "Build rustdesk":
            # Schritte mit continue-on-error dürfen Server und Schlüssel nicht still verfehlen
            conf = (src / "libs/hbb_common/src/config.rs").read_text(encoding="utf-8")
            for must in (ctx.env["server"], ctx.env["key"]):
                if must not in conf:
                    raise SystemExit(f"config.rs enthält {must[:30]} nicht, Server/Schlüssel nicht gesetzt")
            if "rs-ny.rustdesk.com" in conf:
                raise SystemExit("config.rs enthält noch den RustDesk-Server")
            run(f"python3 '{RDGEN}/mhb/patch_source.py' '{src}' {a.brand}", src, env)
            build_x86(src, {**env, **ctx.env}, min_macos)
            patch_universal(src)
            # Logo in die Flutter-Assets, damit es in der App landet (der Workflow legt es neben die App)
            shutil.copy(RDGEN / "mhb" / a.brand / ctx.env["logolink_file"], src / "flutter/assets/logo.png")
        log(name)
        step_env = {**env, **ctx.env, "GITHUB_WORKSPACE": str(src), "GITHUB_ENV": str(gh_env),
                    "GITHUB_PATH": str(gh_path)}
        for k, v in (step.get("env") or {}).items():
            step_env[k] = ctx.expand(v)
        rc = run(script, src, step_env, check=False)
        if rc:
            if step.get("continue-on-error"):
                print(f"!!! Fehler ignoriert (continue-on-error): {name}")
            else:
                raise SystemExit(f"Schritt fehlgeschlagen: {name}")
        for line in gh_env.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                ctx.env[k] = v
        gh_env.write_text("")
        for line in gh_path.read_text().splitlines():
            if line.strip():
                env["PATH"] = line.strip() + ":" + env["PATH"]
        gh_path.write_text("")

    app = collect(src, work, a.brand, {**env, **ctx.env})
    log(f"Fertig in {int((time.time() - t0) / 60)} min: {app}")


if __name__ == "__main__":
    main()
