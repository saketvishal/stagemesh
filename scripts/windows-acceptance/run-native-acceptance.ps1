<#
One-shot native Windows acceptance of the StageMesh local continuous driver, MOCK providers only.

What it does (all inside a temp directory it creates and removes):
  1. builds a venv from the pinned checkout and installs StageMesh and pytest;
  2. records provider login STATE with `stagemesh provider-auth` (no model call, only a classification is stored);
  3. runs the focused driver tests and the process-tree diagnostic on this host;
  4. runs driver_lifecycle.py: installs the REAL driver through its own `driver install` into a throw-away git project with
     mock providers, lets Task Scheduler launch the ticks, then pauses, resumes, tests overlap, kills the whole process tree,
     verifies automatic recovery, stops and uninstalls;
  5. always removes its one Task Scheduler task, its processes and its temp files, and saves evidence.
Every external command has a time limit. PASS requires the lifecycle evidence file with zero failures AND a clean cleanup.
It never touches other scheduled tasks, other projects, credentials or any persistent setting. No real provider is invoked.
#>
param(
    [Parameter(Mandatory = $true)][string]$Work,
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$EvidenceRoot = '',
    [string]$Python = ''
)
$ErrorActionPreference = 'Continue'
$started = Get-Date
$tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$Work = [IO.Path]::GetFullPath($Work).TrimEnd('\')
$workLeaf = Split-Path $Work -Leaf
if (($Work -notlike "$tempRoot\*") -or ($workLeaf -notmatch '^sm-native-[0-9a-f]{8}$') -or ((Split-Path $Work -Parent) -ne $tempRoot)) {
    Write-Host "REFUSED: -Work must be a directory named sm-native-<8 hex> directly under $tempRoot (got $Work)"
    exit 2
}
if ($Commit -notmatch '^[0-9a-f]{40}$') { Write-Host 'REFUSED: -Commit must be a full 40-character sha'; exit 2 }
if (-not $EvidenceRoot) {
    $main = 'C:\development\stagemesh\main'
    if (Test-Path $main) { $EvidenceRoot = Join-Path $main '.stagemesh\acceptance-evidence' }
    else { $EvidenceRoot = Join-Path $env:USERPROFILE 'stagemesh-acceptance-evidence' }
}
$stamp = $started.ToString('yyyyMMdd-HHmmss')
$evidence = Join-Path $EvidenceRoot "native-$stamp"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
Start-Transcript -Path (Join-Path $evidence 'transcript.txt') -Force | Out-Null

$summary = [ordered]@{ commit = $Commit; started = $started.ToString('o'); steps = [ordered]@{}; provider_state = $null; cleanup = [ordered]@{} }
function Step($name, $ok, $detail = '') {
    $summary.steps[$name] = [ordered]@{ ok = [bool]$ok; detail = "$detail" }
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host "[$tag] $name $detail"
}
function Quote($s) { '"' + ($s -replace '"', '\"') + '"' }
# Runs a program with a hard time limit; the whole process tree is killed on timeout. Returns the exit code (-1 on timeout).
function Invoke-Bounded($label, $exe, $argList, $timeoutSeconds, $cwd) {
    $out = Join-Path $evidence "$label.out.txt"
    $err = Join-Path $evidence "$label.err.txt"
    $p = Start-Process -FilePath $exe -ArgumentList ($argList | ForEach-Object { Quote $_ }) -WorkingDirectory $cwd `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -NoNewWindow
    $null = $p.Handle
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while (-not $p.WaitForExit(120000)) {
        if ((Get-Date) -gt $deadline) {
            taskkill /F /T /PID $p.Id *> $null
            Write-Host "  $label timed out after $timeoutSeconds s and was killed"
            return -1
        }
        Write-Host ("  {0} still running ({1:N0} s)" -f $label, ((Get-Date) - $started).TotalSeconds)
    }
    $p.WaitForExit()
    return [int]$p.ExitCode
}
function Read-Ids($file) {
    $ids = @{ root = $null; handle = $null }
    if (Test-Path $file) {
        foreach ($line in Get-Content $file -ErrorAction SilentlyContinue) {
            if ($line -like 'ROOT=*') { $ids.root = $line.Substring(5).Trim() }
            if ($line -like 'TASK_HANDLE=*') { $ids.handle = $line.Substring(12).Trim() }
        }
    }
    return $ids
}

$idsFile = Join-Path $evidence 'ids.txt'
$lifecycleEvidence = Join-Path $evidence 'driver-lifecycle.json'
try {
    $summary.host = [ordered]@{ os = (Get-CimInstance Win32_OperatingSystem).Caption; git = (git --version); powershell = $PSVersionTable.PSVersion.ToString() }

    $overrides = @()
    foreach ($scope in 'Process', 'User', 'Machine') {
        $vars = [Environment]::GetEnvironmentVariables($scope)
        foreach ($k in $vars.Keys) { if ($k -match '^STAGEMESH_.*_CMD$') { $overrides += "${scope}:$k" } }
    }
    if ($overrides.Count -gt 0) {
        Step 'no_provider_command_overrides' $false ("set: " + ($overrides -join ', ') + ' (unset them; the mock guard refuses to run)')
        throw 'provider command overrides present'
    }
    Step 'no_provider_command_overrides' $true

    $head = (git -C $Work rev-parse HEAD)
    Step 'checkout_is_pinned_commit' ($head -eq $Commit) $head
    if ($head -ne $Commit) { throw 'checkout is not the pinned commit' }

    $py = $null
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    $candidates += , @('py', '-3.13'); $candidates += , @('py', '-3.12'); $candidates += , @('py', '-3.11'); $candidates += , @('python')
    foreach ($c in $candidates) {
        $exe = $c[0]; $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        $found = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11)); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found -and $found[0] -eq 'True') { $py = $found[1]; break }
    }
    if (-not $py) { Step 'python_3_11_or_newer' $false 'none found'; throw 'no python' }
    Step 'python_3_11_or_newer' $true $py

    $venv = Join-Path $Work '.venv-accept'
    $vpy = Join-Path $venv 'Scripts\python.exe'
    $code = Invoke-Bounded 'venv' $py @('-m', 'venv', $venv) 300 $Work
    if ($code -eq 0) { $code = Invoke-Bounded 'pip' $vpy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-e', "${Work}[dev]") 900 $Work }
    Step 'install_stagemesh' (($code -eq 0) -and (Test-Path $vpy)) 'logs: venv/pip'
    if ($code -ne 0) { throw 'install failed' }

    # Provider login STATE only (no model call). Exit 1 just means "not ready for real providers", which is information here.
    $authCode = Invoke-Bounded 'provider-auth' $vpy @('-m', 'stagemesh.cli', 'provider-auth', '--json') 120 $Work
    try { $summary.provider_state = (Get-Content (Join-Path $evidence 'provider-auth.out.txt') -Raw | ConvertFrom-Json) } catch { $summary.provider_state = "unreadable (exit $authCode)" }
    Step 'provider_login_state_recorded' ($authCode -ne -1) 'classification only; no model call made'

    $code = Invoke-Bounded 'pytest-driver' $vpy @('-m', 'pytest', 'tests/test_continuous_driver.py', 'tests/test_driver_acceptance.py', '-q', '-p', 'no:cacheprovider') 600 $Work
    Step 'focused_driver_tests_on_this_host' ($code -eq 0) "exit=$code"
    $code = Invoke-Bounded 'diag-killtree' $vpy @('scripts\windows-acceptance\diag-killtree.py', (Join-Path $evidence 'diag-killtree')) 300 $Work
    Step 'process_tree_diagnostic_ran' ($code -eq 0) "exit=$code"

    $env:STAGEMESH_ACCEPT_DRIVER = '1'
    $env:STAGEMESH_ACCEPT_EVIDENCE = $lifecycleEvidence
    $env:STAGEMESH_ACCEPT_IDS = $idsFile
    Write-Host 'Running the real Task Scheduler lifecycle (about 15 to 20 minutes). Heartbeats appear every 2 minutes.'
    $code = Invoke-Bounded 'lifecycle' $vpy @('scripts\windows-acceptance\driver_lifecycle.py', '--tasks', '6') 2700 $Work
    $failures = @('evidence file missing')
    if (Test-Path $lifecycleEvidence) { try { $failures = @((Get-Content $lifecycleEvidence -Raw | ConvertFrom-Json).failures) } catch { $failures = @('evidence unreadable') } }
    $ids = Read-Ids $idsFile
    $ok = ($code -eq 0) -and ($failures.Count -eq 0) -and $ids.root -and $ids.handle
    Step 'driver_lifecycle_mock_providers' $ok ("exit=$code failures=[" + ($failures -join ',') + "] ids_recorded=" + [bool]($ids.root -and $ids.handle))
}
catch {
    Step 'unexpected_error' $false "$($_.Exception.Message)"
}
finally {
    try {
        Set-Location $env:TEMP
        $ids = Read-Ids $idsFile
        $root = $ids.root; $handle = $ids.handle
        $taskOk = $true
        if ($handle) {
            if ($handle -match '^StageMesh\\driver-[0-9a-zA-Z_-]+$') {
                schtasks /Delete /TN $handle /F *> $null
                schtasks /Query /TN $handle *> $null
                $taskOk = ($LASTEXITCODE -ne 0)
                $summary.cleanup.task_handle = $handle
            } else { $taskOk = $false; $summary.cleanup.task_handle_rejected = $handle }
        }
        $summary.cleanup.task_removed = $taskOk
        $rootOk = $true
        $needles = @($Work)
        if ($root) {
            $rootLeaf = Split-Path $root -Leaf
            $rootParentLeaf = Split-Path (Split-Path $root -Parent) -Leaf
            if (($rootLeaf -match '^sm-driver-accept-[A-Za-z0-9_]+$') -and ($rootParentLeaf -ieq (Split-Path $tempRoot -Leaf))) { $needles += $root }
            else { $rootOk = $false; $summary.cleanup.root_rejected = $root }
        }
        $killed = @()
        foreach ($p in (Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe', 'pythonw.exe' -and $_.CommandLine -and $_.ProcessId -ne $PID })) {
            foreach ($n in $needles) {
                if ($p.CommandLine.IndexOf($n, [StringComparison]::OrdinalIgnoreCase) -ge 0) { taskkill /F /T /PID $p.ProcessId *> $null; $killed += $p.ProcessId; break }
            }
        }
        $summary.cleanup.processes_killed = $killed
        Start-Sleep -Seconds 2
        if ($rootOk -and $root -and (Test-Path $root)) {
            Get-ChildItem -Path $root -Recurse -Filter 'driver.log' -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { Copy-Item $_.FullName (Join-Path $evidence 'tick-driver.log') -ErrorAction SilentlyContinue }
            Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
        }
        Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
        $tempOk = (-not (Test-Path $Work)) -and ((-not $root) -or (-not (Test-Path $root)))
        $summary.cleanup.temp_removed = $tempOk
        Step 'cleanup_complete' ($taskOk -and $rootOk -and $tempOk) ("task_removed=$taskOk temp_removed=$tempOk")
    }
    catch { $summary.cleanup.error = "$($_.Exception.Message)"; Step 'cleanup_complete' $false $summary.cleanup.error }
    $summary.finished = (Get-Date).ToString('o')
    $allOk = (@($summary.steps.Values | Where-Object { -not $_.ok }).Count -eq 0) -and ($summary.steps.Count -ge 8)
    $summary.result = if ($allOk) { 'PASS' } else { 'FAIL' }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $evidence 'summary.json') -Encoding UTF8
    Write-Host ''
    Write-Host "RESULT: $($summary.result)   evidence: $evidence"
    Stop-Transcript | Out-Null
}
if ($summary.result -ne 'PASS') { exit 1 }
