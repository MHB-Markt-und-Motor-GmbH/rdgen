# Einmalige Einrichtung ohne Adminrechte (nach setup-admin.ps1):
#   Rust 1.75 (wie generator-windows.yml), Flutter 3.24.5, vcpkg auf dem Workflow-Stand.
# Alles unter C:\rdbuild, nichts an PATH oder Profil.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\rdbuild'
$tools = "$root\tools"
$FlutterVersion = '3.24.5'
$RustVersion = '1.75'
$VcpkgCommit = '9e593bb18ea69cc5095e012465dcd675a822ed0d'
New-Item -ItemType Directory -Force $tools, "$root\downloads" | Out-Null

Write-Host "=== PowerShell 7 (die Workflow-Schritte nutzen pwsh-Syntax wie ||)"
if (-not (Test-Path "$tools\pwsh\pwsh.exe")) {
    $zip = "$root\downloads\pwsh.zip"
    Invoke-WebRequest -Uri 'https://github.com/PowerShell/PowerShell/releases/download/v7.4.22/PowerShell-7.4.22-win-x64.zip' -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath "$tools\pwsh" -Force
}
& "$tools\pwsh\pwsh.exe" -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'

Write-Host "=== Rust $RustVersion"
$rustup = "$env:USERPROFILE\.cargo\bin\rustup.exe"
if (-not (Test-Path $rustup)) {
    $init = "$root\downloads\rustup-init.exe"
    Invoke-WebRequest -Uri 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe' -OutFile $init -UseBasicParsing
    & $init -y --no-modify-path --default-toolchain none --profile minimal
    if ($LASTEXITCODE) { throw "rustup-init $LASTEXITCODE" }
}
& $rustup toolchain install $RustVersion --profile minimal -c rustfmt -t x86_64-pc-windows-msvc
& $rustup default $RustVersion

Write-Host "=== Flutter $FlutterVersion"
if (-not (Test-Path "$tools\flutter\bin\flutter.bat")) {
    git clone --depth 1 -b $FlutterVersion https://github.com/flutter/flutter.git "$tools\flutter"
    if ($LASTEXITCODE) { throw "git clone flutter $LASTEXITCODE" }
}
$env:PATH = "$tools\flutter\bin;$env:PATH"
flutter config --no-analytics --no-cli-animations | Out-Null
flutter precache --windows

Write-Host "=== vcpkg"
if (-not (Test-Path "$root\vcpkg\.git")) {
    git clone https://github.com/microsoft/vcpkg.git "$root\vcpkg"
}
git -C "$root\vcpkg" fetch -q origin
git -C "$root\vcpkg" checkout -q $VcpkgCommit
& "$root\vcpkg\bootstrap-vcpkg.bat" -disableMetrics

Write-Host "=== Python-Pakete für den Packer"
python -m pip install --user --quiet --disable-pip-version-check pyyaml

Write-Host "=== Fertig"
