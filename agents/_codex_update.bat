@echo off
REM 2026-10-15: upgrade Codex CLI via npm (trading AI fallback). Output: logs\codex_update_last.json
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u _codex_update.py > logs\codex_update_run.log 2>&1
del "%~dp0signals\job_codex_update.running" 2>nul
