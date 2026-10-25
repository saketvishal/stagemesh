<#
One-time installation of the PERSISTENT StageMesh driver in an isolated development project (textkit sandbox).

Refuses to run unless ALL of these hold, and leaves nothing behind if any fails before `driver install` succeeds:
  * the newest native mock-provider acceptance evidence (run-native-acceptance.ps1) is PASS for exactly this commit;
  * `stagemesh provider-auth` finds BOTH coding CLIs installed and logged in with a subscription, and no API-billing
    environment variable is visible (no model call is made to find out);
  * the target directory does not exist yet.
It then creates <Base>\{runtime,textkit}, installs the pinned StageMesh into its own venv, builds the sandbox repo with ten real
tasks and independent specs, and registers ONE Task Scheduler task through `driver install` (bounded windows, bounded provider
calls, local integration inside the sandbox repo only, no PRs, no pushes, no deploys).
Pause, resume, stop and uninstall are in README-handsoff.txt next to the project.
#>
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Source = 'https://github.com/saketvishal/stagemesh.git',
    [string]$Base = 'C:\development\stagemesh-handsoff',
    [string]$EvidenceRoot = '',
    [string]$Python = '',
    [string]$KnownGoodConfig = 'C:\development\stagemesh\main\.stagemesh\config.json'
)
$ErrorActionPreference = 'Continue'
if ($Commit -notmatch '^[0-9a-f]{40}$') { Write-Host 'REFUSED: -Commit must be a full 40-character sha'; exit 2 }
if (-not $EvidenceRoot) {
    $main = 'C:\development\stagemesh\main'
    if (Test-Path $main) { $EvidenceRoot = Join-Path $main '.stagemesh\acceptance-evidence' }
    else { $EvidenceRoot = Join-Path $env:USERPROFILE 'stagemesh-acceptance-evidence' }
}
$createdBase = $false
$env:GIT_TERMINAL_PROMPT = '0'
function Invoke-Bounded($exe, $argList, $timeoutSeconds) {
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    $quoted = $argList | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }
    $p = Start-Process -FilePath $exe -ArgumentList $quoted -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -NoNewWindow
    $null = $p.Handle
    if (-not $p.WaitForExit($timeoutSeconds * 1000)) { taskkill /F /T /PID $p.Id *> $null; Remove-Item $out, $err -ErrorAction SilentlyContinue; return @{ code = -1; text = 'timed out' } }
    $p.WaitForExit()
    $text = (Get-Content $out -Raw -ErrorAction SilentlyContinue)
    $result = @{ code = [int]$p.ExitCode; text = "$text" }
    Remove-Item $out, $err -ErrorAction SilentlyContinue
    return $result
}
function Fail($message) {
    Write-Host "REFUSED: $message" -ForegroundColor Red
    if ($createdBase -and (Test-Path $Base)) {
        Set-Location $env:TEMP
        Remove-Item -Recurse -Force $Base -ErrorAction SilentlyContinue
        Write-Host "Removed the partial $Base"
    }
    exit 1
}

# 1. gate: the NEWEST native acceptance evidence must be a PASS for this exact commit -----------------------------------------
$newest = $null
if (Test-Path $EvidenceRoot) { $newest = Get-ChildItem $EvidenceRoot -Directory -Filter 'native-*' | Sort-Object Name -Descending | Select-Object -First 1 }
if (-not $newest) { Fail "no native acceptance evidence under $EvidenceRoot" }
$summaryFile = Join-Path $newest.FullName 'summary.json'
$s = $null
if (Test-Path $summaryFile) { try { $s = Get-Content $summaryFile -Raw | ConvertFrom-Json } catch { } }
if (-not $s -or $s.commit -ne $Commit -or $s.result -ne 'PASS') { Fail "the newest native acceptance ($($newest.Name)) is not a PASS for commit $Commit" }
if (Test-Path $Base) { Fail "$Base already exists; refusing to reuse or overwrite it" }

# 2. python ---------------------------------------------------------------------------------------------------------------------
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

# 3. build the runtime at its FINAL location (a venv cannot be moved) --------------------------------------------------------------
New-Item -ItemType Directory -Path $Base | Out-Null
$createdBase = $true
$runtime = Join-Path $Base 'runtime'
git init -q $runtime
$fetch = Invoke-Bounded 'git' @('-C', $runtime, 'fetch', '-q', '--depth', '1', $Source, $Commit) 300
if ($fetch.code -ne 0) { Fail "could not fetch $Commit from $Source" }
git -C $runtime checkout -q FETCH_HEAD
if ((git -C $runtime rev-parse HEAD) -ne $Commit) { Fail 'pinned commit mismatch' }
$venvRun = Invoke-Bounded $py @('-m', 'venv', (Join-Path $runtime '.venv')) 300
if ($venvRun.code -ne 0) { Fail 'venv creation failed' }
$vpy = Join-Path $runtime '.venv\Scripts\python.exe'
if (-not (Test-Path $vpy)) { Fail 'venv creation failed' }
$pip = Invoke-Bounded $vpy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-e', "${runtime}[dev]") 900
if ($pip.code -ne 0) { Fail 'pip install failed or timed out' }

