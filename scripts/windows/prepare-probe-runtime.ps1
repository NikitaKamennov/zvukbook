param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('cpu', 'cuda')]
    [string]$Device,
    [Parameter(Mandatory = $true)]
    [string]$Python
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'This preparation script requires Windows x64.'
}
if (-not [IO.Path]::IsPathRooted($Python)) { throw 'Pass an absolute path to CPython 3.12.10 x64.' }
$probeBasePython = (Resolve-Path -LiteralPath $Python).Path
$probeIdentity = & $probeBasePython -I -c "import sys,struct; print(str(sys.version_info[:3]) + ';' + str(struct.calcsize('P')*8) + ';' + sys.platform)"
if ($LASTEXITCODE -ne 0 -or $probeIdentity -ne '(3, 12, 10);64;win32') {
    throw 'Expected CPython 3.12.10 x64 for Windows. No environment was created.'
}
$probeProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$probeBase = Join-Path $probeProject '.local-data\windows-x64\probe'
$probeRuntime = Join-Path $probeBase ('runtime-' + $Device)
if (Test-Path -LiteralPath $probeRuntime) {
    throw ('Runtime already exists; it will not be overwritten: ' + $probeRuntime)
}
$probeDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($probeBase))
if ($probeDrive.AvailableFreeSpace -lt 15GB) { throw 'Keep at least 15 GiB free before installing the probe runtime.' }
New-Item -ItemType Directory -Path $probeBase -Force | Out-Null
& $probeBasePython -I -m venv $probeRuntime
if ($LASTEXITCODE -ne 0) { throw 'venv preparation failed; nothing was retried.' }
$probePython = Join-Path $probeRuntime 'Scripts\python.exe'
$probeLock = Join-Path $probeProject ('requirements\windows-probe\' + $Device + '.lock')
& $probePython -I -m pip --isolated install --no-input --no-cache-dir --only-binary=:all: --require-hashes --index-url https://pypi.org/simple -r $probeLock
if ($LASTEXITCODE -ne 0) { throw 'Pinned wheel installation failed; the partial runtime was left for inspection. No retries.' }
Write-Output ('Prepared separate ' + $Device + ' probe runtime: ' + $probeRuntime)
Write-Output 'No model, server, OCR or synthesis was started.'
