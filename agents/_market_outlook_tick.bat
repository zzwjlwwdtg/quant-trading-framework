@echo off
REM Operator-only fallback. The watchdog uses Python directly without cmd/windows.
cd /d "%~dp0"
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 market_outlook.py tick
