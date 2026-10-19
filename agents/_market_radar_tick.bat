@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe
"%PY%" -X utf8 market_radar.py tick > logs/market_radar_last.log 2>&1
