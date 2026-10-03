@echo off
REM Quick health check (Groq, Neo4j, RBI website). Output also saved to logs\doctor.log
cd /d "%~dp0"
if not exist logs mkdir logs
"%LOCALAPPDATA%\asof\venv\Scripts\python.exe" -m asof.cli doctor > logs\doctor.log 2>&1
type logs\doctor.log
pause
