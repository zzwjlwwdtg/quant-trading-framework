@echo off
REM bot self-test: GET /api/bot/* on 127.0.0.1:8080, write signals\bot_selftest.json (read-only)
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 _bot_selftest.py > "%~dp0logs\bot_selftest_last.log" 2>&1
del "%~dp0signals\job_bot_selftest.running" 2>nul
