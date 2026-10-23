@echo off
REM Safe WebUI restart: runs _restart_webui.ps1, output saved to logs\restart_webui_last.log
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_restart_webui.ps1" > "%~dp0logs\restart_webui_last.log" 2>&1
type "%~dp0logs\restart_webui_last.log"
timeout /t 20
