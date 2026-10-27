<#
Reviewed, pinned upgrade of the ALREADY INSTALLED hands-off sandbox. It never deletes or overwrites the ledger, the sandbox repository
or the old runtime.

Preconditions (refused otherwise, changing nothing): the driver was stopped with the audited `driver stop`; <Base>\textkit exists.
What it does:
  1. builds a NEW runtime next to the old one (runtime-<sha8>) from the pinned commit;
  2. backs up .stagemesh\config.json, then writes the explicit provider authorization (codex and claude only), routing pools and the
     known-good pinned Codex command taken from the owner's working StageMesh project;
  3. adds the next ten independent-spec tasks (SANDBOX-11..20) without touching existing rows;
  4. proves authorization offline (`provider-authorization`, no model call) and verifies subscription logins;
  5. replaces the scheduled task through `driver uninstall` (old runtime) and `driver install` (new runtime), then records
     streak-start.json: only tasks that reach DONE after this moment count toward the new clean streak.
On any failure before step 5 nothing is scheduled or changed except the new runtime folder, which is removed.
#>
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Source = 'https://github.com/saketvishal/stagemesh.git',
    [string]$Base = 'C:\development\stagemesh-handsoff',
    [string]$KnownGoodConfig = 'C:\development\stagemesh\main\.stagemesh\config.json',
    [string]$Python = ''
)
$ErrorActionPreference = 'Continue'
$env:GIT_TERMINAL_PROMPT = '0'
if ($Commit -notmatch '^[0-9a-f]{40}$') { Write-Host 'REFUSED: -Commit must be a full 40-character sha'; exit 2 }
$project = Join-Path $Base 'textkit'
$oldRuntime = Join-Path $Base 'runtime'
$newRuntime = Join-Path $Base ("runtime-" + $Commit.Substring(0, 8))
$created = $false
function Fail($message) {
    Write-Host "REFUSED: $message" -ForegroundColor Red
    if ($created -and (Test-Path $newRuntime)) { Set-Location $env:TEMP; Remove-Item -Recurse -Force $newRuntime -ErrorAction SilentlyContinue; Write-Host "Removed the partial $newRuntime" }
    exit 1
}
function Invoke-Bounded($exe, $argList, $timeoutSeconds) {
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    $quoted = $argList | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }
    $p = Start-Process -FilePath $exe -ArgumentList $quoted -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -NoNewWindow
    $null = $p.Handle
    if (-not $p.WaitForExit($timeoutSeconds * 1000)) { taskkill /F /T /PID $p.Id *> $null; Remove-Item $out, $err -ErrorAction SilentlyContinue; return @{ code = -1; text = 'timed out' } }
    $p.WaitForExit()
    $result = @{ code = [int]$p.ExitCode; text = "$(Get-Content $out -Raw -ErrorAction SilentlyContinue)$(Get-Content $err -Raw -ErrorAction SilentlyContinue)" }
    Remove-Item $out, $err -ErrorAction SilentlyContinue
    return $result
}

# 0. preconditions ----------------------------------------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $project '.stagemesh\continuous-driver.json'))) { Fail "no installed sandbox at $project" }
$driverState = (Get-Content (Join-Path $project '.stagemesh\continuous-driver.json') -Raw | ConvertFrom-Json).state
if ($driverState -ne 'stopped') { Fail "the driver is '$driverState'; stop it first with the audited driver stop command" }
if (Test-Path $newRuntime) { Fail "$newRuntime already exists" }
$oldPy = Join-Path $oldRuntime '.venv\Scripts\python.exe'
if (-not (Test-Path $oldPy)) { Fail "the old runtime was not found at $oldRuntime" }

$codexCommand = $null
if (Test-Path $KnownGoodConfig) {
    try {
        $known = (Get-Content $KnownGoodConfig -Raw | ConvertFrom-Json).providers.codex
        if ($known -is [string]) { $codexCommand = $known } elseif ($known) { $codexCommand = "$($known.command)" }
    } catch { }
}
if (-not $codexCommand -or $codexCommand -notmatch '(--model|\s-m)\s') { Fail "no known-good pinned Codex command found in $KnownGoodConfig" }

$py = $null
$candidates = @()
if ($Python) { $candidates += , @($Python) }
$candidates += , @('py', '-3.13'); $candidates += , @('py', '-3.12'); $candidates += , @('py', '-3.11'); $candidates += , @('python')
foreach ($c in $candidates) {
    $exe = $c[0]; $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    $found = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11)); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $found -and $found[0] -eq 'True') { $py = $found[1]; break }
}
if (-not $py) { Fail 'Python 3.11 or newer was not found' }

