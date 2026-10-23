@echo off
REM cn_brief: regenerate the 9:00 concise morning brief on demand (read-only). Output: signals\cn_brief\latest_morning.md
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 -m cn_brief run --slot morning --json > "%~dp0logs\cn_brief_morning_last.log" 2>&1
del "%~dp0signals\job_cn_brief_morning.running" 2>nul
