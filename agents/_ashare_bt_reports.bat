@echo off
REM A-share backtest (reports): read-only, pre-registered. Output development\2026-10-19\ashare_backtest\
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" -X utf8 -u _backtest_ashare_signals.py reports > "%~dp0logs\ashare_bt_reports_last.log" 2>&1
del "%~dp0signals\job_ashare_bt_reports.running" 2>nul
