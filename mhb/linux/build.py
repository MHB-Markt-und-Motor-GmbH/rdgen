#!/usr/bin/env python3
"""Lokaler Linux-Build (x86_64) in WSL oder auf einem Linux-Rechner, mit Podman.

Gegenstück zu mhb/mac/build.py und mhb/win/build.py. Arbeitet die Jobs
`build-rustdesk-linux` und `build-appimage` aus .github/workflows/generator-linux.yml
der Reihe nach ab, mit den Werten aus mhb/<marke>/config.json.

Wie auf GitHub:
- Schritte, die dort direkt auf dem Runner (ubuntu-22.04) laufen, laufen hier in einem
  Ubuntu-22.04-Container (Abbild mhb-runner). Jeder Schritt in einem frischen Container,
  Arbeitsordner /workspace, vcpkg unter /opt/artifacts.
- Der eigentliche Bau (run-on-arch-action) läuft in Ubuntu 18.04, damit das Ergebnis auch
  auf älteren Systemen startet (glibc 2.27).
`uses:`-Schritte ersetzt das Skript durch lokale Entsprechungen, ein unbekannter bricht ab.
Nur x86_64; aarch64 bräuchte Emulation.

Voraussetzung: podman, python3-yaml. Die Flutter-Brücke kommt aus BRIDGE (vom Mac-Build).

Aufruf:  python3 mhb/linux/build.py pas|carspider [--version 1.5.1]
Ergebnis: <OUT>/<marke>/<Dateiname>-x86_64.deb und .AppImage
"""
import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RDGEN = HERE.parent.parent
_spec = importlib.util.spec_from_file_location("macbuild", HERE.parent / "mac" / "build.py")
mac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mac)
Ctx, load_brand, BRIDGE_FILES, log = mac.Ctx, mac.load_brand, mac.BRIDGE_FILES, mac.log

WF = RDGEN / ".github" / "workflows" / "generator-linux.yml"
WORK = Path(os.environ.get("RDBUILD_WORK", Path.home() / "rdbuild"))
OUT = Path(os.environ.get("RDBUILD_OUT", "/mnt/c/rdbuild/out"))
BRIDGE = Path(os.environ.get("RDBUILD_BRIDGE", "/mnt/c/rdbuild/bridge"))
ARCH = "x86_64"

RUNNER_FILE = """FROM docker.io/library/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
      sudo ca-certificates git curl wget python3 python3-pip imagemagick nasm cmake ninja-build \\
      pkg-config build-essential autoconf automake autoconf-archive libtool zip unzip tar xz-utils \\
      bison flex libva-dev file gnupg libarchive-tools libfuse2 squashfs-tools desktop-file-utils \\
      python-is-python3 fakeroot patchelf zsync libgdk-pixbuf2.0-bin libglib2.0-bin libgtk-3-bin clang llvm lld libx11-dev libxext-dev libxfixes-dev libdrm-dev libxcb1-dev
"""
# Wie die GitHub-Runner: clang und X11-Header sind dort vorinstalliert (FFmpeg: cuda_llvm, vaapi).
RUNNER = "localhost/mhb-runner:22.04-" + hashlib.sha256(RUNNER_FILE.encode()).hexdigest()[:10]

SKIP = {
    "Checkout Repository", "Export GitHub Actions cache environment variables",
    "Maximize build space", "Install dependencies",  # steckt im Runner-Abbild
    "Set Swap Space", "Free Space", "Install Rust toolchain", "Save Rust toolchain version",
    "Restore vcpkg installed cache", "Save vcpkg installed cache", "Install CMake for vcpkg on Linux ARM64",
    "Patch archlinux PKGBUILD", "Build archlinux package", "Rename archlinux package",
    "send file to rdgen server", "send file to api server",
}


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, **kw)


def ensure_runner():
    have = subprocess.run(["podman", "image", "exists", RUNNER]).returncode == 0
    if not have:
        log("Runner-Abbild Ubuntu 22.04 bauen")
        sh(["podman", "build", "-t", RUNNER, "-f", "-", str(WORK)], input=RUNNER_FILE.encode())


def arch_image(install_script):
    """Abbild für run-on-arch: Ubuntu 18.04 plus install-Skript, zwischengespeichert nach Inhalt."""
    tag = "localhost/mhb-arch:" + hashlib.sha256(install_script.encode()).hexdigest()[:12]
    if subprocess.run(["podman", "image", "exists", tag]).returncode != 0:
        log(f"Bau-Abbild Ubuntu 18.04 erzeugen ({tag})")
        ctxdir = WORK / "arch-image"
        shutil.rmtree(ctxdir, ignore_errors=True)
        ctxdir.mkdir(parents=True)
        (ctxdir / "install.sh").write_text("#!/bin/bash\nset -eo pipefail\n" + install_script, encoding="utf-8")
        (ctxdir / "Containerfile").write_text(
            "FROM docker.io/library/ubuntu:18.04\nENV DEBIAN_FRONTEND=noninteractive\n"
            "COPY install.sh /install.sh\nRUN bash /install.sh\n", encoding="utf-8")
        sh(["podman", "build", "-t", tag, str(ctxdir)])
    return tag


