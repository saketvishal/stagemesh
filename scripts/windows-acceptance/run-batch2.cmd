@echo off
REM Batch 2: kill-tree diagnostic, then read-only provider/project inspection. No provider calls, no scheduler changes.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diag-killtree.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0inspect-providers.ps1"
