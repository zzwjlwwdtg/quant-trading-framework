@echo off
REM cn_brief: on-demand A-share report digest + econ calendar (read-only). Output: signals\cn_brief\latest_now.md
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 -m cn_brief run --slot now --json > "%~dp0logs\cn_brief_now_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\cn_brief_now_last.log"
"%PY%" -X utf8 -m cn_brief calendar --days 3 > "%~dp0logs\cn_brief_calendar_last.log" 2>&1
del "%~dp0signals\job_cn_brief_now.running" 2>nul
