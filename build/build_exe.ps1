[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    & (Join-Path $repoRoot 'scripts\build_bridge.ps1')
    py -3.12 -m pip install -e '.[build]'
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    py -3.12 -m PyInstaller --noconfirm --clean --distpath build\dist --workpath build\work build\VoltShift.spec
    if ($LASTEXITCODE -ne 0) { throw 'Application packaging failed.' }
    $application = Join-Path $repoRoot 'build\dist\AMD-GPU-Tuner\AMD-GPU-Tuner.exe'
    if (-not (Test-Path -LiteralPath $application)) { throw 'Packaged executable missing.' }
    Write-Output $application
} finally {
    Pop-Location
}
