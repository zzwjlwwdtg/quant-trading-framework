@echo off
REM cn_brief tick (every ~5 min via watchdog): fixed-slot reports 09:00/16:30/21:00 JST + 30-min pre-release reminders + post-release results
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 -m cn_brief tick > "%~dp0logs\cn_brief_tick_last.log" 2>&1
del "%~dp0signals\job_cn_brief_tick.running" 2>nul
