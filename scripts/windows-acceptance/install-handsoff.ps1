<#
One-time installation of the PERSISTENT StageMesh driver in an isolated development project (textkit sandbox).

Refuses to run unless ALL of these hold, and changes nothing before they do:
  * the native mock-provider acceptance (run-native-acceptance.ps1) passed for exactly this commit;
  * `stagemesh provider-auth` finds BOTH coding CLIs installed and logged in with a subscription, and no API-billing
    environment variable is visible (no model call is made to find out);
  * the target directory does not exist yet.
It then creates C:\development\stagemesh-handsoff\{runtime,textkit}, installs the pinned StageMesh into its own venv, builds the
sandbox repo with ten real tasks and independent specs, and registers ONE Task Scheduler task through `driver install`
(bounded windows, bounded provider calls, local integration inside the sandbox repo only, no PRs, no pushes, no deploys).
Pause, resume, stop and uninstall are in README-handsoff.txt next to the project.
#>
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Source = 'https://github.com/saketvishal/stagemesh.git',
    [string]$Base = 'C:\development\stagemesh-handsoff',
    [string]$EvidenceRoot = 'C:\development\stagemesh\main\.stagemesh\acceptance-evidence',
    [string]$Python = ''
)
$ErrorActionPreference = 'Continue'
function Fail($message) { Write-Host "REFUSED: $message" -ForegroundColor Red; exit 1 }

# 1. gate: native acceptance passed for this exact commit ---------------------------------------------------------------------
$passed = $false
if (Test-Path $EvidenceRoot) {
    foreach ($dir in (Get-ChildItem $EvidenceRoot -Directory -Filter 'native-*' | Sort-Object Name -Descending)) {
        $file = Join-Path $dir.FullName 'summary.json'
        if (Test-Path $file) {
            $s = Get-Content $file -Raw | ConvertFrom-Json
            if ($s.commit -eq $Commit -and $s.result -eq 'PASS') { $passed = $true; break }
        }
    }
}
if (-not $passed) { Fail "no passing native acceptance evidence for commit $Commit under $EvidenceRoot" }
if (Test-Path $Base) { Fail "$Base already exists; refusing to reuse or overwrite it" }

# 2. python ---------------------------------------------------------------------------------------------------------------------
$py = $null
$candidates = @()
if ($Python) { $candidates += , @($Python) }
$candidates += , @('py', '-3.13'); $candidates += , @('py', '-3.12'); $candidates += , @('py', '-3.11'); $candidates += , @('python')
foreach ($c in $candidates) {
    $exe = $c[0]; $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    $out = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11)); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq 'True') { $py = $out[1]; break }
}
if (-not $py) { Fail 'Python 3.11 or newer was not found' }

# 3. build runtime in a staging dir; only move into place after every check passed ------------------------------------------
$stage = Join-Path $env:TEMP ("sm-handsoff-stage-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage | Out-Null
$runtime = Join-Path $stage 'runtime'
git init -q $runtime
git -C $runtime fetch -q --depth 1 $Source $Commit
git -C $runtime checkout -q FETCH_HEAD
if ((git -C $runtime rev-parse HEAD) -ne $Commit) { Remove-Item -Recurse -Force $stage; Fail 'pinned commit mismatch' }
& $py -m venv (Join-Path $runtime '.venv')
$vpy = Join-Path $runtime '.venv\Scripts\python.exe'
& $vpy -m pip install --quiet --disable-pip-version-check -e $runtime
if ($LASTEXITCODE -ne 0) { Remove-Item -Recurse -Force $stage; Fail 'pip install failed' }

# 4. subscription gate (no model call) ------------------------------------------------------------------------------------------
$auth = (& $vpy -m stagemesh.cli provider-auth --json) | ConvertFrom-Json
$problems = @()
foreach ($p in $auth.providers) {
    if (-not $p.installed) { $problems += "$($p.provider) CLI is not installed" }
    elseif (-not $p.subscription) { $problems += "$($p.provider) is logged in as '$($p.auth_mode)', not a subscription" }
}
foreach ($b in $auth.blockers) { $problems += $b }
if ($problems.Count -gt 0) {
    Remove-Item -Recurse -Force $stage
    Fail ("subscription check failed: " + ($problems -join '; ') + '. No model call was made and nothing was installed.')
}
Write-Host 'Subscription logins verified for claude and codex (no model call made, no API billing variables set).'

# 5. move into place, build the sandbox project, configure bounds ----------------------------------------------------------------
New-Item -ItemType Directory -Path $Base | Out-Null
Move-Item $runtime (Join-Path $Base 'runtime')
Remove-Item -Recurse -Force $stage
$runtime = Join-Path $Base 'runtime'
$vpy = Join-Path $runtime '.venv\Scripts\python.exe'
$project = Join-Path $Base 'textkit'
& $vpy (Join-Path $runtime 'scripts\handsoff_sandbox.py') $project | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'sandbox creation failed (is a global git user.name/user.email configured?)' }
$config = @{
    budgets = @{ max_provider_calls_per_task = 12; max_equivalent_failures = 3; max_provider_calls_per_window = 30 }
    routing = @{ require_independent_review = $true }
} | ConvertTo-Json -Depth 5
Set-Content -Path (Join-Path $project '.stagemesh\config.json') -Value $config -Encoding UTF8

# 6. register the driver (the product's own install path) ----------------------------------------------------------------------
& $vpy -m stagemesh.cli --project $project driver install --acknowledge-provider-use --interval 10m --window-duration 40m `
    --concurrency 1 --max-tasks 2 --max-steps-per-task 6 --max-consecutive-failures 3 --integration local --json | Out-File (Join-Path $Base 'install.json') -Encoding UTF8
if ($LASTEXITCODE -ne 0) { Fail 'driver install failed; see output above (nothing is scheduled)' }
$status = (& $vpy -m stagemesh.cli --project $project driver status --json) | ConvertFrom-Json

$cli = "`"$vpy`" -m stagemesh.cli --project `"$project`""
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
