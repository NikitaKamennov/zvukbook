param([string]$Python = 'C:\Python312\python.exe', [switch]$Release, [switch]$Portable,
      [ValidateSet('full', 'cpu')][string]$Edition = 'full')
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'Build on 64-bit Windows; this script cannot assemble a Windows bundle on Linux.'
}
if ($Release -and $Portable) { throw 'Choose -Release or -Portable, not both.' }
$Edition = $Edition.ToLowerInvariant()
$buildProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildWindows = Join-Path $buildProject '.local-data\windows-x64'
$buildDistribution = if ($Edition -eq 'cpu') { 'dist-cpu' } else { 'dist' }
$buildExe = Join-Path $buildWindows ($buildDistribution + '\Zvukbook\Zvukbook.exe')
$buildRunning = @(Get-Process -Name Zvukbook -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $buildExe })
if ($buildRunning.Count -gt 0) { throw 'Close Zvukbook before rebuilding its files.' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'CPython 3.12.10 x64 is required; pass its absolute path using -Python.' }
New-Item -ItemType Directory -Path $buildWindows -Force | Out-Null
try {
    $buildLock = [IO.File]::Open((Join-Path $buildWindows 'build.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch [IO.IOException] {
    throw 'Another Windows build is using the dependency cache. Wait for it to finish.'
}
$buildStagingName = if ($Edition -eq 'cpu') { 'Zvukbook-cpu-staging.exe' } else { 'Zvukbook-staging.exe' }
$buildStaging = Join-Path $buildWindows $buildStagingName
$buildPriorToolchain = $env:GOTOOLCHAIN
try {
    $env:GOTOOLCHAIN = 'local'
    & go -C (Join-Path $buildProject 'desktop') build -mod=readonly -tags desktop,production -trimpath -ldflags '-H=windowsgui -s -w' -o $buildStaging ./cmd/zvukbook
    if ($LASTEXITCODE -ne 0) { throw 'Go/Wails build failed; the previous EXE was not replaced.' }
    $bundleArgs = @('-I', (Join-Path $PSScriptRoot 'windows\package_bundle.py'), '--exe', $buildStaging, '--edition', $Edition)
    if ($Release) { $bundleArgs += '--include-release-tools' }
    & $Python @bundleArgs
    if ($LASTEXITCODE -ne 0) { throw 'Windows bundle preparation failed. Inspect the error above.' }
    if ($Release) {
        & $Python -I (Join-Path $PSScriptRoot 'windows\package_release.py') --edition $Edition
        if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed. Inspect the error above.' }
    }
    if ($Portable) {
        & $Python -I (Join-Path $PSScriptRoot 'windows\package_release.py') --edition $Edition --portable-only
        if ($LASTEXITCODE -ne 0) { throw 'Portable packaging failed. Inspect the error above.' }
    }
} finally {
    $env:GOTOOLCHAIN = $buildPriorToolchain
    $buildLock.Dispose()
}
Write-Output ('Ready: ' + $buildExe)
Write-Output 'Books and MP3 are stored separately in %LOCALAPPDATA%\Zvukbook.'
