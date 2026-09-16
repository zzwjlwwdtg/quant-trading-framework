@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%SCRIPT_DIR%"

REM 1. moomoo OpenD watchdog: check process + port 11111; launch exe if dead.
REM    Auto-login handled by OpenD's own "remember password" GUI setting.
REM    OpenD must come up first so orchestrator can connect on restart.
"%PY%" -X utf8 -u _opend_watchdog.py

REM 2. Orchestrator watchdog: check .orchestrator.lock PID alive; restart if dead
REM    Task Scheduler friendly: no pause, exits after check
"%PY%" -X utf8 -u _watchdog.py

endlocal
