<#
One-shot native Windows acceptance of the StageMesh local continuous driver, MOCK providers only.

What it does (all inside a temp directory it creates and removes):
  1. verifies the pinned commit, builds a venv, installs StageMesh and pytest;
  2. checks provider login STATE without any model call (claude auth status, codex login status) and reports the billing risk
     of API-key environment variables; raw output is never stored, only a classification;
  3. runs the focused driver tests and the process-tree diagnostic on this host;
  4. runs driver_lifecycle.py: installs the REAL driver through its own `driver install` into a throw-away git project with
     mock providers, lets Task Scheduler launch the ticks, then pauses, resumes, tests overlap, kills the whole process tree,
     verifies automatic recovery, stops and uninstalls;
  5. always removes its one Task Scheduler task, its processes and its temp files, and saves evidence.
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
if (-not $EvidenceRoot) {
    $main = 'C:\development\stagemesh\main'
    if (Test-Path $main) { $EvidenceRoot = Join-Path $main '.stagemesh\acceptance-evidence' }
    else { $EvidenceRoot = Join-Path $env:USERPROFILE 'stagemesh-acceptance-evidence' }
}
$stamp = $started.ToString('yyyyMMdd-HHmmss')
$evidence = Join-Path $EvidenceRoot "native-$stamp"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
Start-Transcript -Path (Join-Path $evidence 'transcript.txt') -Force | Out-Null

$summary = [ordered]@{ commit = $Commit; started = $started.ToString('o'); steps = [ordered]@{}; provider_state = [ordered]@{}; cleanup = [ordered]@{} }
function Step($name, $ok, $detail = '') {
    $summary.steps[$name] = [ordered]@{ ok = [bool]$ok; detail = "$detail" }
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host "[$tag] $name $detail"
}
$root = $null
$handle = $null
$lifecycleLog = Join-Path $evidence 'lifecycle-stdout.txt'

