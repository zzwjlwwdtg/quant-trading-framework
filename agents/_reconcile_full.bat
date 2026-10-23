@echo off
REM Read-only broker history + fee query (no orders). Output: logs\reconcile_full_last.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" "%~dp0_broker_history_reconcile.py" --start 2024-01-01 --tickers ALL > "%~dp0logs\reconcile_full_last.log" 2>&1
echo done >> "%~dp0logs\reconcile_full_last.log"
timeout /t 10
