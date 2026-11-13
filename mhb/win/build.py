#!/usr/bin/env python3
"""Lokaler Windows-Build (x64), Gegenstück zu mhb/mac/build.py.

Arbeitet die Schritte des Jobs `build-for-windows-flutter` aus
.github/workflows/generator-windows.yml der Reihe nach ab, mit den Werten aus
mhb/<marke>/config.json. `uses:`-Schritte ersetzt das Skript durch lokale
Entsprechungen, ein unbekannter `uses:`-Schritt bricht ab.

Shells wie auf dem GitHub-Runner: ohne Angabe PowerShell 7 (C:\\rdbuild\\tools\\pwsh),
`bash` über Git Bash, `python` über Python 3.

Signieren (--sign): vor dem Packen die inneren Dateien, danach die äußere .exe, mit
signtool und dem Schlüssel auf dem SafeNet-USB-Stick (PIN-Abfrage am Bildschirm),
Zeitstempel DigiCert. Ohne Build nur signieren: --sign-only <datei.exe>.

Aufruf:  python mhb\\win\\build.py pas|carspider [--version 1.5.1] [--sign]
Ergebnis: C:\\rdbuild\\out\\<marke>\\<Dateiname>.exe
Werkzeuge: setup-admin.ps1 und setup.ps1.
"""
import argparse
import glob
import importlib.util
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
_spec = importlib.util.spec_from_file_location("macbuild", HERE.parent / "mac" / "build.py")
mac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mac)
Ctx, load_brand, BRIDGE_FILES, log = mac.Ctx, mac.load_brand, mac.BRIDGE_FILES, mac.log

WF = RDGEN / ".github" / "workflows" / "generator-windows.yml"
JOB = "build-for-windows-flutter"
WORK = Path(os.environ.get("RDBUILD_WORK", r"C:\rdbuild"))
TOOLS = WORK / "tools"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
LLVM = r"C:\Program Files\LLVM"
FORK_RAW = "https://raw.githubusercontent.com/MHB-Markt-und-Motor-GmbH/rdgen/refs/heads/mhb/"
TIMESTAMP = "http://timestamp.digicert.com"
TOPMOST_REPO = "https://github.com/rustdesk-org/RustDeskTempTopMostWindow"
TOPMOST_COMMIT = "ecd8d6a139eee76845ea66423fb739af450fda90"  # wie third-party-RustDeskTempTopMostWindow.yml

SKIP = {
    "Checkout Repository", "Load brand config", "Export GitHub Actions cache environment variables",
    "Install ImageMagick on Windows", "Install LLVM and Clang", "Install flutter", "Install Rust toolchain",
    "Restore vcpkg installed cache", "Setup vcpkg", "Save vcpkg installed cache",
    # Signieren lokal mit dem USB-Stick (--sign), kein Signierdienst
    "zip dlls", "sign dlls", "unzip dlls", "zip exe and msi", "sign exe and msi", "unzip exe and msi",
    # MSI wird nicht ausgeliefert
    "Add MSBuild to PATH", "Build msi", "rename rustdesk.msi to filename.msi",
    "Upload artifact", "send file to rdgen server", "send file to api server",
}
SKIP_USES = {"Swatinem/rust-cache"}


def python_exe():
    return sys.executable


def make_shims():
    """python3 auf das echte Python (unter Windows ist python3 sonst der Store-Platzhalter)."""
    ps = WORK / "shims" / "ps"
    sh = WORK / "shims" / "sh"
    ps.mkdir(parents=True, exist_ok=True)
    sh.mkdir(parents=True, exist_ok=True)
    py = python_exe()
    for n in ("python3", "pip3"):
        arg = "" if n == "python3" else " -m pip"
        (ps / f"{n}.cmd").write_text(f'@"{py}"{arg} %*\r\n', encoding="ascii")
        posix = "/" + py[0].lower() + py[2:].replace("\\", "/")
        (sh / n).write_text(f'#!/bin/sh\nexec "{posix}"{arg} "$@"\n', encoding="ascii", newline="\n")
    return ps, sh


def base_env(ps_shims):
    env = dict(os.environ)
    path = [str(ps_shims), str(TOOLS / "flutter" / "bin"), str(Path.home() / ".cargo" / "bin"), rf"{LLVM}\bin"]
    path += sorted(glob.glob(r"C:\Program Files\ImageMagick*"))[-1:]  # magick.exe
    path += [p for p in env.get("PATH", "").split(";") if p and "WindowsApps" not in p]
    env.update({
        "PATH": ";".join(path),
        "VCPKG_ROOT": str(WORK / "vcpkg"),
        "VCPKG_BINARY_SOURCES": "clear",
        "VCPKG_DISABLE_METRICS": "1",
        "LIBCLANG_PATH": rf"{LLVM}\bin",
        "FLUTTER_SUPPRESS_ANALYTICS": "true",
        "PYTHONUTF8": "1",
        "CI": "true",
    })
    return env