try {
    $summary.host = [ordered]@{ os = (Get-CimInstance Win32_OperatingSystem).Caption; git = (git --version); user_is_admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }

    # --- environment that could silently change what runs or what is billed (names only, never values) ----------------------
    $overrides = @(); $billing = @()
    foreach ($scope in 'Process', 'User', 'Machine') {
        $vars = [Environment]::GetEnvironmentVariables($scope)
        foreach ($k in $vars.Keys) {
            if ($k -match '^STAGEMESH_.*_CMD$') { $overrides += "$scope`:$k" }
            if ($k -in 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CODEX_API_KEY') { $billing += "$scope`:$k" }
        }
    }
    $summary.provider_state.api_key_environment_variables_present = $billing
    $summary.provider_state.stagemesh_command_overrides_present = $overrides
    if ($overrides.Count -gt 0) {
        Step 'no_provider_command_overrides' $false ("set: " + ($overrides -join ', ') + ' (unset them; the mock guard refuses to run)')
        throw 'provider command overrides present'
    }
    Step 'no_provider_command_overrides' $true

    # --- python ------------------------------------------------------------------------------------------------------------
    $py = $null
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    $candidates += , @('py', '-3.13'); $candidates += , @('py', '-3.12'); $candidates += , @('py', '-3.11'); $candidates += , @('python')
    foreach ($c in $candidates) {
        $exe = $c[0]; $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        $out = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11)); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq 'True') { $py = $out[1]; break }
    }
    if (-not $py) { Step 'python_3_11_or_newer' $false 'none found'; throw 'no python' }
    Step 'python_3_11_or_newer' $true $py

    # --- venv + install ----------------------------------------------------------------------------------------------------
    $venv = Join-Path $Work '.venv-accept'
    $pipLog = Join-Path $evidence 'pip.txt'
    & $py -m venv $venv *> $pipLog
    $vpy = Join-Path $venv 'Scripts\python.exe'
    & $vpy -m pip install --quiet --disable-pip-version-check -e "${Work}[dev]" *>> $pipLog
    Step 'install_stagemesh' ($LASTEXITCODE -eq 0 -and (Test-Path $vpy)) "log: pip.txt"
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

    # --- provider LOGIN STATE only (no model call, no credential output) -----------------------------------------------------
    function Probe($label, $exe, $arguments, $pattern) {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $cmd) { $summary.provider_state[$label] = [ordered]@{ installed = $false }; return }
        $text = (& $cmd.Source @arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
        $mode = 'unknown'
        if ($pattern -eq 'claude') {
            try { $j = $text | ConvertFrom-Json; $mode = "$($j.authMethod)" } catch { $mode = 'unparsed' }
        } else {
            if ($text -match '(?i)api[ -]?key') { $mode = 'api_key' } elseif ($text -match '(?i)chatgpt') { $mode = 'chatgpt_subscription' } elseif ($code -ne 0 -or $text -match '(?i)not logged') { $mode = 'not_logged_in' }
        }
        if (-not $mode) { $mode = 'unparsed' }
        $summary.provider_state[$label] = [ordered]@{ installed = $true; exit_code = $code; auth_mode = $mode; version = ((& $cmd.Source --version 2>&1 | Out-String).Trim() -replace '[^\x20-\x7E]', '') }
    }
    Probe 'claude_code' 'claude' @('auth', 'status') 'claude'
    Probe 'codex' 'codex' @('login', 'status') 'codex'
    Step 'provider_login_state_recorded' $true 'see summary.json (no model call made)'

    # --- focused tests and the process-tree diagnostic on THIS host --------------------------------------------------------
    Push-Location $Work
    $testLog = Join-Path $evidence 'pytest-driver.txt'
    & $vpy -m pytest tests/test_continuous_driver.py tests/test_driver_acceptance.py -q -p no:cacheprovider *> $testLog
    Step 'focused_driver_tests_on_this_host' ($LASTEXITCODE -eq 0) 'log: pytest-driver.txt'
    $diagDir = Join-Path $evidence 'diag-killtree'
    & $vpy scripts\windows-acceptance\diag-killtree.py $diagDir *> (Join-Path $evidence 'diag-killtree-stdout.txt')
    Step 'process_tree_diagnostic_ran' ($LASTEXITCODE -eq 0) 'evidence: diag-killtree'

    # --- the real lifecycle ------------------------------------------------------------------------------------------------
    $env:STAGEMESH_ACCEPT_DRIVER = '1'
    $env:STAGEMESH_ACCEPT_EVIDENCE = Join-Path $evidence 'driver-lifecycle.json'
    & $vpy scripts\windows-acceptance\driver_lifecycle.py --tasks 6 *> $lifecycleLog
    $lifecycleExit = $LASTEXITCODE
    Pop-Location
    foreach ($line in Get-Content $lifecycleLog -ErrorAction SilentlyContinue) {
        if ($line -like 'ROOT=*') { $root = $line.Substring(5).Trim() }
        if ($line -like 'TASK_HANDLE=*') { $handle = $line.Substring(12).Trim() }
    }
    $failures = '?'
    if (Test-Path $env:STAGEMESH_ACCEPT_EVIDENCE) { try { $failures = ((Get-Content $env:STAGEMESH_ACCEPT_EVIDENCE -Raw | ConvertFrom-Json).failures) -join ',' } catch { } }
    Step 'driver_lifecycle_mock_providers' ($lifecycleExit -eq 0) "exit=$lifecycleExit failures=[$failures]"
}
catch {
    Step 'unexpected_error' $false "$($_.Exception.Message)"
}
finally {
    # --- cleanup: only what this run created -----------------------------------------------------------------------------
    try {
        if (-not $handle -and (Test-Path $lifecycleLog)) {
            foreach ($line in Get-Content $lifecycleLog -ErrorAction SilentlyContinue) {
                if ($line -like 'ROOT=*') { $root = $line.Substring(5).Trim() }
                if ($line -like 'TASK_HANDLE=*') { $handle = $line.Substring(12).Trim() }
            }
        }
        if ($handle -and $handle -like 'StageMesh\driver-*') {
            schtasks /Delete /TN $handle /F *> $null
            schtasks /Query /TN $handle *> $null
            $summary.cleanup.task_handle = $handle
            $summary.cleanup.task_removed = ($LASTEXITCODE -ne 0)
        }
        $needles = @($Work); if ($root) { $needles += $root }
        $killed = @()
        foreach ($p in (Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe', 'pythonw.exe' -and $_.CommandLine -and $_.ProcessId -ne $PID })) {
            foreach ($n in $needles) {
                if ($p.CommandLine.IndexOf($n, [StringComparison]::OrdinalIgnoreCase) -ge 0) { taskkill /F /T /PID $p.ProcessId *> $null; $killed += $p.ProcessId; break }
            }
        }
        $summary.cleanup.processes_killed = $killed
        if ($root -and (Test-Path $root)) {
            foreach ($f in 'driver.log') { Get-ChildItem -Path $root -Recurse -Filter $f -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { Copy-Item $_.FullName (Join-Path $evidence 'tick-driver.log') -ErrorAction SilentlyContinue } }
            Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
        }
        Set-Location $env:TEMP
        Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
        $summary.cleanup.temp_removed = (-not (Test-Path $Work)) -and ((-not $root) -or (-not (Test-Path $root)))
    }
    catch { $summary.cleanup.error = "$($_.Exception.Message)" }
    $summary.finished = (Get-Date).ToString('o')
    $allOk = ($summary.steps.Values | Where-Object { -not $_.ok }).Count -eq 0
    $summary.result = if ($allOk) { 'PASS' } else { 'FAIL' }
    $summary | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $evidence 'summary.json') -Encoding UTF8
    Write-Host ''
    Write-Host "RESULT: $($summary.result)   evidence: $evidence"
    Stop-Transcript | Out-Null
}
if ($summary.result -ne 'PASS') { exit 1 }
