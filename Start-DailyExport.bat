@echo off
setlocal
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_daily_export.ps1"
exit /b %ERRORLEVEL%
