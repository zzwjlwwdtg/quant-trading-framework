@echo off
REM Overnight overseas drivers -> A-share open gap / intraday, pre-registered backtest (read-only)
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 -u _backtest_overnight_ashare.py > "%~dp0logs\overnight_bt_last.log" 2>&1
del "%~dp0signals\job_overnight_bt.running" 2>nul
