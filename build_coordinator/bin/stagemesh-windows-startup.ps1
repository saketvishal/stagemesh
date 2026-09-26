# Install/remove an unattended Windows Task Scheduler entry for `stagemesh continue`.
#
# The task runs at the current user's logon with limited privileges. It stores
# only local paths and StageMesh command arguments; credentials must come from
# the user's normal environment, credential manager, or project configuration.

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

    $result = & schtasks.exe /Query /TN $Name /FO LIST 2>&1
    return $LASTEXITCODE -eq 0
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
        & schtasks.exe /Delete /TN $EffectiveTaskName /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "failed to delete scheduled task '$EffectiveTaskName'"
        }
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
    "powershell.exe",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $StageMeshLauncher
) + $continueArgs
$taskRun = ($taskRunParts | ForEach-Object { Quote-TaskArgument $_ }) -join " "

& schtasks.exe /Create /F /TN $EffectiveTaskName /TR $taskRun /SC ONLOGON /RL LIMITED | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "failed to create/update scheduled task '$EffectiveTaskName'"
}

[pscustomobject]@{
    task_name = $EffectiveTaskName
    installed = $true
    trigger = "ONLOGON"
    project_dir = $ResolvedProjectDir
    command = $taskRun
} | ConvertTo-Json
