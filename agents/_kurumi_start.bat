@echo off
REM On-demand (user request): start the Kurumi QQ bot with its saved production settings. Refuses duplicates by itself.
chcp 65001 > nul
cd /d "D:\personal plan"
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\personal plan\scripts\kurumi-persona.ps1" -Action Start > "%~dp0logs\kurumi_start_last.log" 2>&1
echo exit=%ERRORLEVEL% >> "%~dp0logs\kurumi_start_last.log"
del "%~dp0signals\job_kurumi_start.running" 2>nul
