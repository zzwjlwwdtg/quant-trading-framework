@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%SCRIPT_DIR%"

REM Trump Truth Social signal (last 24h) via AI CLI (Claude first, Codex fallback)
"%PY%" -X utf8 -u trump_signal.py --hours 24

echo.
pause
endlocal
