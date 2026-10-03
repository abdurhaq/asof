@echo off
REM Manage AsOf on AWS: status and link, recent app log, keep-warm on/off, delete everything.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\asof\venv\Scripts\python.exe"
"%PY%" -c "import boto3" 2>nul || "%PY%" -m pip install -q -U boto3
"%PY%" scripts\deploy_aws.py menu %*
echo.
pause
