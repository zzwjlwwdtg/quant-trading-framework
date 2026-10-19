@echo off
REM READ-ONLY: lists python/cmd/OpenD processes to logs\proc_report_last.txt
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_proc_report.ps1"
