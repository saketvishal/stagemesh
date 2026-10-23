# Runs the kill-tree diagnostic with the pytest venv. Creates only .accept-evidence\<run>-killtree\ and its own test processes.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$ev = Join-Path $root (".accept-evidence\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-killtree")
New-Item -ItemType Directory -Force $ev | Out-Null
Start-Transcript -Path (Join-Path $ev "transcript.txt") | Out-Null
"started $((Get-Date).ToString('o')) pid=$PID"
& (Join-Path $root ".venv-win\Scripts\python.exe") (Join-Path $PSScriptRoot "diag-killtree.py") $ev
"exit=$LASTEXITCODE finished $((Get-Date).ToString('o'))"
"done" | Set-Content (Join-Path $ev "done.flag")
Stop-Transcript | Out-Null
Start-Sleep -Seconds 3
