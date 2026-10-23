# Read-only provider and project inspection. Makes NO provider calls. Writes only .accept-evidence\<run>-providers\.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$mainProject = "C:\development\stagemesh\main"
Set-Location $root
$ev = Join-Path $root (".accept-evidence\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-providers")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript -Path (Join-Path $ev "transcript.txt") | Out-Null
"started $((Get-Date).ToString('o')) pid=$PID"
foreach ($tool in @("codex", "claude", "gh", "git", "py", "python")) {
    $found = Get-Command $tool -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { "$tool -> $($found.Source)" } else { "$tool -> NOT FOUND" }
}
foreach ($c in @(@("codex", "--version"), @("claude", "--version"), @("gh", "--version"), @("gh", "auth", "status"))) {
    "--- $($c -join ' ')"
    try { & $c[0] $c[1..($c.Length - 1)] 2>&1 | Select-Object -First 8 } catch { "ERR $_" }
}
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$vpy = Join-Path $root ".venv-win\Scripts\python.exe"
foreach ($sub in @("doctor", "capacity")) {
    "--- stagemesh $sub (project $mainProject)"
    & $vpy -m stagemesh.cli --project $mainProject $sub 2>&1 | Tee-Object -FilePath (Join-Path $ev "$sub.txt") | Select-Object -First 80
}
"--- scheduled tasks named StageMesh (read-only listing)"
schtasks /Query /FO LIST 2>$null | Select-String -Pattern "TaskName:.*StageMesh" 
"finished $((Get-Date).ToString('o'))"
"done" | Set-Content (Join-Path $ev "done.flag")
Stop-Transcript | Out-Null
Start-Sleep -Seconds 3
