@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe
"%PY%" -X utf8 premarket_wrap.py tick > logs/premarket_wrap_last.log 2>&1
