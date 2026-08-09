@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%SCRIPT_DIR%"

REM Watchdog: check http://127.0.0.1:8080/api/health; restart webui if dead
"%PY%" -X utf8 -u _webui_watchdog.py

endlocal
