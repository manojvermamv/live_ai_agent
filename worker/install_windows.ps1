# Install the Live AI Agent worker as a Windows scheduled task for the current user.
#
#   powershell -ExecutionPolicy Bypass -File worker\install_windows.ps1
#   powershell -ExecutionPolicy Bypass -File worker\install_windows.ps1 -Uninstall
#
# No administrator rights are needed. The task starts at logon and, as a
# watchdog, every 5 minutes: a second start while one is running is ignored by
# Task Scheduler and refused by the worker's own lock, so there is never more
# than one. It runs windowless (pythonw) and does not depend on any terminal or
# AI agent. Limitation: a task registered without a stored password runs only
# while this user is logged on (a locked screen is fine). To run while logged
# off, open Task Scheduler, edit "LiveAIAgentWorker" and choose "Run whether
# user is logged on or not" — Windows then asks for the password itself.
param([switch]$Uninstall, [string]$Python = "")

$Name = "LiveAIAgentWorker"
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*live_ai_worker.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    "removed $Name (state and logs in $env:LOCALAPPDATA\live_ai_agent are kept)"
    return
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $PSScriptRoot "live_ai_worker.py"
if (-not $Python) { $Python = (Get-Command pythonw -ErrorAction Stop).Source }
if (-not (Test-Path (Join-Path $Repo ".git"))) { throw "$Repo is not a git checkout" }

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`" run --repo `"$Repo`"" -WorkingDirectory $Repo
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Name -Action $action -Trigger @($logon, $watchdog) `
    -Settings $settings -Principal $principal -Force `
    -Description "Live AI Agent background worker (no AI): $Repo" | Out-Null
Start-ScheduledTask -TaskName $Name
"installed and started $Name for $env:USERNAME; state and logs: $env:LOCALAPPDATA\live_ai_agent"
