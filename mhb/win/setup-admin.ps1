# Einmalige Einrichtung mit Adminrechten (Windows PowerShell 5.1, als Administrator):
#   Visual Studio 2022 Build Tools (C++), LLVM 15.0.6 (wie generator-windows.yml), ImageMagick.
# Protokoll: C:\rdbuild\setup-admin.log
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\rdbuild'
New-Item -ItemType Directory -Force "$root\downloads" | Out-Null
Start-Transcript -Path "$root\setup-admin.log" -Append | Out-Null

function Get-File($url, $name) {
    $out = Join-Path "$root\downloads" $name
    if (-not (Test-Path $out)) { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing }
    return $out
}

Write-Host "=== Visual Studio 2022 Build Tools"
$vs = Get-File 'https://aka.ms/vs/17/release/vs_buildtools.exe' 'vs_buildtools.exe'
$p = Start-Process -FilePath $vs -Wait -PassThru -ArgumentList @(
    '--quiet', '--wait', '--norestart', '--nocache',
    '--add', 'Microsoft.VisualStudio.Workload.VCTools',
    '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
    '--add', 'Microsoft.VisualStudio.Component.VC.ATL',
    '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.22621',
    '--includeRecommended')
Write-Host "vs_buildtools exit $($p.ExitCode)"   # 3010 = Neustart empfohlen, ist ok
if ($p.ExitCode -notin 0, 3010) { throw "Build Tools fehlgeschlagen ($($p.ExitCode))" }

Write-Host "=== LLVM 15.0.6"
if (-not (Test-Path 'C:\Program Files\LLVM\bin\libclang.dll')) {
    $llvm = Get-File 'https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.6/LLVM-15.0.6-win64.exe' 'LLVM-15.0.6-win64.exe'
    Start-Process -FilePath $llvm -Wait -ArgumentList '/S', '/D=C:\Program Files\LLVM'
}
& 'C:\Program Files\LLVM\bin\clang.exe' --version | Select-Object -First 1

Write-Host "=== ImageMagick"
if (-not (Get-ChildItem 'C:\Program Files' -Directory -Filter 'ImageMagick*' -ErrorAction SilentlyContinue)) {
    $im = Get-File 'https://github.com/ImageMagick/ImageMagick/releases/download/7.1.2-35/ImageMagick-7.1.2-35-Q16-x64-static.exe' 'ImageMagick-static.exe'
    Start-Process -FilePath $im -Wait -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-'
}
Get-ChildItem 'C:\Program Files' -Directory -Filter 'ImageMagick*' | Select-Object -ExpandProperty FullName

Write-Host "=== Fertig"
Stop-Transcript | Out-Null