def run_shell(shell, script, cwd, env, sh_shims):
    """Wie der GitHub-Runner: pwsh mit Abbruch bei Fehlern, bash -eo pipefail, python."""
    shell = (shell or "pwsh").strip()
    tmpdir = Path(tempfile.mkdtemp(prefix="step_", dir=WORK))
    try:
        if shell in ("pwsh", "powershell"):
            f = tmpdir / "step.ps1"
            body = ("$ErrorActionPreference = 'stop'\n$ProgressPreference = 'SilentlyContinue'\n" + script +
                    "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n")
            f.write_text(body, encoding="utf-8-sig")
            pwsh = TOOLS / "pwsh" / "pwsh.exe"  # PowerShell 7 wie auf dem Runner (5.1 kennt z. B. || nicht)
            if not pwsh.exists():
                raise SystemExit(f"{pwsh} fehlt (setup.ps1)")
            cmd = [str(pwsh), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-Command", f". '{f}'"]
        elif shell == "bash":
            f = tmpdir / "step.sh"
            f.write_text(script, encoding="utf-8", newline="\n")
            env = dict(env)
            env["MHB_SH_SHIMS"] = "/" + str(sh_shims)[0].lower() + str(sh_shims)[2:].replace("\\", "/")
            posix = "/" + str(f)[0].lower() + str(f)[2:].replace("\\", "/")
            # Git Bash stellt /mingw64/bin und /usr/bin vorn an, danach die eigenen Shims
            cmd = [GIT_BASH, "--noprofile", "--norc", "-c",
                   f'export PATH="$MHB_SH_SHIMS:$PATH"; exec bash --noprofile --norc -eo pipefail "{posix}"']
        elif shell == "python":
            f = tmpdir / "step.py"
            f.write_text(script, encoding="utf-8")
            cmd = [python_exe(), str(f)]
        else:
            raise SystemExit(f"Unbekannte Shell: {shell}")
        return subprocess.run(cmd, cwd=cwd, env=env).returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def localize(script, flutter_root):
    """Downloads aus diesem Fork aus dem lokalen Checkout, feste Runner-Pfade auf lokale Pfade."""
    def iwr(m):
        rel = m.group(1)
        return f"Copy-Item -LiteralPath '{RDGEN / rel}' -Destination {m.group(2)}"
    script = re.sub(r"Invoke-WebRequest\s+-Uri\s+" + re.escape(FORK_RAW) + r"(\S+)\s+-OutFile\s+(\S+)", iwr, script)
    script = re.sub(r"C:/hostedtoolcache/windows/flutter/stable-[\d.]+-x64", flutter_root.as_posix(), script)
    script = script.replace("Invoke-WebRequest ", "Invoke-WebRequest -UseBasicParsing ")
    if FORK_RAW in script:
        raise SystemExit("Download aus dem Fork, den build.py nicht auf den lokalen Checkout umstellt:\n" + script)
    return script


def prepare_source(src, version, env):
    log(f"Quellcode RustDesk {version}")
    if not (src / ".git").exists():
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", "--depth", "1", "-b", version,
                        "--recurse-submodules", "--shallow-submodules",
                        "https://github.com/rustdesk/rustdesk.git", str(src)], check=True, env=env)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=src, check=True)
    # -x: auch ignorierte Reste wie *.bak der Workflow-Schritte entfernen, Build-Zwischenstände behalten
    subprocess.run("git reset -q --hard && git clean -fdqx -e /target -e /flutter/build -e /flutter/.dart_tool "
                   "-e /vcpkg_installed && git submodule foreach -q --recursive "
                   "\"git reset -q --hard && git clean -fdq\"", cwd=src, shell=True, check=True, env=env)
    for p in ("rustdesk", "SignOutput", "temp-repo", "printer-adapter", "flutter/build/windows/x64/runner/Release"):
        shutil.rmtree(src / p, ignore_errors=True)


def bridge_files(src, version):
    cache = WORK / "bridge" / version
    missing = [f for f in BRIDGE_FILES if not (cache / f).exists()]
    if missing:
        raise SystemExit(f"Bridge-Dateien fehlen in {cache} (vom Mac: /Volumes/SSD/rdbuild/bridge/{version}): {missing}")
    for f in BRIDGE_FILES:
        shutil.copy(cache / f, src / f)
    print("--- Bridge-Dateien aus", cache)


