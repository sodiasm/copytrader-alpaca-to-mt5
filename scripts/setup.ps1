[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$SystemPython = (Get-Command python -ErrorAction Stop).Source

& $SystemPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.12 64-bit is required.'
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $SystemPython -m venv (Join-Path $ProjectRoot '.venv')
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.lock')
& $VenvPython -m pip install --no-deps -e $ProjectRoot

$EnvFile = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot '.env.example') -Destination $EnvFile
}

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $EnvFile /inheritance:r /grant:r "${Identity}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Could not restrict the .env ACL.'
}

Write-Output "installed project=$ProjectRoot"
Write-Output "python=$VenvPython"
Write-Output "next: edit $EnvFile, confirm config.toml mapping, then run:"
Write-Output "  & '$VenvPython' -m copytrader --config '$ProjectRoot\config.toml' preflight"
