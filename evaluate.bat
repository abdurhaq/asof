@echo off
REM Re-run only the benchmark and the audit export (no download, no rebuild). Progress: logs\run_all.log
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_all.ps1" -Steps eval,audit
echo.
echo Finished. You can close this window.
pause