def msbuild():
    vswhere = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    out = subprocess.run([vswhere, "-latest", "-products", "*", "-requires", "Microsoft.Component.MSBuild",
                          "-find", r"MSBuild\**\Bin\MSBuild.exe"], capture_output=True, text=True).stdout.splitlines()
    out = [o.strip() for o in out if o.strip()]
    if not out:
        raise SystemExit("MSBuild nicht gefunden (Build Tools installiert?)")
    return out[0]


def topmost_dll(env):
    """WindowInjection.dll wie third-party-RustDeskTempTopMostWindow.yml (ohne eigenes Privatsphäre-Bild)."""
    dll = WORK / "topmost" / "WindowInjection" / "x64" / "Release" / "WindowInjection.dll"
    if dll.exists():
        return dll
    log("RustDeskTempTopMostWindow bauen")
    d = WORK / "topmost"
    if not (d / ".git").exists():
        subprocess.run(["git", "clone", "-q", TOPMOST_REPO, str(d)], check=True, env=env)
    subprocess.run(["git", "checkout", "-q", TOPMOST_COMMIT], cwd=d, check=True, env=env)
    subprocess.run([msbuild(), r"WindowInjection\WindowInjection.vcxproj", "-p:Configuration=Release",
                    "-p:Platform=x64", "/p:TargetVersion=Windows10",
                    # Projekt verlangt v142 (VS 2019, auf dem GitHub-Runner vorhanden), hier VS 2022
                    "-p:PlatformToolset=v143"], cwd=d, check=True, env=env)
    if not dll.exists():
        raise SystemExit(f"{dll} fehlt nach dem Bauen")
    return dll


def signtool():
    hits = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\10.*\x64\signtool.exe"))
    if not hits:
        raise SystemExit("signtool.exe nicht gefunden")
    return hits[-1]


KSP = "SafeNet Smart Card Key Storage Provider"
_EXPORT_CERT = r"""
$k = [System.Security.Cryptography.CngKey]::Open($env:MHB_KC,
    [System.Security.Cryptography.CngProvider]::new($env:MHB_KSP),
    [System.Security.Cryptography.CngKeyOpenOptions]::Silent)
$raw = $k.GetProperty('SmartCardKeyCertificate', [System.Security.Cryptography.CngPropertyOptions]::None).GetValue()
$c = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($raw)
[IO.File]::WriteAllBytes($env:MHB_CER, $c.RawData)
$c.Subject + ' | bis ' + $c.NotAfter.ToString('dd.MM.yyyy')
"""


def token_args():
    """Zertifikat und Schlüssel direkt vom SafeNet-Stick, ohne Umweg über den Zertifikatspeicher.

    Der Stick-Client übernimmt das Zertifikat nicht immer in CurrentUser\\My, dann findet
    signtool /a nichts. Deshalb: Zertifikat aus dem Schlüsselcontainer lesen und mit
    /f /csp /kc signieren. Die PIN fragt der SafeNet-Client beim Signieren am Bildschirm ab.
    """
    info = subprocess.run(["certutil", "-scinfo", "-silent"], capture_output=True, text=True,
                          encoding="cp850", errors="replace").stdout
    containers = sorted(set(re.findall(r"p11#[0-9a-f]+", info)))
    if not containers:
        raise SystemExit("USB-Stick nicht gefunden (certutil -scinfo)")
    cer = WORK / "sign" / "codesign.cer"
    cer.parent.mkdir(parents=True, exist_ok=True)
    seen = []
    for kc in containers:
        cer.unlink(missing_ok=True)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", _EXPORT_CERT], capture_output=True,
                           text=True, env={**os.environ, "MHB_KC": kc, "MHB_KSP": KSP, "MHB_CER": str(cer)})
        subject = r.stdout.strip()
        seen.append(f"{kc}: {subject or 'kein Zertifikat'}")
        if not r.returncode and cer.exists() and "MHB Markt und Motor GmbH" in subject:
            log(f"Zertifikat: {subject}")
            return ["/f", str(cer), "/csp", KSP, "/kc", kc]
    raise SystemExit("Kein MHB-Zertifikat auf dem Stick: " + "; ".join(seen))


