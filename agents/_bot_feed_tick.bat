@echo off
REM bot feed: push new paper-account operations to BOT_WEBHOOK_URL (read-only, every ~5 min via watchdog)
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 bot_gateway.py tick > "%~dp0logs\bot_feed_last.log" 2>&1
del "%~dp0signals\job_bot_feed_tick.running" 2>nul
