<#
.SYNOPSIS
  Install the Avora biometric connector as a self-healing Windows Scheduled Task.

.DESCRIPTION
  Registers "AvoraBiometricConnector" to run ONE short sync every few minutes,
  forever, whether or not anyone is logged on.

  Why a repeating one-shot and not a --loop daemon:

    * Task Scheduler's default ExecutionTimeLimit is 3 days. It silently kills a
      long-running --loop process, and an "At startup" trigger will not start it
      again until the next reboot. This is the usual reason attendance stops
      arriving days after a setup that "worked".
    * A one-shot that exits in seconds cannot be killed for running too long and
      cannot leak. If a run fails - SQL busy, network down, laptop asleep - the
      next tick simply retries. Nothing to babysit.
    * The connector keeps a watermark and the Avora server is idempotent, so
      repeating a run is always safe.

  Task settings chosen deliberately:
    AtStartup + AtLogOn + repeat every N minutes  - covers reboot, logon, and plain running
    S4U principal                                 - runs with no one logged on, no stored password
    StartWhenAvailable                            - catches up a tick missed while asleep/off
    AllowStartIfOnBatteries / DontStopIfGoing...  - a UPS blip does not stop attendance
    MultipleInstances IgnoreNew                   - ticks never overlap
    RestartCount 3 / RestartInterval 1m           - covers a crash on start
    ExecutionTimeLimit 1h                         - a wedged run is killed so the next can proceed

.PARAMETER IntervalMinutes
  How often to sync. Default 5.

.PARAMETER KeepAwake
  Also stop the PC sleeping on AC power. An asleep PC sends nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -KeepAwake
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 5,
    [switch]$KeepAwake,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AvoraBiometricConnector'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Connector = Join-Path $ScriptDir 'avora_biometric.py'

function Write-Step($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Write-Bad($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red }

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "Removed scheduled task '$TaskName'."
    } catch {
        Write-Warn "No task named '$TaskName' to remove."
    }
    exit 0
}

Write-Host ''
Write-Host 'Avora biometric connector - install' -ForegroundColor Cyan
Write-Host ''

# --- 1. Locate a real python.exe -------------------------------------------- #
# Never register the bare word "python": under a scheduled task the PATH differs
# from an interactive shell, and on many machines it resolves to the Windows Store
# stub, which exits immediately and does nothing. An absolute path is the only
# reliable form.
$Python = $null
foreach ($candidate in @('python.exe', 'python3.exe', 'py.exe')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        $resolved = $found.Source
        if ($resolved -notlike '*WindowsApps*') { $Python = $resolved; break }
    }
}
if (-not $Python) {
    foreach ($guess in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        'C:\Python312\python.exe', 'C:\Python311\python.exe', 'C:\Python310\python.exe'
    )) { if (Test-Path $guess) { $Python = $guess; break } }
}
if (-not $Python) {
    Write-Bad 'Could not find python.exe. Install Python 3.10+ from python.org'
    Write-Bad '(tick "Add python.exe to PATH"), then re-run this script.'
    exit 1
}
Write-Ok "Python: $Python"

# Stamp it where the .bat helpers can read it, so check-health / diagnose run on
# the SAME interpreter as the task. Without this they fall back to "py -3", and
# on a PC where that is the Store stub the person diagnosing a dead connector
# gets a blank window - the one moment the tooling must not fail silently.
Set-Content -Path (Join-Path $ScriptDir 'avora-python-path.txt') `
    -Value $Python -Encoding ASCII -NoNewline

if (-not (Test-Path $Connector)) {
    Write-Bad "avora_biometric.py not found next to this script ($ScriptDir)."
    exit 1
}
Write-Ok "Connector: $Connector"

# --- 2. Prerequisites, before promising anything works ----------------------- #
Write-Host ''
Write-Step 'Checking prerequisites...'
& $Python $Connector --selftest
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Bad 'Prerequisites failed (see above). Fix those first, then re-run.'
    Write-Bad 'A task installed over a broken setup just fails silently every 5 minutes.'
    exit 1
}

# --- 3. Register the task ---------------------------------------------------- #
Write-Host ''
Write-Step "Registering '$TaskName' (every $IntervalMinutes min)..."

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Connector`" --quiet" -WorkingDirectory $ScriptDir

$repeat = New-TimeSpan -Minutes $IntervalMinutes
$forever = New-TimeSpan -Days 3650

