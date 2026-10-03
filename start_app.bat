@echo off
REM Double-click to start the AsOf web app at http://127.0.0.1:8000 (close this window to stop it)
cd /d "%~dp0"
set "ASOF_DATA_DIR=%LOCALAPPDATA%\asof\data"
set "PYTHONIOENCODING=utf-8"
start "" cmd /c "timeout /t 12 >nul & start http://127.0.0.1:8000"
"%LOCALAPPDATA%\asof\venv\Scripts\python.exe" -m asof.cli serve
pause
