@echo off
REM Pre-registered test: how do tradable assets behave in strong-dollar regimes? Read-only.
REM Output: development\2026-10-08\usd_regime\report.md ; log: logs\usd_regime_test.log
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _usd_regime_test.py
del "%~dp0signals\job_usd_regime_test.running" 2>nul
