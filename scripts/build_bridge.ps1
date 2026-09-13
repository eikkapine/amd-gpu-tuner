[CmdletBinding()]
param([string]$Configuration = 'Release')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$sdkPath = Join-Path $repoRoot 'third_party\ADLX'
$sdkRevision = 'd9f04a9bba022d6cf6333f005dd540b4ad19fb63'
if (-not (Test-Path -LiteralPath (Join-Path $sdkPath 'SDK\ADLXHelper\Windows\Cpp\ADLXHelper.h'))) {
    if (Test-Path -LiteralPath $sdkPath) { throw "Incomplete SDK at $sdkPath. Set up the ADLX SDK before building." }
    git clone --no-checkout https://github.com/GPUOpen-LibrariesAndSDKs/ADLX $sdkPath
    if ($LASTEXITCODE -ne 0) { throw 'ADLX download failed.' }
    git -C $sdkPath checkout --detach $sdkRevision
    if ($LASTEXITCODE -ne 0) { throw 'ADLX checkout failed.' }
}
# --fresh also handles a checkout moved or renamed since its last build.
cmake --fresh -S (Join-Path $repoRoot 'bridge') -B (Join-Path $repoRoot 'bridge\build') -A x64
if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed.' }
cmake --build (Join-Path $repoRoot 'bridge\build') --config $Configuration --parallel
if ($LASTEXITCODE -ne 0) { throw 'Native bridge build failed.' }
Write-Output (Join-Path $repoRoot "bridge\build\$Configuration\voltshift_bridge.exe")
