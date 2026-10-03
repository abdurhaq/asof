@echo off
REM Double-click to set up, download, build and evaluate AsOf. Progress: logs\run_all.log
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_all.ps1" %*
echo.
echo Finished. You can close this window.
pause
