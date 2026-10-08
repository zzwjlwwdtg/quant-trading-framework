@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
REM Secrets (FRED_API_KEY etc.) loaded from secrets.local.json by config.py
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TRADER_DRY_RUN=1"
if "%AI_CLI_PRIMARY%"=="" set "AI_CLI_PRIMARY=claude"
if "%AI_CLI_FALLBACK%"=="" set "AI_CLI_FALLBACK=codex"
if "%SNAPSHOT_TIMEOUT_SEC%"=="" set "SNAPSHOT_TIMEOUT_SEC=900"
if "%SNAPSHOT_WITH_AI%"=="" set "SNAPSHOT_WITH_AI=1"
if "%SNAPSHOT_AI_TIMEOUT_SEC%"=="" set "SNAPSHOT_AI_TIMEOUT_SEC=300"

cd /d "%SCRIPT_DIR%"

REM One-shot snapshot: regime + trump + signals + decisions + event calendar
"%PY%" -X utf8 -u _snapshot_today.py

echo.
pause
endlocal
