"""_probe_veto_live_tracker.py — sim_active_probe_after_claude_veto 规则 live 表现 tracker

EVO-20261122-1:
  读 cohort_tracker ledger (position_cohorts.jsonl) 和 trade_log.jsonl 里由
  sim_active_probe_after_claude_veto 规则触发的建仓记录, 聚合 live 分布:
    · hit-stop 率 (止损退出占比)
    · 平均 PnL %
    · 平均持仓时长 (天)
  累计 N 样本后与预注册 OOS 分布做偏离检验 (binomial z-test, 2σ criterion);
  偏离超阈值仅输出告警, 不停单, 不改规则, 不拉黑标的.

设计约束:
  · 只读: 不改入场规则, 不改止损, 不拉黑标的
  · 告警: 偏离仅写 backtest_verdicts.live_followup + 打印告警信息
  · N < MIN_N 时不执行偏离检验 (防止小样本假阳性)

预注册阈值 (EVO-20261122-1):
  MIN_N = 20, α = 0.05
  kill-switch criterion = "live hit-stop 率 > OOS 预期 + 2σ" (z > 1.96)
  OOS 预期由 EVO-20261118-1 backtest verdict 文件提供;
  若 verdict 文件尚未写入, 使用 OOS_HIT_STOP_RATE_DEFAULT 作为占位.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import SIGNALS_DIR

# ── 常量 ────────────────────────────────────────────────────────────────────

RULE_ID     = "sim_active_probe_after_claude_veto"
# claude_gate.py 在 demote 时将 action 改为 "WATCH_BUY_PROBE" (唯一标识)
RULE_ACTION = "WATCH_BUY_PROBE"

# 预注册阈值
MIN_N                       = 20     # 最少样本数才启动偏离检验
ALPHA                       = 0.05   # 显著性水平 (单侧 z ≥ 1.96)
Z_THRESHOLD                 = 1.96
# EVO-20261118-1 OOS 验证得出的 hit-stop 率预期; verdict 文件不存在时用此默认
OOS_HIT_STOP_RATE_DEFAULT   = 0.35

_COHORT_LEDGER = Path(SIGNALS_DIR) / "position_cohorts.jsonl"
_TRADE_LOG     = Path(SIGNALS_DIR) / "trade_log.jsonl"


# ── 识别与分类 ────────────────────────────────────────────────────────────

def _is_rule_entry(signal_ctx: dict) -> bool:
    """判断 cohort open 的 signal_ctx 是否由 sim_active_probe_after_claude_veto 触发.

    检测顺序:
    1. action == RULE_ACTION ("WATCH_BUY_PROBE") — claude_gate.py 专属 demote action
    2. reason 以 RULE_ID 开头 — 冗余双保险 (同一信号源的两个字段一致)
    """
    if not isinstance(signal_ctx, dict):
        return False
    if signal_ctx.get("action") == RULE_ACTION:
        return True
    reason = signal_ctx.get("reason") or ""
    if isinstance(reason, str) and reason.startswith(RULE_ID):
        return True
    return False


def _is_stop_exit(reason: str) -> bool:
    """判断退出原因是否含止损 (TRAILING-STOP / SOFTWARE-STOP / HARD-STOP 等)."""
    return "STOP" in (reason or "").upper()


# ── 数据加载 ────────────────────────────────────────────────────────────────

def load_rule_cohorts_from_ledger(
    ledger_path: Optional[Path] = None,
) -> list[dict]:
    """从 position_cohorts.jsonl 读取由该规则触发且已 close 的 cohort.

    两遍扫描:
    1. 收集所有由该规则触发的 cohort_id (open 事件 signal_ctx 匹配)
    2. 收集这些 cohort_id 的 close 事件

    返回: list of closed cohort dicts, 格式与 load_rule_trades_from_tradelog 对齐.
    """
    p = ledger_path or _COHORT_LEDGER
    if not p.exists():
        return []

    rule_cohort_ids: set[str]        = set()
    close_events:    dict[str, dict] = {}  # cohort_id → close event

    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue

        event     = ev.get("event")
        cohort_id = ev.get("cohort_id", "")

        if event == "open":
            signal_ctx = ev.get("signal_ctx") or {}
            if _is_rule_entry(signal_ctx):
                rule_cohort_ids.add(cohort_id)

        if event == "close" and cohort_id:
            close_events[cohort_id] = ev

    result = []
    for cid in rule_cohort_ids:
        if cid not in close_events:
            continue
        ev     = close_events[cid]
        cohort = ev.get("cohort") or {}
        result.append({
            "cohort_id":        cid,
            "ticker":           ev.get("ticker") or cohort.get("ticker", ""),
            "close_ts":         ev.get("ts", ""),
            "close_reason":     ev.get("close_reason") or ev.get("reason") or "",
            "hold_days":        float(ev.get("hold_days")
                                      or cohort.get("hold_days") or 0),
            "realized_pnl_pct": float(ev.get("realized_pnl_pct")
                                      or cohort.get("realized_pnl_pct") or 0),
            "realized_pnl_usd": float(ev.get("realized_pnl_usd")
                                      or cohort.get("realized_pnl_usd") or 0),
            "is_winner":        bool(ev.get("is_winner")
                                     or cohort.get("is_winner")),
            "source":           "cohort_ledger",
        })
    return result


def load_rule_trades_from_tradelog(
    log_path: Optional[Path] = None,
) -> list[dict]:
    """从 trade_log.jsonl 读取 WATCH_BUY_PROBE BUY/SELL 配对作为影子回填.

    用于历史样本 (cohort_ledger 未记录的老 live 样本, 包括 INTC 等).
    返回与 load_rule_cohorts_from_ledger 相同格式.
    FIFO 配对: 每个 ticker 维护开仓队列, SELL 消耗最早的 BUY.
    """
    p = log_path or _TRADE_LOG
    if not p.exists():
        return []

    events: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not ev.get("dry_run", False):
                events.append(ev)
    except OSError:
        return []

    open_positions: dict[str, list[dict]] = defaultdict(list)
    result: list[dict] = []

    for ev in events:
        ticker   = ev.get("ticker", "")
        side     = (ev.get("side") or "").upper()
        decision = ev.get("decision") or {}

        if side == "BUY":
            d_action = decision.get("action") or ""
            d_reason = decision.get("reason") or ""
            is_rule  = (
                d_action == RULE_ACTION or
                (isinstance(d_reason, str) and d_reason.startswith(RULE_ID))
            )
            if is_rule:
                open_positions[ticker].append({
                    "qty":      ev.get("qty") or 0,
                    "price":    float(ev.get("price") or 0),
                    "ts":       ev.get("ts", ""),
                    "order_id": str(ev.get("order_id") or ""),
                })

        elif side in ("SELL", "SELL_ALL", "REDUCE"):
            if not open_positions[ticker]:
                continue
            pos = open_positions[ticker][0]
            try:
                entry_dt  = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
                exit_dt   = datetime.fromisoformat(
                    ev["ts"].replace("Z", "+00:00"))
                hold_days = (exit_dt - entry_dt).total_seconds() / 86400
            except Exception:
                hold_days = 0.0
            ep      = pos["price"]
            xp      = float(ev.get("price") or 0)
            pnl_pct = (xp - ep) / ep * 100 if ep > 0 else 0.0
            qty     = min(int(ev.get("qty") or 0), int(pos["qty"]))
            close_reason = ev.get("tag") or ""
            cohort_id    = f"tradelog_{ticker}_{pos['order_id']}"
            result.append({
                "cohort_id":        cohort_id,
                "ticker":           ticker,
                "close_ts":         ev.get("ts", ""),
                "close_reason":     close_reason,
                "hold_days":        round(hold_days, 2),
                "realized_pnl_pct": round(pnl_pct, 3),
                "realized_pnl_usd": round((xp - ep) * qty, 2),
                "is_winner":        pnl_pct > 0,
                "source":           "trade_log",
            })
            open_positions[ticker].pop(0)

    return result


def load_all_rule_samples(
    ledger_path: Optional[Path] = None,
    log_path:    Optional[Path] = None,
) -> list[dict]:
    """合并 cohort_ledger + trade_log 两个来源, 双层去重.

    cohort_ledger 权威; trade_log 仅补充不在 ledger 里的历史样本.
    去重策略:
    1. cohort_id 精确去重 (同一系统内)
    2. (ticker, close_date) 近似去重: 同日同标的的 close 在两源视为同一真实交易,
       保留 cohort_ledger 版本 (权威), 丢弃 trade_log 版本 (防止重复计数).
    """
    ledger_samples   = load_rule_cohorts_from_ledger(ledger_path)
    tradelog_samples = load_rule_trades_from_tradelog(log_path)

    seen_ids:         set[str]           = {s["cohort_id"] for s in ledger_samples}
    # (ticker, YYYY-MM-DD) 对用于跨源去重
    seen_ticker_date: set[tuple[str, str]] = set()
    for s in ledger_samples:
        date_str = (s.get("close_ts") or "")[:10]
        seen_ticker_date.add((s["ticker"], date_str))

    merged = list(ledger_samples)
    for s in tradelog_samples:
        if s["cohort_id"] in seen_ids:
            continue
        date_str = (s.get("close_ts") or "")[:10]
        if (s["ticker"], date_str) in seen_ticker_date:
            continue  # 同日同标的 close 在 cohort_ledger 中已有权威版本
        merged.append(s)
        seen_ids.add(s["cohort_id"])
        seen_ticker_date.add((s["ticker"], date_str))
    return merged


# ── 统计计算 ────────────────────────────────────────────────────────────────

def compute_live_stats(samples: list[dict]) -> dict:
    """聚合 live 分布.

    返回:
      n, n_stop, hit_stop_rate, n_winners, win_rate,
      avg_pnl_pct, median_pnl_pct, p25_pnl_pct, p75_pnl_pct,
      avg_hold_days, tickers
    """
    if not samples:
        return {
            "n": 0, "n_stop": 0, "hit_stop_rate": None,
            "n_winners": 0, "win_rate": None,
            "avg_pnl_pct": None, "median_pnl_pct": None,
            "p25_pnl_pct": None, "p75_pnl_pct": None,
            "avg_hold_days": None, "tickers": [],
        }

    n       = len(samples)
    n_stop  = sum(1 for s in samples if _is_stop_exit(s.get("close_reason", "")))
    pnls    = sorted(float(s.get("realized_pnl_pct") or 0) for s in samples)
    holds   = [float(s.get("hold_days") or 0) for s in samples]
    n_win   = sum(1 for s in samples if s.get("is_winner", False))

    def _pct(frac: float) -> float:
        # 线性插值分位数
        pos   = (n - 1) * frac
        lo    = int(pos)
        hi    = min(lo + 1, n - 1)
        return pnls[lo] + (pnls[hi] - pnls[lo]) * (pos - lo)

    return {
        "n":               n,
        "n_stop":          n_stop,
        "hit_stop_rate":   round(n_stop / n, 4),
        "n_winners":       n_win,
        "win_rate":        round(n_win / n, 4),
        "avg_pnl_pct":     round(sum(pnls) / n, 4),
        "median_pnl_pct":  round(_pct(0.5), 4),
        "p25_pnl_pct":     round(_pct(0.25), 4),
        "p75_pnl_pct":     round(_pct(0.75), 4),
        "avg_hold_days":   round(sum(holds) / n, 2),
        "tickers":         sorted({s["ticker"] for s in samples}),
    }


# ── 偏离检验 ────────────────────────────────────────────────────────────────

def deviation_test(live_stats: dict, oos_params: dict) -> dict:
    """预注册偏离检验: live hit-stop 率 vs OOS 预期 (binomial proportion z-test).

    kill-switch criterion (EVO-20261122-1):
      live hit-stop 率 > OOS 预期 + 2σ  →  triggered=True, 只告警不停单.

    公式:
      σ = sqrt(p0 * (1-p0) / n)
      z = (p_hat - p0) / σ
      triggered ↔ p_hat > p0 + Z_THRESHOLD * σ

    返回 dict: triggered, min_n_met, z_score, oos_hit_stop_rate,
               live_hit_stop_rate, sigma, threshold_hit_stop,
               alpha, alert_message
    """
    n     = live_stats.get("n") or 0
    min_n = int(oos_params.get("min_n") or MIN_N)

    if n < min_n:
        return {
            "triggered":          False,
            "min_n_met":          False,
            "z_score":            None,
            "oos_hit_stop_rate":  None,
            "live_hit_stop_rate": live_stats.get("hit_stop_rate"),
            "sigma":              None,
            "threshold_hit_stop": None,
            "alpha":              ALPHA,
            "alert_message":      f"N={n} < 预注册最小样本 {min_n}, 暂不检验",
        }

    p_hat     = float(live_stats.get("hit_stop_rate") or 0)
    p0        = float(oos_params.get("oos_hit_stop_rate") or OOS_HIT_STOP_RATE_DEFAULT)
    sigma     = math.sqrt(p0 * (1 - p0) / n) if n > 0 else 0.0
    threshold = p0 + Z_THRESHOLD * sigma
    z_score   = (p_hat - p0) / sigma if sigma > 0 else 0.0
    triggered = p_hat > threshold

    alert_msg: Optional[str] = None
    if triggered:
        alert_msg = (
            f"[ALERT] {RULE_ID} live hit-stop 率偏高: "
            f"live={p_hat:.1%} > OOS+2σ={threshold:.1%} "
            f"(OOS={p0:.1%}, σ={sigma:.3f}, z={z_score:.2f}, N={n}). "
            f"建议人工复核规则有效性, 冻结决策不自动执行."
        )

    return {
        "triggered":          triggered,
        "min_n_met":          True,
        "z_score":            round(z_score, 4),
        "oos_hit_stop_rate":  round(p0, 4),
        "live_hit_stop_rate": round(p_hat, 4),
        "sigma":              round(sigma, 4),
        "threshold_hit_stop": round(threshold, 4),
        "alpha":              ALPHA,
        "alert_message":      alert_msg,
    }


# ── 主入口 ────────────────────────────────────────────────────────────────

def run_tracker(
    ledger_path: Optional[Path] = None,
    log_path:    Optional[Path] = None,
    *,
    verbose: bool = True,
) -> dict:
    """主入口: 读数据 → 算 live stats → 偏离检验 → 更新 verdict live_followup.

    返回: {live_stats, deviation, alert_triggered, samples_n}.
    不改交易路径, 不改止损, 只读.
    """
    # 读 OOS 预期 (verdict 文件, 由 EVO-20261118-1 backtest 落档)
    try:
        from backtest_verdicts import read_verdict as _rv
        verdict = _rv(RULE_ID) or {}
    except Exception:
        verdict = {}

    oos_params = {
        "oos_hit_stop_rate": (verdict.get("metrics") or {}).get(
            "oos_hit_stop_rate", OOS_HIT_STOP_RATE_DEFAULT),
        "min_n":             (verdict.get("metrics") or {}).get(
            "min_n_for_test", MIN_N),
    }

    samples    = load_all_rule_samples(ledger_path, log_path)
    live_stats = compute_live_stats(samples)
    dev        = deviation_test(live_stats, oos_params)

    # 更新 verdict live_followup (写盘失败不影响告警逻辑)
    live_followup = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "live_stats": live_stats,
        "deviation":  dev,
    }
    try:
        from backtest_verdicts import update_live_followup as _ulf
        _ulf(RULE_ID, live_followup)
    except Exception:
        pass

    if verbose:
        _print_summary(live_stats, dev)

    return {
        "live_stats":      live_stats,
        "deviation":       dev,
        "alert_triggered": bool(dev.get("triggered", False)),
        "samples_n":       live_stats.get("n") or 0,
    }


# ── 展示 ────────────────────────────────────────────────────────────────────

def _print_summary(live_stats: dict, dev: dict) -> None:
    """打印 tracker 摘要 (供 postmortem / CLI 展示)."""
    n = live_stats.get("n") or 0
    print(f"\n  ── {RULE_ID} live tracker ──")
    if n == 0:
        print("    暂无已 close 的 live 样本.")
        return

    hit_r  = live_stats.get("hit_stop_rate") or 0
    win_r  = live_stats.get("win_rate") or 0
    avg_p  = live_stats.get("avg_pnl_pct") or 0
    med_p  = live_stats.get("median_pnl_pct") or 0
    p25    = live_stats.get("p25_pnl_pct") or 0
    p75    = live_stats.get("p75_pnl_pct") or 0
    hold   = live_stats.get("avg_hold_days") or 0

    print(f"    N={n}  hit-stop={hit_r:.1%}  win={win_r:.1%}  "
          f"avg_pnl={avg_p:+.2f}%  median={med_p:+.2f}%  avg_hold={hold:.1f}d")
    print(f"    PnL 分位: p25={p25:+.2f}%  p75={p75:+.2f}%")

    tickers = live_stats.get("tickers") or []
    if tickers:
        shown = tickers[:10]
        suffix = " ..." if len(tickers) > 10 else ""
        print(f"    样本标的: {', '.join(shown)}{suffix}")

    if not dev.get("min_n_met"):
        print(f"    偏离检验: {dev.get('alert_message', '暂不检验')}")
    elif dev.get("triggered"):
        print(f"    ⚠ {dev['alert_message']}")
    else:
        z         = dev.get("z_score") or 0
        threshold = dev.get("threshold_hit_stop") or 0
        print(f"    偏离检验: 通过  z={z:.2f}  阈值 hit-stop < {threshold:.1%}")


if __name__ == "__main__":
    import sys
    result = run_tracker()
    sys.exit(1 if result["alert_triggered"] else 0)
