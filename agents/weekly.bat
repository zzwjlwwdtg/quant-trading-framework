@echo off
chcp 65001 > nul
setlocal

set "PY=C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe"
set "SCRIPT_DIR=%~dp0"
REM Secrets loaded from secrets.local.json by config.py
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%SCRIPT_DIR%"

REM Weekly: refresh module accuracy report (5-10 min)
REM Output: signals/module_accuracy.md (auto-injected into AI prompt)
"%PY%" -X utf8 -u _backtest_modules_accuracy.py

REM Weekly: recalibrate confluence weights + confidence percentiles (~1-2 min)
REM Output: signals/confidence_calibration.json (read by confluence.py + decision_agent.py)
"%PY%" -X utf8 -u _calibrate_confidence.py

REM Weekly: benchmark NAV vs SPY/QQQ (alpha + sharpe + max DD)
REM Output: signals/benchmark_report.md
"%PY%" -X utf8 -u _benchmark_report.py

REM Weekly: trade postmortem (BUY/SELL pairing + attribution)
REM Output: signals/trade_postmortem.md (after enough live trades)
"%PY%" -X utf8 -u _trade_postmortem.py

REM Weekly: data source health check (yfinance / FRED / moomoo / AI CLI / calibration / HMM)
"%PY%" -X utf8 -u _data_source_health.py

REM Weekly: claude_gate hit-rate audit (parse 60-90d prompt+raw pairs, compare verdict vs 5d fwd returns)
REM Output: prints APPROVE rate + HOLD median with alert thresholds
"%PY%" -X utf8 -u _backtest_claude_gate.py

REM Weekly: auto-rebalance audit (replay historical rebalance_plan.jsonl for 5d/10d P&L)
REM Output: prints each historical rebalance plan's actual forward P&L
"%PY%" -X utf8 -u _backtest_rebalance.py

REM Weekly: 26-year liquidity history (MOVE + funding + bank stress, monthly aggregation)
REM Output: signals/liquidity_history.json (dashboard 26 年图渲染的数据源)
"%PY%" -X utf8 -u _build_liquidity_history.py

REM Weekly: impact_matrix historical calibration (6 events → model vs actual diff)
REM Output: stdout - 累积数据, 若 median diff 系统性偏离才手动调 magnitudes
"%PY%" -X utf8 -u _backtest_impact_matrix.py

REM Weekly: 经济日历动态刷新 (FRED release schedule 官方 API)
REM Output: signals/economic_calendar.json — events_watch.py 优先读它
REM 若 FRED 不可用 events_watch 会 fallback 到 hardcoded (可能过时)
"%PY%" -X utf8 -u _refresh_calendar.py

REM Weekly: cache prune (删 >30d 缓存, 归档 >20MB 大文件, 防无限增长)
REM Output: 释放的 MB 数
"%PY%" -X utf8 -u _cache_prune.py

REM Daily/Weekly: thesis invalidation check (CPI MoM + macro conditions)
REM Output: signals/thesis_invalidation_log.jsonl + notification on trigger
"%PY%" -X utf8 -u _check_thesis_invalidation.py

REM Weekly: auto-rerun expired backtest verdicts + diff alert
REM Output: signals/verdict_change_log.jsonl + notification on verdict change
"%PY%" -X utf8 -u _weekly_backtest_review.py

echo.
REM 只有交互式（有 stdin）才 pause，避免 Task Scheduler 卡住
if defined SESSIONNAME if "%SESSIONNAME%"=="Console" pause
endlocal
