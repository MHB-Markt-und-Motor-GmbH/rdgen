"""Stellt die rdgen-Workflows auf feste Markeneinstellungen um (ohne rdgen-Server).

- Eingabe `brand` (pas/carspider) statt `zip_url`; Werte aus mhb/<brand>/config.json
- keine Abrufe von bryangerlach/rdgen@master zur Bauzeit, sondern aus diesem Fork (Zweig mhb)
- Symbol und Logo aus mhb/<brand>/
- Ergebnis als Actions-Artefakt statt Upload an den rdgen-Server

Nach einem Abgleich mit upstream erneut ausführen: python mhb/patch_workflows.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
FORK_RAW = "https://raw.githubusercontent.com/MHB-Markt-und-Motor-GmbH/rdgen/refs/heads/mhb/"
UP_RAW = "https://raw.githubusercontent.com/bryangerlach/rdgen/refs/heads/master/"


def sub(text, old, new, count=None, regex=False, name=""):
    if regex:
        result, n = re.subn(old, new, text, flags=re.M)
    else:
        n = text.count(old)
        result = text.replace(old, new)
    if n == 0 or (count is not None and n != count):
        sys.exit(f"{name}: expected {count or '>=1'} match(es) for {old[:70]!r}, found {n}")
    return result


INPUT_OLD = """      zip_url:
        description: 'url to zip of json'
        required: true
        default: ''
        type: string
"""
INPUT_NEW = """      brand:
        description: 'Marke (pas oder carspider)'
        required: true
        default: 'pas'
        type: choice
        options:
          - pas
          - carspider
"""

SETUP_OLD = """  setup:
    uses: ./.github/workflows/fetch-encrypted-secrets.yml
    with:
      zip_url_json: ${{ inputs.zip_url }}
"""
SETUP_NEW = """  setup:
    runs-on: ubuntu-latest
    steps:
      - run: echo "brand ${{ inputs.brand }}, RustDesk ${{ inputs.version }}"
"""

LOAD_RE = (r"^( +)- uses: actions/download-artifact@v\d+\n"
           r"\1  with:\n"
           r"\1    name: encrypted-secrets-zip\n\n"
           r"\1- name: Load Secrets\n"
           r"\1  uses: \./\.github/actions/decrypt-secrets\n"
           r"\1  with:\n"
           r"\1    zip_password: \$\{\{ secrets\.ZIP_PASSWORD \}\}\n\n"
           r"\1- name: Finalize and Cleanup zip/json\n"
           r"(?:\1  .*\n)+")
LOAD_NEW = (r"\1- name: Load brand config\n"
            r"\1  uses: ./.github/actions/load-config\n"
            r"\1  with:\n"
            r"\1    brand: ${{ inputs.brand }}\n\n")

CLEANUP_RE = r"\n  cleanup:\n    needs: \[[\w-]+\]\n(?:    .*\n|\n)*$"

GETPNG_RE = r'/get_png\?filename=\$\{\{ env\.(\w+)_file \}\}"?&"?uuid=\$\{\{ env\.\w+_uuid \}\}'


def common(text, name):
    text = sub(text, INPUT_OLD, INPUT_NEW, 1, name=name)
    text = sub(text, SETUP_OLD, SETUP_NEW, 1, name=name)
    text = sub(text, LOAD_RE, LOAD_NEW, 1, regex=True, name=name)
    text = sub(text, UP_RAW, FORK_RAW, name=name)
    text = sub(text, GETPNG_RE, r"/${{ env.\1_file }}", regex=True, name=name)
    text = sub(text, CLEANUP_RE, "\n", 1, regex=True, name=name)
    return text


def windows():
    p = WF / "generator-windows.yml"
    t = p.read_text(encoding="utf-8")
    t = common(t, p.name)
    t = sub(t, "          repository: bryangerlach/rdgen\n          ref: master\n",
            "          repository: MHB-Markt-und-Motor-GmbH/rdgen\n          ref: mhb\n", 1, name=p.name)
    t = sub(t, "      - name: send file to rdgen server\n", """      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: ${{ env.filename }}-windows
          path: |
            ./SignOutput/${{ env.filename }}.exe
            ./SignOutput/${{ env.filename }}.msi
          if-no-files-found: error
          retention-days: 30

      - name: send file to rdgen server
""", 1, name=p.name)
    p.write_text(t, encoding="utf-8", newline="\n")
    print("patched", p.name)


def macos():
    p = WF / "generator-macos.yml"
    t = p.read_text(encoding="utf-8")
    t = common(t, p.name)
    # macos-15 (arm64) bekam mehrfach keinen Runner (Kapazität); macos-14 nicht mehr unterstützt, macos-latest ist arm64
    t = sub(t, "              os: macos-15,\n", "              os: macos-14,\n", 1, name=p.name)
    t = sub(t, "      - name: send file to rdgen server\n", """      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: ${{ env.filename }}-macos-${{ matrix.job.arch }}
          path: ${{ github.workspace }}/${{ env.filename }}-${{ matrix.job.arch }}.dmg
          if-no-files-found: error
          retention-days: 30

      - name: send file to rdgen server
""", 1, name=p.name)
    p.write_text(t, encoding="utf-8", newline="\n")
    print("patched", p.name)


def topmost():
    p = WF / "third-party-RustDeskTempTopMostWindow.yml"
    t = p.read_text(encoding="utf-8")
    # aufrufender Workflow reicht die Marke durch
    t = sub(t, "    inputs:\n        rustdesk-version:\n",
            "    inputs:\n        brand:\n            type: string\n            default: 'pas'\n        rustdesk-version:\n", 1, name=p.name)
    t = sub(t, LOAD_RE, LOAD_NEW.replace("inputs.brand", "inputs.brand"), 1, regex=True, name=p.name)
    t = sub(t, GETPNG_RE, r"/${{ env.\1_file }}", regex=True, name=p.name)
    p.write_text(t, encoding="utf-8", newline="\n")
    print("patched", p.name)

    w = WF / "generator-windows.yml"
    t = w.read_text(encoding="utf-8")
    t = sub(t, "    with:\n      rustdesk-version: ${{ inputs.version }}\n",
            "    with:\n      brand: ${{ inputs.brand }}\n      rustdesk-version: ${{ inputs.version }}\n", 1, name=w.name)
    w.write_text(t, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    windows()
    macos()
    topmost()
    for f in ("generator-windows.yml", "generator-macos.yml", "third-party-RustDeskTempTopMostWindow.yml"):
        t = (WF / f).read_text(encoding="utf-8")
        for bad in ("bryangerlach", "encrypted-secrets-zip", "zip_url", "get_png"):
            if bad in t:
                print(f"WARN {f}: still contains {bad}")
