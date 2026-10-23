@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diag-killtree.ps1" %*