# 4. subscription gate (no model call); fails closed on anything unexpected ------------------------------------------------------
$authRun = Invoke-Bounded $vpy @('-m', 'stagemesh.cli', 'provider-auth', '--json', '--require-installed') 180
$authText = $authRun.text
$authExit = $authRun.code
$auth = $null
try { $auth = $authText | ConvertFrom-Json } catch { }
$problems = @()
if (-not $auth) { $problems += 'provider-auth output was unreadable' }
else {
    foreach ($name in 'claude', 'codex') {
        $p = @($auth.providers | Where-Object { $_.provider -eq $name })[0]
        if (-not $p) { $problems += "$name state missing" }
        elseif ($p.installed -ne $true) { $problems += "$name CLI is not installed" }
        elseif ($p.subscription -ne $true) { $problems += "$name is logged in as '$($p.auth_mode)', not a subscription" }
    }
    foreach ($b in @($auth.blockers)) { $problems += "$b" }
    if ($auth.ok -ne $true) { $problems += 'provider-auth did not report ok' }
}
if ($authExit -ne 0) { $problems += "provider-auth exit code $authExit" }
if ($problems.Count -gt 0) { Fail ("subscription check failed: " + (($problems | Select-Object -Unique) -join '; ') + '. No model call was made and nothing was installed.') }
Write-Host 'Subscription logins verified for claude and codex (no model call made, no API billing variables set).'

# 5. sandbox project and bounds -------------------------------------------------------------------------------------------------
$project = Join-Path $Base 'textkit'
& $vpy (Join-Path $runtime 'scripts\handsoff_sandbox.py') $project
if ($LASTEXITCODE -ne 0) { Fail 'sandbox creation failed (is a global git user.name/user.email configured?)' }
# The Codex command comes from a project where Codex is known to work (pinned model, working flags), never from a guess.
$codexCommand = $null
if (Test-Path $KnownGoodConfig) {
    try {
        $known = (Get-Content $KnownGoodConfig -Raw | ConvertFrom-Json).providers.codex
        if ($known -is [string]) { $codexCommand = $known } elseif ($known) { $codexCommand = "$($known.command)" }
    } catch { }
}
if (-not $codexCommand -or $codexCommand -notmatch '(--model|\s-m)\s') { Fail "no known-good pinned Codex command found in $KnownGoodConfig" }
$config = @{
    budgets = @{ max_provider_calls_per_task = 12; max_equivalent_failures = 3; max_provider_calls_per_window = 30 }
    routing = @{ require_independent_review = $true; pools = @{ IMPLEMENT = @('codex', 'claude'); REVIEW = @('codex', 'claude') } }
    authorization = @{ unattended_providers = @('codex', 'claude') }
    providers = @{ codex = @{ command = $codexCommand; capabilities = @('IMPLEMENT', 'REVIEW') } }
} | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $project '.stagemesh\config.json'), $config, (New-Object Text.UTF8Encoding($false)))

# 5b. prove authorization offline (no model call): only codex and claude may be eligible
$authz = (& $vpy -m stagemesh.cli --project $project provider-authorization | Out-String)
$authzExit = $LASTEXITCODE
$authzObj = $null
try { $authzObj = $authz | ConvertFrom-Json } catch { }
if ($authzExit -ne 0 -or -not $authzObj -or $authzObj.ok -ne $true) { Fail "provider authorization check failed: $authz" }
$authz | Out-File (Join-Path $Base 'provider-authorization.json') -Encoding UTF8

# 6. register the driver (the product's own install path) ----------------------------------------------------------------------
$installOut = & $vpy -m stagemesh.cli --project $project driver install --acknowledge-provider-use --interval 10m --window-duration 40m `
    --concurrency 1 --max-tasks 2 --max-steps-per-task 6 --max-consecutive-failures 3 --integration local --json
$installExit = $LASTEXITCODE
$installOut | Out-File (Join-Path $Base 'install.json') -Encoding UTF8
if ($installExit -ne 0) {
    & $vpy -m stagemesh.cli --project $project driver uninstall 2>$null | Out-Null
    Fail 'driver install failed; see install.json (nothing is scheduled)'
}
$createdBase = $false   # from here on the installation is real and is never auto-removed
$status = (& $vpy -m stagemesh.cli --project $project driver status --json | Out-String) | ConvertFrom-Json

$cli = "& `"$vpy`" -m stagemesh.cli --project `"$project`""
@"
StageMesh hands-off sandbox  (commit $Commit)
Project:  $project
Runtime:  $runtime
Log:      $project\.stagemesh\driver\driver.log
Ledger:   $project\.stagemesh\stagemesh.sqlite3
Reports:  $project\.stagemesh\driver\reports\

You do not need to run anything. These exist for emergencies:
  status    $cli driver status
  pause     $cli driver pause --reason "manual"
  resume    $cli driver resume
  stop      $cli driver stop --terminate-running --reason "manual"
  remove    $cli driver uninstall

Bounds: one task at a time, at most 2 tasks and 6 steps per task per 40-minute window, one window per 10 minutes,
12 provider calls per task, 30 per window, three consecutive failures block the driver until resumed.
Integration is local to this sandbox repository only. Nothing is pushed, published, merged elsewhere or deployed.
"@ | Set-Content -Path (Join-Path $Base 'README-handsoff.txt') -Encoding UTF8

Write-Host ''
Write-Host "INSTALLED. state=$($status.state) schedule_exists=$($status.schedule_exists)"
Write-Host "Project: $project"
Write-Host "Emergency commands: $Base\README-handsoff.txt"
