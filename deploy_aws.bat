@echo off
REM Deploy (or update) AsOf on AWS Lambda. The first time you are asked for your AWS access key (secret input is hidden).
cd /d "%~dp0"
set "ASOF_DATA_DIR=%LOCALAPPDATA%\asof\data"
set "PY=%LOCALAPPDATA%\asof\venv\Scripts\python.exe"
echo Checking the AWS library ...
"%PY%" -m pip install -q -U boto3
"%PY%" scripts\deploy_aws.py deploy %*
echo.
pause
