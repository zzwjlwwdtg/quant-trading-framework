@echo off
REM AI CLI channel monitor: alert once per ET day when claude/codex keeps failing >60 min (read-only, every ~30 min via watchdog)
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 ai_channel_monitor.py tick > "%~dp0logs\ai_channel_monitor_last.log" 2>&1
del "%~dp0signals\job_ai_channel_monitor.running" 2>nul
