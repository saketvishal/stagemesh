param([string]$Label = "baseline", [switch]$SkipFull)
# StageMesh Windows host verification, batch 1: read-only with respect to the repo.
# Creates only: .venv-win (pytest venv), .accept-evidence\<run>\ (logs), and pytest temp dirs under %TEMP%.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
function Stamp { (Get-Date).ToString("o") }
$run = Get-Date -Format "yyyyMMdd-HHmmss"
$ev = Join-Path $root (".accept-evidence\" + $run + "-" + $Label)
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript -Path (Join-Path $ev "transcript.txt") | Out-Null
"started $(Stamp) pid=$PID user=$env:USERNAME host=$env:COMPUTERNAME"
[System.Environment]::OSVersion.VersionString
"git: $(git --version)"
"head: $(git rev-parse HEAD)"
$py = $null
foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    $parts = $cand.Split(" ")
    $exe = $parts[0]; $rest = @(); if ($parts.Length -gt 1) { $rest = $parts[1..($parts.Length - 1)] }
    try {
        $out = & $exe @rest -c "import sys; print(int(sys.version_info >= (3, 11))); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out[0] -eq "1") { $py = $out[1]; break }
    } catch { }
}
if (-not $py) { "NO_PYTHON_3_11_FOUND"; "blocked" | Set-Content (Join-Path $ev "done.flag"); Stop-Transcript | Out-Null; exit 2 }
"python: $py"
$venv = Join-Path $root ".venv-win"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & $py -m venv $venv }
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install -q pytest 2>&1 | Select-Object -Last 3
$env:PYTHONDONTWRITEBYTECODE = "1"
$results = [ordered]@{}
function Run-Suite($name, $pytestArgs) {
    $log = Join-Path $ev ($name + ".log")
    $start = Stamp
    & $vpy -m pytest -p no:cacheprovider -q @pytestArgs *>&1 | Tee-Object -FilePath $log | Select-Object -Last 6
    $code = $LASTEXITCODE
    $results[$name] = [ordered]@{ exit = $code; start = $start; end = (Stamp) }
    "suite $name exit=$code"
}
Run-Suite "driver" @("tests/test_continuous_driver.py", "tests/test_driver_acceptance.py")
Run-Suite "ownership" @("tests/test_workspace_ownership.py", "tests/test_builtin_execution_ownership.py")
Run-Suite "autonomy" @("tests/test_autonomy_base_state.py", "tests/test_autonomy_lifecycle.py", "tests/test_autonomy_delivery.py", "tests/test_autonomy_dependencies.py", "tests/test_autonomy_corpus.py")
if (-not $SkipFull) { Run-Suite "full" @("tests") }
$results | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $ev "summary.json")
"finished $(Stamp)"
"done" | Set-Content (Join-Path $ev "done.flag")
Stop-Transcript | Out-Null
Start-Sleep -Seconds 3
