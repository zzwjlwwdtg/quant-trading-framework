@echo off
REM Daily AI post-mortem of every sell (5 reasons + 3 limitations). Read-only, no orders.
REM Output: signals\postmortem\latest.json ; log: logs\trade_postmortem_last.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "AI_CLI_PRIMARY=claude"
set "AI_CLI_FALLBACK=codex"
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u trade_postmortem.py
del "%~dp0signals\job_trade_postmortem.running" 2>nul
