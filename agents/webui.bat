@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if "%AI_CLI_PRIMARY%"=="" set "AI_CLI_PRIMARY=claude"
if "%AI_CLI_FALLBACK%"=="" set "AI_CLI_FALLBACK=codex"

REM Optional: override with env vars before launch
if "%WEBUI_PORT%"=="" set "WEBUI_PORT=8080"
if "%WEBUI_HOST%"=="" set "WEBUI_HOST=127.0.0.1"

cd /d "%SCRIPT_DIR%"

title FSI Trading Dashboard
echo Starting WebUI at http://%WEBUI_HOST%:%WEBUI_PORT%
echo AI CLI: %AI_CLI_PRIMARY%  fallback=%AI_CLI_FALLBACK%
echo Open in browser then Ctrl+C here to stop.
echo.

"%PY%" -X utf8 -u webui.py

endlocal
