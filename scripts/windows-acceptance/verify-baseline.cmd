@echo off
REM Double-click launcher. Runs the inspectable PowerShell script next to it with a process-scoped execution policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify-baseline.ps1" %*
