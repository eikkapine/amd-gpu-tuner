<#
.SYNOPSIS
    Downloads Intel PresentMon into third_party/presentmon/.

.DESCRIPTION
    PresentMon (github.com/GameTechDev/PresentMon, MIT licence) is what gives
    VoltShift frame-rate, 1% lows and stutter data. Without it VoltShift can
    still tune on power, clocks and temperature, but it cannot tell whether a
    change helped what you actually see.

    It is fetched rather than vendored so the binary in your install is one
    you pulled from Intel's own release page, and so this repository does not
    redistribute a third-party executable.

    Requires administrator rights at runtime (PresentMon uses ETW tracing),
    which VoltShift already needs for tuning writes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\fetch_presentmon.ps1
#>

[CmdletBinding()]
param(
    [string]$Version = "latest",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$targetDir = Join-Path $repoRoot "third_party\presentmon"
$targetExe = Join-Path $targetDir "PresentMon.exe"

if ((Test-Path $targetExe) -and -not $Force) {
    Write-Host "PresentMon already present at $targetExe" -ForegroundColor Green
    Write-Host "Re-run with -Force to replace it."
    exit 0
}

Write-Host "Querying the PresentMon release feed..." -ForegroundColor Cyan

$api = if ($Version -eq "latest") {
    "https://api.github.com/repos/GameTechDev/PresentMon/releases/latest"
} else {
    "https://api.github.com/repos/GameTechDev/PresentMon/releases/tags/$Version"
}

$headers = @{ "User-Agent" = "VoltShift-fetch-presentmon" }
$release = Invoke-RestMethod -Uri $api -Headers $headers

# Prefer the standalone console executable over the full installer: VoltShift
# drives the CLI directly and does not want a service registered on the machine.
$asset = $release.assets |
    Where-Object { $_.name -match '^PresentMon.*\.exe$' -and $_.name -notmatch 'Setup|Installer|Service' } |
    Select-Object -First 1

if (-not $asset) {
    Write-Host "No standalone PresentMon .exe in release '$($release.tag_name)'." -ForegroundColor Red
    Write-Host "Assets offered:" -ForegroundColor Yellow
    $release.assets | ForEach-Object { Write-Host "  $($_.name)" }
    Write-Host ""
    Write-Host "Download a console build manually and save it as:" -ForegroundColor Yellow
    Write-Host "  $targetExe"
    exit 1
}

New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

$sizeMb = [math]::Round($asset.size / 1MB, 2)
Write-Host "Release : $($release.tag_name)"
Write-Host "Asset   : $($asset.name)  ($sizeMb MB)"
Write-Host "Source  : $($asset.browser_download_url)"
Write-Host "Target  : $targetExe"
Write-Host ""

Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $targetExe -Headers $headers

$hash = (Get-FileHash -Path $targetExe -Algorithm SHA256).Hash
Write-Host "Downloaded. SHA256 $hash" -ForegroundColor Green

# Record what was fetched so an install can be audited later.
@{
    tag     = $release.tag_name
    asset   = $asset.name
    url     = $asset.browser_download_url
    sha256  = $hash
    fetched = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -Path (Join-Path $targetDir "source.json") -Encoding utf8

Write-Host ""
Write-Host "AMD GPU Tuner will pick this up automatically on next launch." -ForegroundColor Green
Write-Host "Verify with:  voltshift metrics" -ForegroundColor Cyan
