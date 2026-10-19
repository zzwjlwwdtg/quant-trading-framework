@echo off
REM EVO-20261013-3 redo: premarket take-profit defer, ~2y 1h prepost backtest. Read-only, no orders.
REM Output: development\<date>\premarket_tp_long\result.json ; verdict: signals\backtest_verdicts\premarket_tp_defer_long.json
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
echo [%DATE% %TIME%] start > "%~dp0logs\premarket_tp_long_last.log"
"%PY%" -X utf8 -u _backtest_premarket_tp_long.py >> "%~dp0logs\premarket_tp_long_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\premarket_tp_long_last.log"
echo [%DATE% %TIME%] done >> "%~dp0logs\premarket_tp_long_last.log"
del "%~dp0signals\job_premarket_tp_long.running" 2>nul
