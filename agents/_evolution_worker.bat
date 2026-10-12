@echo off
REM Self-evolution (worker). Proposals need owner approval; development only after approval. Kill switch: agents\.evolution_off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "AI_CLI_PRIMARY=claude"
set "AI_CLI_FALLBACK=codex"
"C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 -u evolution.py work
del "%~dp0signals\job_evo_worker.running" 2>nul
