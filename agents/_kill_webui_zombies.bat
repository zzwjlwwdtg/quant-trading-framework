@echo off
REM Kill orphan WebUI instances only (not the 8080 owner). Log: logs\kill_webui_zombies_last.txt
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_kill_webui_zombies.ps1"
