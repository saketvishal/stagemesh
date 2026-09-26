# Install/remove an unattended Windows Task Scheduler entry for `stagemesh continue`.
#
# The task runs at system startup as the current user with S4U logon semantics.
# It stores only local paths and StageMesh command arguments; credentials must
# come from machine-accessible credential stores or project configuration.

[CmdletBinding()]
param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "install",

    [string]$ProjectDir = (Get-Location).Path,
    [string]$ProjectName,
    [string]$TaskName,
    [switch]$All,
    [switch]$Github,
    [int]$MaxCycles = 2000
)

$ErrorActionPreference = "Stop"

function ConvertTo-StableTaskName {
    param([string]$Seed)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Seed.Trim().TrimEnd("\", "/").ToLowerInvariant())
        $hash = $sha.ComputeHash($bytes)
        $hex = -join ($hash[0..7] | ForEach-Object { $_.ToString("x2") })
        return "StageMesh-Continue-$hex"
    } finally {
        $sha.Dispose()
    }
}

function Quote-TaskArgument {
    param([string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Test-TaskInstalled {
    param([string]$Name)

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    return $null -ne $task
}

function Get-CurrentUserId {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StageMeshLauncher = Join-Path $ScriptDir "stagemesh.ps1"
$ResolvedProjectDir = (Resolve-Path $ProjectDir).Path
$TaskSeed = if ($All) { "all|$ResolvedProjectDir" } elseif ($ProjectName) { "$ResolvedProjectDir|$ProjectName" } else { $ResolvedProjectDir }
$EffectiveTaskName = if ($TaskName) { $TaskName } else { ConvertTo-StableTaskName $TaskSeed }

if ($Action -eq "status") {
    $installed = Test-TaskInstalled $EffectiveTaskName
    [pscustomobject]@{
        task_name = $EffectiveTaskName
        installed = $installed
        project_dir = $ResolvedProjectDir
    } | ConvertTo-Json
    exit 0
}

if ($Action -eq "uninstall") {
    if (Test-TaskInstalled $EffectiveTaskName) {
        Unregister-ScheduledTask -TaskName $EffectiveTaskName -Confirm:$false
    }
    [pscustomobject]@{
        task_name = $EffectiveTaskName
        installed = $false
        project_dir = $ResolvedProjectDir
    } | ConvertTo-Json
    exit 0
}

$continueArgs = @("continue", "--project-dir", $ResolvedProjectDir, "--max-cycles", "$MaxCycles")
if ($ProjectName) {
    $continueArgs += $ProjectName
}
if ($All) {
    $continueArgs += "--all"
}
if ($Github) {
    $continueArgs += "--github"
}

$taskRunParts = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $StageMeshLauncher
) + $continueArgs
$taskRun = ($taskRunParts | ForEach-Object { Quote-TaskArgument $_ }) -join " "

$trigger = New-ScheduledTaskTrigger -AtStartup
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskRun -WorkingDirectory $ResolvedProjectDir
$principal = New-ScheduledTaskPrincipal -UserId (Get-CurrentUserId) -LogonType S4U -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 7)

Register-ScheduledTask `
    -TaskName $EffectiveTaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

[pscustomobject]@{
    task_name = $EffectiveTaskName
    installed = $true
    trigger = "AtStartup"
    logon_type = "S4U"
    project_dir = $ResolvedProjectDir
    command = "powershell.exe $taskRun"
} | ConvertTo-Json
