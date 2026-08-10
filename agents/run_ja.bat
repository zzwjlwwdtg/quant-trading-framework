@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
REM Secrets (FRED_API_KEY, MOOMOO_ACC_ID) loaded from secrets.local.json
set "PYTHONUTF8=1"
set "OUTPUT_LANG=ja"

REM default: LIVE on moomoo SIMULATE account. for dry-run: set TRADER_DRY_RUN=1
if "%TRADER_DRY_RUN%"=="" set "TRADER_DRY_RUN=0"
if "%TRADER_SIM_ACTIVE%"=="" set "TRADER_SIM_ACTIVE=1"
if "%CLAUDE_DECISION_GATE%"=="" set "CLAUDE_DECISION_GATE=1"
if "%CLAUDE_DECISION_MODE%"=="" set "CLAUDE_DECISION_MODE=gate"
if "%CLAUDE_DECISION_TIMEOUT_SEC%"=="" set "CLAUDE_DECISION_TIMEOUT_SEC=180"
if "%CLAUDE_DECISION_FAIL_CLOSED%"=="" set "CLAUDE_DECISION_FAIL_CLOSED=1"
REM Crisis V-bounce probe (same default as run.bat and watchdog)
if "%CRISIS_VBOUNCE_ENABLED%"=="" set "CRISIS_VBOUNCE_ENABLED=1"

cd /d "%SCRIPT_DIR%"
if not exist logs mkdir logs

REM Auto-clean zombie python.exe from this project
"%PY%" -X utf8 -u _cleanup_zombies.py

REM Single-instance guard (Japanese-mode orchestrator)
if exist ".orchestrator.lock" (
    set /p ORCH_PID=<.orchestrator.lock
    tasklist /FI "PID eq !ORCH_PID!" /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
    if not errorlevel 1 (
        echo ============================================================
        echo   [WARN] orchestrator already running ^(PID=!ORCH_PID!^)
        echo   This run_ja.bat will NOT start a second instance.
        echo   To restart: taskkill /PID !ORCH_PID! /F ^&^& del .orchestrator.lock
        echo ============================================================
        pause
        endlocal
        exit /b 1
    )
    echo [INFO] stale lock for dead PID !ORCH_PID! - python will clear it
)

echo ============================================================
echo  Trading Agents (Japanese mode)
echo  Core: TQQQ + SOXL + GLD   + SOX satellite (PCA dynamic picks)
echo ============================================================
if "%TRADER_DRY_RUN%"=="1" (
    echo  TRADER MODE:  [DRY-RUN]  log only, no orders sent
) else (
    echo  TRADER MODE:  [LIVE]     SIMULATE account real orders
)
echo  CLAUDE GATE:  %CLAUDE_DECISION_GATE%  (1=pre-trade approval required)
echo ============================================================
echo.

if "%OPENAI_API_KEY%"=="" (
    echo [INFO] OPENAI_API_KEY not set - using rule-based fallback
) else (
    echo [INFO] GPT-4o-mini decision engine active
)
echo [INFO] Pre-open: regime -^> SOX picks -^> evolver -^> trader.apply_universe
echo [INFO] Tools:    run tools.bat for status / flatten / picks / regime
echo [INFO] Log file: logs\run_YYYYMMDD.log  UTF-8
echo [INFO] OUTPUT_LANG=ja  Claude AI analysis output in Japanese
echo.

REM AI analysis (Claude CLI) is the heaviest text output - now in Japanese.
REM System reports (signal tables, leaderboards) remain Chinese (terse, mostly numbers).
"%PY%" -X utf8 orchestrator.py

pause
endlocal
