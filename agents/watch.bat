@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
REM Secrets loaded from secrets.local.json by config.py (not needed here, log-only)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM Initial backlog lines (default 30). Override: set WATCH_TAIL=100 before launch.
if "%WATCH_TAIL%"=="" set "WATCH_TAIL=30"

cd /d "%SCRIPT_DIR%"
if not exist logs mkdir logs

title FSI Trading Monitor (single window - minimize this)
echo ================================================================
echo   FSI Trading Monitor
echo ================================================================
echo   This is the SINGLE monitor window for the whole trading system.
echo   All toasts/popups are disabled by default (TOAST_ENABLE=0).
echo   All signals + alerts stream here as log lines.
echo.
echo   Minimize this window and forget about it.
echo   Task bar cmd count > 1 = zombie process (should not happen).
echo.
echo   Ctrl+C or X = close monitor (orchestrator keeps running).
echo   Backlog lines: %WATCH_TAIL% (override: set WATCH_TAIL=N)
echo ================================================================
echo.

"%PY%" -X utf8 -u _log_watch.py

endlocal
