@echo off
REM Read-only AI CLI diagnostics (codex / claude). Output: logs\ai_cli_diag_last.json
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _ai_cli_diag.py > logs\ai_cli_diag_run.log 2>&1
del "%~dp0signals\job_ai_cli_diag.running" 2>nul
