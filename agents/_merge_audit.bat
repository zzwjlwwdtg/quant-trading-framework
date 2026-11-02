@echo off
REM Daily audit: trading-critical commits on main must have a recorded user approval (read-only git, alerts via notifications)
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 _merge_audit.py run > "%~dp0logs\merge_audit_last.log" 2>&1
del "%~dp0signals\job_merge_audit.running" 2>nul
