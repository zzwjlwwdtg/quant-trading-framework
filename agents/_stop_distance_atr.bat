@echo off
REM EVO-20261014-1: fixed 8% trailing stop vs ATR multiples, pre-registered train/OOS backtest. Read-only, no orders.
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
echo [%DATE% %TIME%] start > "%~dp0logs\stop_distance_atr_last.log"
"%PY%" -X utf8 -u _backtest_stop_distance.py >> "%~dp0logs\stop_distance_atr_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\stop_distance_atr_last.log"
echo [%DATE% %TIME%] done >> "%~dp0logs\stop_distance_atr_last.log"
del "%~dp0signals\job_stop_distance_atr.running" 2>nul
