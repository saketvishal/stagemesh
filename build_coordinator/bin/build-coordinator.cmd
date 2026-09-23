@echo off
rem Location-independent launcher for the Build Coordinator CLI.
rem Delegates to build-coordinator.ps1 in this same folder.
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build-coordinator.ps1" %*
exit /b %ERRORLEVEL%
