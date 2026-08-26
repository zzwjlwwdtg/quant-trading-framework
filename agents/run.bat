@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
REM Secrets (FRED_API_KEY, MOOMOO_ACC_ID) loaded from secrets.local.json by config.py
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM default: LIVE on moomoo SIMULATE account. for dry-run: set TRADER_DRY_RUN=1
if "%TRADER_DRY_RUN%"=="" set "TRADER_DRY_RUN=0"
if "%TRADER_SIM_ACTIVE%"=="" set "TRADER_SIM_ACTIVE=1"
if "%AI_CLI_PRIMARY%"=="" set "AI_CLI_PRIMARY=codex"
if "%AI_CLI_FALLBACK%"=="" set "AI_CLI_FALLBACK=none"
if "%CLAUDE_DECISION_GATE%"=="" set "CLAUDE_DECISION_GATE=1"
if "%CLAUDE_DECISION_MODE%"=="" set "CLAUDE_DECISION_MODE=gate"
if "%CLAUDE_DECISION_TIMEOUT_SEC%"=="" set "CLAUDE_DECISION_TIMEOUT_SEC=180"
if "%CLAUDE_DECISION_FAIL_CLOSED%"=="" set "CLAUDE_DECISION_FAIL_CLOSED=1"
REM Crisis V-bounce probe (backtest 5d hit 80% avg +6%, probe 30% pos)
if "%CRISIS_VBOUNCE_ENABLED%"=="" set "CRISIS_VBOUNCE_ENABLED=1"
REM Auto-rebalance: pre-close hook writes signals/rebalance_plan.jsonl + submits orders
REM Backtest 5d +$2.9K after falling-knife filter; falls back to dry-run if unset
if "%AUTO_REBALANCE_EXECUTE%"=="" set "AUTO_REBALANCE_EXECUTE=1"

cd /d "%SCRIPT_DIR%"
if not exist logs mkdir logs

REM Auto-clean zombie python.exe from this project (stuck-at-pause test runs etc.)
REM Protects the active orchestrator/snapshot via their lock files.
"%PY%" -X utf8 -u _cleanup_zombies.py

REM Single-instance guard: refuse to start if another orchestrator is alive.
REM orchestrator.py also locks via .orchestrator.lock, but this gives an
REM instant message in the bat window instead of waiting for python startup.
if exist ".orchestrator.lock" (
    set /p ORCH_PID=<.orchestrator.lock
    tasklist /FI "PID eq !ORCH_PID!" /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
    if not errorlevel 1 (
        echo ============================================================
        echo   [WARN] orchestrator already running ^(PID=!ORCH_PID!^)
        echo   This run.bat will NOT start a second instance.
        echo   To restart: taskkill /PID !ORCH_PID! /F ^&^& del .orchestrator.lock
        echo ============================================================
        pause
        endlocal
        exit /b 1
    )
    echo [INFO] stale lock for dead PID !ORCH_PID! - python will clear it
)

if "%TRADER_LIVE_FRACTION%"=="" set "TRADER_LIVE_FRACTION=1.0"

echo Trading Agents starting...
echo   TRADER_DRY_RUN=%TRADER_DRY_RUN%   (0=LIVE on moomoo SIMULATE, 1=dry log-only)
echo   TRADER_LIVE_FRACTION=%TRADER_LIVE_FRACTION%   (gradual rollout: 0.1=10%% size, 1.0=full)
echo   TRADER_SIM_ACTIVE=%TRADER_SIM_ACTIVE%   (1=active probes; SIMULATE account only)
echo   AI_CLI=%AI_CLI_PRIMARY%   fallback=%AI_CLI_FALLBACK%
echo   CLAUDE_DECISION_GATE=%CLAUDE_DECISION_GATE%   (legacy name; 1=AI pre-trade approval required)
echo   AUTO_REBALANCE_EXECUTE=%AUTO_REBALANCE_EXECUTE%   (1=pre-close 自动再平衡下单, 0=dry-run 只 log)
echo   Tools menu: run tools.bat
echo   Log: logs\run_YYYYMMDD.log
echo.

"%PY%" -X utf8 orchestrator.py

pause
endlocal
