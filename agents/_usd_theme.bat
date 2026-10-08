@echo off
REM Strong-USD theme: daily dollar strength / spillover readings + invalidation check. Read-only.
REM Output: signals\usd_theme.json ; log: logs\usd_theme_last.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u usd_theme.py > logs\usd_theme_last.log 2>&1
del "%~dp0signals\job_usd_theme.running" 2>nul
