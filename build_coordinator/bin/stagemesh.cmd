@echo off
rem Location-independent launcher for the StageMesh CLI.
rem Delegates to stagemesh.ps1 in this same folder.
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%stagemesh.ps1" %*
exit /b %ERRORLEVEL%
