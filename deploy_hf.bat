@echo off
REM Deploy AsOf to a Hugging Face Space. You will be asked for a Hugging Face WRITE token (input is hidden).
cd /d "%~dp0"
set "ASOF_DATA_DIR=%LOCALAPPDATA%\asof\data"
"%LOCALAPPDATA%\asof\venv\Scripts\python.exe" scripts\deploy_hf.py %*
echo.
pause