# The cadence lives on ONE -Once trigger with an indefinite repetition. Task
# Scheduler persists it across reboots, and StartWhenAvailable catches up a tick
# missed while the PC was off or asleep, so this alone keeps the sync running
# forever. It starts 30s from now so the first sync does not wait for a reboot.
#
# Deliberately NOT done: assigning .Repetition onto the AtStartup/AtLogOn
# triggers. That property is read-only on the CIM objects some Windows builds
# return, so it throws there, and sharing one Repetition object across two
# triggers does not always serialise. It would also be redundant. The agent's
# installer (agent/internal/autostart/autostart_windows.go) avoids it for the
# same reason; boot and logon are plain triggers that just kick things off.
$tBoot = New-ScheduledTaskTrigger -AtStartup
$tLogon = New-ScheduledTaskTrigger -AtLogOn
$tNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) `
    -RepetitionInterval $repeat -RepetitionDuration $forever

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3 `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$settings.DisallowStartOnRemoteAppSession = $false
$settings.RunOnlyIfNetworkAvailable = $false   # offline runs still read SQL and log
# An idle PC is exactly when this must keep running. IdleSettings can be absent
# on some builds, so never dereference it blindly - a null here would abort the
# install for a setting that is only a refinement.
if ($null -ne $settings.IdleSettings) {
    $settings.IdleSettings.StopOnIdleEnd = $false
    $settings.IdleSettings.RestartOnIdle = $false
}

# S4U: "run whether the user is logged on or not" WITHOUT storing a password.
# It keeps the user's own SID, so Trusted_Connection to the local SQL Server still
# authenticates as the account that actually has rights on SmartOfficedb. Running
# as SYSTEM would be simpler but is frequently NOT a SQL login on Smart Office
# installs, which fails every single run.
$me = "$env:USERDOMAIN\$env:USERNAME"
$registered = $false
foreach ($logonType in @('S4U', 'Password', 'Interactive')) {
    try {
        if ($logonType -eq 'Password') {
            Write-Warn "S4U refused. Enter the Windows password for $me to run unattended."
            $cred = Get-Credential -UserName $me -Message 'Password for the unattended task'
            Register-ScheduledTask -TaskName $TaskName -Action $action `
                -Trigger @($tBoot, $tLogon, $tNow) -Settings $settings `
                -User $cred.UserName `
                -Password $cred.GetNetworkCredential().Password `
                -RunLevel Highest -Force | Out-Null
        } else {
            $principal = New-ScheduledTaskPrincipal -UserId $me `
                -LogonType $logonType -RunLevel Highest
            Register-ScheduledTask -TaskName $TaskName -Action $action `
                -Trigger @($tBoot, $tLogon, $tNow) -Settings $settings `
                -Principal $principal -Force | Out-Null
        }
        Write-Ok "Registered with logon type: $logonType"
        if ($logonType -eq 'Interactive') {
            Write-Warn 'Interactive only: this runs when someone is LOGGED ON.'
            Write-Warn 'Leave the PC logged in (locking the screen is fine).'
        }
        $registered = $true
        break
    } catch {
        Write-Warn "$logonType failed: $($_.Exception.Message)"
    }
}
if (-not $registered) { Write-Bad 'Could not register the task.'; exit 1 }

# --- 4. Prove it exists and actually runs ------------------------------------ #
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Bad 'Task vanished after registration.'; exit 1 }

Write-Step 'Running one sync now to prove it works...'
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 20
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Step "Last run result: 0x$('{0:X}' -f $info.LastTaskResult)"

& $Python $Connector --status
$healthy = ($LASTEXITCODE -eq 0)

# --- 5. Keep the machine awake ----------------------------------------------- #
if ($KeepAwake) {
    Write-Host ''
    Write-Step 'Disabling sleep and hibernate on AC power...'
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change disk-timeout-ac 0
    Write-Ok 'This PC will stay awake on mains power.'
}

Write-Host ''
if ($healthy) {
    Write-Host '  Installed and syncing.' -ForegroundColor Green
} else {
    Write-Warn 'Installed, but the first sync did not report healthy. Check:'
    Write-Warn "  $ScriptDir\avora_biometric.log"
}
Write-Host ''
Write-Host '  Check health any time:' -ForegroundColor Cyan
Write-Host "    $Python `"$Connector`" --status"
Write-Host '  Diagnose a problem:' -ForegroundColor Cyan
Write-Host "    $Python `"$Connector`" --selftest"
Write-Host '  Remove:' -ForegroundColor Cyan
Write-Host "    powershell -ExecutionPolicy Bypass -File `"$ScriptDir\install-windows.ps1`" -Uninstall"
Write-Host ''
if (-not $KeepAwake) {
    Write-Warn 'If this PC sleeps it sends nothing. Re-run with -KeepAwake to prevent that.'
    Write-Host ''
}