class Job:
    def __init__(self, ctx, workspace, gh):
        self.ctx, self.workspace, self.gh = ctx, workspace, gh

    def mounts(self):
        return ["-v", f"{self.workspace}:/workspace", "-v", f"{WORK / 'artifacts'}:/opt/artifacts",
                "-v", f"{self.gh}:/gh", "-w", "/workspace"]

    def env_file(self, extra=None):
        env = {**self.ctx.env, **(extra or {}), "GITHUB_ENV": "/gh/env", "GITHUB_PATH": "/gh/path",
               "GITHUB_WORKSPACE": "/workspace", "RUNNER_TEMP": "/gh/tmp", "CI": "true",
               "APPIMAGE_EXTRACT_AND_RUN": "1",
               # 16 Kerne, 15 GB RAM: volle Parallelität sprengt beim Rust-Bau den Speicher
               "CARGO_BUILD_JOBS": os.environ.get("CARGO_BUILD_JOBS", "8")}
        f = self.gh / "envfile"
        lines = []
        for k, v in env.items():
            v = str(v)
            if "\n" in v or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                continue
            lines.append(f"{k}={v}")
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f

    def read_back(self):
        envf = self.gh / "env"
        if envf.exists():
            text = envf.read_text(encoding="utf-8")
            for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)<<(\S+)\n(.*?)\n\2$", text, re.M | re.S):
                self.ctx.env[m.group(1)] = m.group(3)
            for line in text.splitlines():
                if "=" in line and "<<" not in line.split("=", 1)[0]:
                    k, v = line.split("=", 1)
                    self.ctx.env[k] = v
            envf.unlink()

    def bash(self, script, image=RUNNER, strict=True, extra_env=None):
        (self.gh / "tmp").mkdir(parents=True, exist_ok=True)
        step = self.gh / "step.sh"
        step.write_text(script, encoding="utf-8")
        flags = "-eo pipefail" if strict else "-e"
        sh(["podman", "run", "--rm", "--env-file", str(self.env_file(extra_env)), *self.mounts(),
            image, "bash", "--noprofile", "--norc", *flags.split(), "/gh/step.sh"])
        self.read_back()


def prepare_source(dest, version):
    cache = WORK / "cache" / f"rustdesk-{version}"
    if not (cache / ".git").exists():
        log(f"RustDesk {version} klonen")
        cache.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "clone", "-q", "--depth", "1", "-b", version, "--recurse-submodules", "--shallow-submodules",
            "https://github.com/rustdesk/rustdesk.git", str(cache)])
    if dest.exists():
        # Dateien aus Containern gehören evtl. fremden UIDs des Benutzernamensraums
        sh(["podman", "unshare", "rm", "-rf", str(dest)])
    sh(["cp", "-a", str(cache), str(dest)])


def vcpkg_setup(commit):
    root = WORK / "artifacts" / "vcpkg"
    if not (root / ".git").exists():
        log("vcpkg klonen")
        root.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "clone", "-q", "https://github.com/microsoft/vcpkg.git", str(root)])
    sh(["git", "-C", str(root), "fetch", "-q", "origin"])
    sh(["git", "-C", str(root), "checkout", "-q", commit])
    return root


