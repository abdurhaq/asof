@echo off
REM Re-run only build, evaluation and audit (after code fixes). Progress: logs\run_all.log
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_all.ps1" -Steps build,eval,audit
echo.
echo Finished. You can close this window.
pause