# 1. new runtime beside the old one -------------------------------------------------------------------------------------------
New-Item -ItemType Directory -Path $newRuntime | Out-Null
$created = $true
git init -q $newRuntime
$fetch = Invoke-Bounded 'git' @('-C', $newRuntime, 'fetch', '-q', '--depth', '1', $Source, $Commit) 300
if ($fetch.code -ne 0) { Fail "could not fetch $Commit" }
git -C $newRuntime checkout -q FETCH_HEAD
if ((git -C $newRuntime rev-parse HEAD) -ne $Commit) { Fail 'pinned commit mismatch' }
$venv = Invoke-Bounded $py @('-m', 'venv', (Join-Path $newRuntime '.venv')) 300
$vpy = Join-Path $newRuntime '.venv\Scripts\python.exe'
if ($venv.code -ne 0 -or -not (Test-Path $vpy)) { Fail 'venv creation failed' }
$pip = Invoke-Bounded $vpy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-e', "${newRuntime}[dev]") 900
if ($pip.code -ne 0) { Fail 'pip install failed or timed out' }

# 2. subscription logins (no model call) ----------------------------------------------------------------------------------------
$authRun = Invoke-Bounded $vpy @('-m', 'stagemesh.cli', 'provider-auth', '--json', '--require-installed') 180
$auth = $null
try { $auth = $authRun.text | ConvertFrom-Json } catch { }
if ($authRun.code -ne 0 -or -not $auth -or $auth.ok -ne $true) { Fail ("subscription check failed: " + $authRun.text) }

# 3. configuration (backup first) and tasks -----------------------------------------------------------------------------------------
$configPath = Join-Path $project '.stagemesh\config.json'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
Copy-Item $configPath "$configPath.pre-upgrade-$stamp"
$current = Get-Content $configPath -Raw | ConvertFrom-Json
$budgets = if ($current.budgets) { $current.budgets } else { @{ max_provider_calls_per_task = 12; max_equivalent_failures = 3; max_provider_calls_per_window = 30 } }
$config = @{
    budgets = $budgets
    routing = @{ require_independent_review = $true; pools = @{ IMPLEMENT = @('codex', 'claude'); REVIEW = @('codex', 'claude') } }
    authorization = @{ unattended_providers = @('codex', 'claude') }
    providers = @{ codex = @{ command = $codexCommand; capabilities = @('IMPLEMENT', 'REVIEW') } }
} | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($configPath, $config, (New-Object Text.UTF8Encoding($false)))
$ext = Invoke-Bounded $vpy @((Join-Path $newRuntime 'scripts\handsoff_sandbox.py'), $project, '--extend') 300
if ($ext.code -ne 0) { Copy-Item "$configPath.pre-upgrade-$stamp" $configPath -Force; Fail "adding tasks failed: $($ext.text)" }

# 4. offline authorization proof ---------------------------------------------------------------------------------------------------
$az = Invoke-Bounded $vpy @('-m', 'stagemesh.cli', '--project', $project, 'provider-authorization') 120
if ($az.code -ne 0) { Copy-Item "$configPath.pre-upgrade-$stamp" $configPath -Force; Fail "provider authorization check failed: $($az.text)" }
$evidenceDir = Join-Path $Base 'evidence'
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
$az.text | Out-File (Join-Path $evidenceDir "provider-authorization-$stamp.json") -Encoding UTF8

# 5. swap the scheduled task; the ledger and repository are never touched ------------------------------------------------------
$un = Invoke-Bounded $oldPy @('-m', 'stagemesh.cli', '--project', $project, 'driver', 'uninstall') 180
if ($un.code -ne 0) { Fail "driver uninstall (old runtime) failed: $($un.text)" }
$created = $false   # the new runtime is in use from here on
$inst = Invoke-Bounded $vpy @('-m', 'stagemesh.cli', '--project', $project, 'driver', 'install', '--acknowledge-provider-use', '--interval', '10m',
    '--window-duration', '40m', '--concurrency', '1', '--max-tasks', '2', '--max-steps-per-task', '6', '--max-consecutive-failures', '3', '--integration', 'local', '--json') 300
$inst.text | Out-File (Join-Path $evidenceDir "install-$stamp.json") -Encoding UTF8
if ($inst.code -ne 0) {
    Write-Host 'REFUSED: driver install with the new runtime failed; the sandbox is stopped and unscheduled. See evidence\install-*.json' -ForegroundColor Red
    exit 1
}
@{ commit = $Commit; started_utc = (Get-Date).ToUniversalTime().ToString('o'); rule = 'only tasks reaching DONE after started_utc, by codex and claude with independent review and no founder intervention, count' } |
    ConvertTo-Json | Out-File (Join-Path $Base 'streak-start.json') -Encoding UTF8
$cli = "& `"$vpy`" -m stagemesh.cli --project `"$project`""
@"
StageMesh hands-off sandbox  (runtime $Commit)
Project: $project   Runtime: $newRuntime   Old runtime kept: $oldRuntime
Log: $project\.stagemesh\driver\driver.log   Evidence: $evidenceDir
  status  $cli driver status
  pause   $cli driver pause --reason "manual"
  resume  $cli driver resume
  stop    $cli driver stop --terminate-running --reason "manual"
  remove  $cli driver uninstall
Authorized providers: codex and claude only (project config authorization.unattended_providers).
"@ | Set-Content -Path (Join-Path $Base 'README-handsoff.txt') -Encoding UTF8
Write-Host ''
Write-Host 'UPGRADED. Authorized providers: codex, claude. New clean streak starts now. Ledger and repository untouched.'