def run_steps(job_name, ctx, workspace, gh, brand, version, outdir, deb=None):
    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    job = Job(ctx, workspace, gh)
    for step in wf["jobs"][job_name]["steps"]:
        name = step.get("name") or step.get("uses", "")
        if name in SKIP:
            log(f"--- übersprungen: {name}")
            continue
        if not ctx.cond(step.get("if")):
            log(f"--- übersprungen (if): {name}")
            continue
        log(name)
        uses = step.get("uses", "")
        w = {k: ctx.expand(v) if isinstance(v, str) else v for k, v in (step.get("with") or {}).items()}
        step_env = {k: ctx.expand(v) for k, v in (step.get("env") or {}).items()}
        if uses.startswith("actions/checkout"):
            if w.get("repository") != "rustdesk/rustdesk":
                raise SystemExit(f"Unerwarteter Checkout: {w}")
            prepare_source(workspace, version)
        elif uses == "./.github/actions/load-config":
            ctx.env.update(load_brand(brand))
        elif uses.startswith("lukka/run-vcpkg"):
            vcpkg_setup(w["vcpkgGitCommitId"])
            job.bash("cd /opt/artifacts/vcpkg && ./bootstrap-vcpkg.sh -disableMetrics")
            ctx.env["VCPKG_ROOT"] = "/opt/artifacts/vcpkg"
        elif uses.startswith("actions/download-artifact"):
            if w.get("name") == "bridge-artifact":
                src = BRIDGE / version
                for rel in BRIDGE_FILES:
                    if not (src / rel).exists():
                        raise SystemExit(f"Bridge-Datei fehlt: {src / rel} (vom Mac-Build)")
                    (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src / rel, workspace / rel)
            elif w.get("name", "").endswith(".deb") and deb:
                shutil.copy(deb, workspace / Path(deb).name)
            else:
                raise SystemExit(f"Unbekanntes Artefakt: {w.get('name')}")
        elif uses.startswith("actions/upload-artifact"):
            path = w["path"].replace("./", "", 1)
            f = workspace / path
            if not f.exists():
                raise SystemExit(f"Ergebnis fehlt: {f}")
            outdir.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, outdir / f.name)
            log(f"Ergebnis: {outdir / f.name}")
        elif uses.startswith("nick-fields/retry"):
            if (w.get("shell") or "bash") != "bash":
                raise SystemExit(f"Unbekannte Shell: {w.get('shell')}")
            job.bash(w["command"], extra_env=step_env)
        elif uses.startswith("rustdesk-org/run-on-arch-action"):
            if w.get("arch") != ARCH or w.get("distro") != "ubuntu18.04":
                raise SystemExit(f"Nur x86_64/ubuntu18.04: {w.get('arch')} {w.get('distro')}")
            if w.get("setup"):
                job.bash(w["setup"])
            image = arch_image(w["install"])
            # Wie run-on-arch: ohne pipefail (`dpkg-deb -c | grep -q` bricht sonst mit SIGPIPE ab)
            job.bash(w["run"], image=image, strict=False, extra_env=step_env)
        elif uses:
            raise SystemExit(f"Unbekannter uses-Schritt: {uses} ({name})")
        else:
            shell = step.get("shell")
            if shell not in (None, "bash"):
                raise SystemExit(f"Unbekannte Shell {shell} in {name}")
            job.bash(ctx.expand(step["run"]), strict=(shell == "bash"), extra_env=step_env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brand", choices=["pas", "carspider"])
    ap.add_argument("--version", default="1.5.1")
    ap.add_argument("--appimage-only", action="store_true", help="vorhandene .deb nehmen, nur das AppImage bauen")
    a = ap.parse_args()
    t0 = time.time()

    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    (WORK / "artifacts").mkdir(parents=True, exist_ok=True)
    ensure_runner()

    brand_cfg = load_brand(a.brand)
    outdir = OUT / a.brand
    inputs = {"version": a.version, "brand": a.brand, "app_version": "", "upload-tag": ""}

    def new_ctx(job_name):
        job = wf["jobs"][job_name]
        matrix = next(m for m in job["strategy"]["matrix"]["job"] if m["arch"] == ARCH)
        ctx = Ctx({}, inputs, matrix, "/workspace")
        for k, v in (wf.get("env") or {}).items():
            ctx.env[k] = ctx.expand(v)
        ctx.env.update(brand_cfg)
        # Auf GitHub liefert „Save Rust toolchain version“ die volle Fassung des Runners.
        ctx.env["RUST_TOOLCHAIN_VERSION"] = ctx.env["RUST_VERSION"] + ".0"
        return ctx

    # 1. .deb (und rpm) in Ubuntu 18.04
    if not a.appimage_only:
        ws = WORK / f"src-{a.version}"
        gh = WORK / "gh-deb"
        shutil.rmtree(gh, ignore_errors=True)
        gh.mkdir(parents=True)
        run_steps("build-rustdesk-linux", new_ctx("build-rustdesk-linux"), ws, gh, a.brand, a.version, outdir)
    deb = outdir / f"{brand_cfg['filename']}-{ARCH}.deb"
    if not deb.exists():
        raise SystemExit(f".deb fehlt: {deb}")

    # 2. AppImage aus der .deb
    ws2 = WORK / f"appimage-{a.version}"
    gh2 = WORK / "gh-appimage"
    shutil.rmtree(gh2, ignore_errors=True)
    gh2.mkdir(parents=True)
    run_steps("build-appimage", new_ctx("build-appimage"), ws2, gh2, a.brand, a.version, outdir, deb=deb)

    for f in sorted(outdir.glob(f"{brand_cfg['filename']}-{ARCH}.*")):
        log(f"{f.name}  {f.stat().st_size // 1048576} MB")
    log(f"Fertig in {int((time.time() - t0) / 60)} min: {outdir}")


if __name__ == "__main__":
    main()
