[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = 'Alpaca-MT5-CopyTrader'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Config = Join-Path $ProjectRoot 'config.toml'

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Run scripts\setup.ps1 first.'
}

& $Python -m copytrader --config $Config preflight
if ($LASTEXITCODE -ne 0) {
    throw 'Preflight failed. The Scheduled Task was not registered.'
}

$Arguments = "-m copytrader --config `"$Config`" run"
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Delay = 'PT30S'
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register current-user copy-trader scheduled task')) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description 'Copies confirmed Alpaca paper fills to Darwinex MT5 Demo1.' `
        -Force | Out-Null
    Write-Output "registered task=$TaskName user=$UserId"
}