def sign(files, description):
    files = [str(f) for f in files]
    if not files:
        return
    log(f"Signieren ({len(files)} Dateien, PIN-Abfrage des USB-Sticks am Bildschirm)")
    subprocess.run([signtool(), "sign", *token_args(), "/fd", "sha256", "/tr", TIMESTAMP, "/td", "sha256",
                    "/d", description, *files], check=True)
    subprocess.run([signtool(), "verify", "/pa", "/q", *files], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brand", choices=["pas", "carspider"])
    ap.add_argument("--version", default="1.5.1")
    ap.add_argument("--sign", action="store_true", help="mit dem USB-Stick signieren")
    ap.add_argument("--sign-only", metavar="EXE", help="nur diese Datei signieren, kein Build")
    a = ap.parse_args()

    if a.sign_only:
        sign([a.sign_only], load_brand(a.brand)["filename"].replace("-", " "))
        return
    if a.sign:
        token_args()  # Stick vor dem langen Build prüfen

    src = WORK / f"src-{a.version}"
    ps_shims, sh_shims = make_shims()
    env = base_env(ps_shims)
    flutter_root = TOOLS / "flutter"

    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    job = wf["jobs"][JOB]
    matrix = job["strategy"]["matrix"]["job"][0]
    inputs = {"version": a.version, "brand": a.brand, "app_version": "", "upload-tag": ""}
    ctx = Ctx({}, inputs, matrix, src)
    for k, v in (wf.get("env") or {}).items():
        ctx.env[k] = ctx.expand(v)
    for k, v in (job.get("env") or {}).items():
        ctx.env[k] = ctx.expand(v)
    ctx.env.update(load_brand(a.brand))
    display = load_brand(a.brand)["filename"].replace("-", " ")

    gh_env = Path(tempfile.mkstemp(prefix="gh_env_", dir=WORK)[1])
    gh_path = Path(tempfile.mkstemp(prefix="gh_path_", dir=WORK)[1])
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
                prepared = True
            continue
        if name == "Restore bridge files":
            bridge_files(src, a.version)
            continue
        if name == "Checkout printer-adapter crate":
            shutil.copytree(RDGEN / "printer-adapter", src / "temp-repo" / "printer-adapter",
                            ignore=shutil.ignore_patterns("target"))
            print("--- printer-adapter aus dem lokalen Fork")
            continue
        if name == "Download RustDeskTempTopMostWindow artifacts":
            (src / "rustdesk").mkdir(exist_ok=True)
            shutil.copy(topmost_dll(env), src / "rustdesk" / "WindowInjection.dll")
            print("--- WindowInjection.dll")
            continue
        if uses and "run" not in step and not (isinstance(step.get("with"), dict) and "command" in step["with"]):
            raise SystemExit(f"Unbekannter uses-Schritt im Workflow: {name} ({uses}). build.py anpassen.")

        if "run" in step:
            script, shell = step["run"], step.get("shell")
        else:
            script, shell = step["with"]["command"], step["with"].get("shell")
        script = localize(ctx.expand(script), flutter_root)

        if name == "Patch flutter":
            # Der Runner hat jedes Mal ein frisches Flutter, lokal ist der Patch nach dem ersten Lauf schon drin
            diff = src / ".github/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff"
            if diff.exists() and subprocess.run(["git", "apply", "--reverse", "--check", str(diff)],
                                                cwd=flutter_root, capture_output=True).returncode == 0:
                print("--- Flutter-Patch ist schon angewendet")
                continue
        if name == "Build rustdesk":
            subprocess.run([python_exe(), str(RDGEN / "mhb" / "patch_source.py"), str(src), a.brand], check=True)
            conf = (src / "libs/hbb_common/src/config.rs").read_text(encoding="utf-8")
            for must in (ctx.env["server"], ctx.env["key"]):
                if must not in conf:
                    raise SystemExit(f"config.rs enthält {must[:30]} nicht, Server/Schlüssel nicht gesetzt")
            if "rs-ny.rustdesk.com" in conf:
                raise SystemExit("config.rs enthält noch den RustDesk-Server")
        if name == "Build self-extracted executable" and a.sign:
            inner = [p for p in (src / "rustdesk").glob("*") if p.suffix.lower() in (".exe", ".dll")]
            sign(inner, display)

        log(name)
        step_env = {**env, **ctx.env, "GITHUB_WORKSPACE": str(src), "GITHUB_ENV": str(gh_env),
                    "GITHUB_PATH": str(gh_path)}
        for k, v in (step.get("env") or {}).items():
            step_env[k] = ctx.expand(v)
        rc = run_shell(shell, script, src, step_env, sh_shims)
        if rc:
            if step.get("continue-on-error") or (step.get("with") or {}).get("continue_on_error"):
                print(f"!!! Fehler ignoriert (continue-on-error): {name}")
            else:
                raise SystemExit(f"Schritt fehlgeschlagen: {name}")
        for line in gh_env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                ctx.env[k.strip()] = v.strip()
        gh_env.write_text("")
        for line in gh_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.strip():
                env["PATH"] = line.strip() + ";" + env["PATH"]
        gh_path.write_text("")

    exe = src / "SignOutput" / f"{ctx.env['filename']}.exe"
    if not exe.exists():
        raise SystemExit(f"Ergebnis fehlt: {exe}")
    out = WORK / "out" / a.brand
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copy(exe, out / exe.name)
    if a.sign:
        sign([out / exe.name], display)
    log(f"Fertig in {int((time.time() - t0) / 60)} min: {out / exe.name}")


if __name__ == "__main__":
    main()
