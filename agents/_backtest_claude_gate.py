"""
_backtest_claude_gate.py — 60d gate on/off 对比回测

原理:
  历史上每次 claude_gate 触发都会落两个文件:
    claude_gate_prompt_<TK>_<TS>.md   — 发给 CLI 的完整决策包 (含 market/regime/price)
    claude_gate_raw_<TK>_<TS>.txt     — CLI 返回的 JSON verdict

  这些文件构成天然的实盘 gate 决策日志. 逐个解析:
    · 从 prompt 提: ticker, date, price (entry_ref), regime, rule_action, rule_confidence
    · 从 raw 提: gate_verdict (APPROVE|HOLD|CAUTION), gate_reason
  然后用 yfinance 拉每个 ticker 的 5d/10d 前向 close, 算实际收益.

  三桶对照:
    APPROVE       - gate 放行 (最终执行, 视 sim_active 或 live 而定)
    HOLD (block)  - gate 完全否 (无 sim_active_probe 时不产单)
    CAUTION       - gate 警告但不硬否

  两个关键指标:
    1) APPROVE 桶 vs HOLD/CAUTION 桶: 若 HOLD 桶跌得更狠 → gate 挡对了
       若 HOLD 桶反而涨了 → gate 挡错了 (机会成本)
    2) 按 regime 切: gate 在哪个 regime 下最准/最错

用法:
    python _backtest_claude_gate.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import SIGNALS_DIR

SIG_DIR = Path(SIGNALS_DIR)

# ── 解析 prompt / raw 文件 ───────────────────────────────────────────────────
_RE_TS = re.compile(r'"timestamp_local":\s*"([^"]+)"')
_RE_TK = re.compile(r'"ticker":\s*"([^"]+)"')
_RE_PX = re.compile(r'"price":\s*([0-9.]+)')
_RE_RG = re.compile(r'"regime":\s*"([a-z_]+)"')
_RE_AC = re.compile(r'"action":\s*"([A-Z_]+)"')
_RE_CF = re.compile(r'"confidence":\s*(\d+)\s*,')  # 需要逗号避免匹配 schema 说明里的 "1-10"
_RE_WD = re.compile(r'"window":\s*"([a-z]+)"')
_RE_TREND = re.compile(r'"trend":\s*"([a-z]+)"')
_RE_ZONE = re.compile(r'"pct_chg_zone":\s*"([a-z]+)"')


def _parse_prompt(path: Path) -> dict | None:
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    ts_m = _RE_TS.search(txt)
    tk_m = _RE_TK.search(txt)
    px_m = _RE_PX.search(txt)
    rg_m = _RE_RG.search(txt)
    ac_m = _RE_AC.search(txt)
    cf_m = _RE_CF.search(txt)
    wd_m = _RE_WD.search(txt)
    if not (ts_m and tk_m and px_m and ac_m):
        return None
    trend_m = _RE_TREND.search(txt)
    zone_m = _RE_ZONE.search(txt)
    return {
        "ts": ts_m.group(1),
        "ticker": tk_m.group(1).replace("US.", ""),
        "price": float(px_m.group(1)),
        "regime": rg_m.group(1) if rg_m else "unknown",
        "action": ac_m.group(1),
        "confidence": int(cf_m.group(1)) if cf_m else 0,
        "window": wd_m.group(1) if wd_m else "unknown",
        "trend": trend_m.group(1) if trend_m else "neutral",
        "pct_zone": zone_m.group(1) if zone_m else "normal",
    }


def _parse_raw(path: Path) -> dict | None:
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    try:
        d = json.loads(txt)
        verdict = str(d.get("verdict", "")).upper()
        if verdict not in ("APPROVE", "HOLD", "CAUTION"):
            return None
        return {
            "verdict": verdict,
            "gate_conf": int(d.get("confidence", 0)),
            "reason": str(d.get("reason", ""))[:200],
            "risk_flags": d.get("risk_flags") or [],
        }
    except Exception:
        return None


def _pair_files() -> list[dict]:
    """扫描 signals/, 把 prompt + raw 按同名前缀配对成决策记录."""
    events = []
    for prompt_path in sorted(SIG_DIR.glob("claude_gate_prompt_*.md")):
        name = prompt_path.stem  # claude_gate_prompt_US-NBIS_20260813_010558
        # 对应的 raw 文件同后缀
        raw_stem = name.replace("prompt_", "raw_")
        raw_path = SIG_DIR / f"{raw_stem}.txt"
        if not raw_path.exists():
            continue
        pr = _parse_prompt(prompt_path)
        rw = _parse_raw(raw_path)
        if not pr or not rw:
            continue
        events.append({**pr, **rw})
    return events


# ── yfinance 前向价格 ───────────────────────────────────────────────────────
_PRICE_CACHE: dict[str, dict] = {}


def _forward_close(ticker: str, entry_date: datetime.date,
                   offset_days: int) -> float | None:
    """entry_date + offset_days 交易日的 close (跳过周末/假日, 前后 5 天内找)."""
    cache = _PRICE_CACHE.get(ticker)
    if cache is None:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="90d")
            if hist.empty:
                _PRICE_CACHE[ticker] = {}
                return None
            cache = {ts.date(): float(px) for ts, px in hist["Close"].items()}
            _PRICE_CACHE[ticker] = cache
        except Exception:
            _PRICE_CACHE[ticker] = {}
            return None
    if not cache:
        return None
    for i in range(offset_days, offset_days + 6):
        cand = entry_date + timedelta(days=i)
        if cand in cache:
            return cache[cand]
    return None


# ── 主逻辑 ──────────────────────────────────────────────────────────────────
def run() -> dict:
    events = _pair_files()
    print(f"配对到 {len(events)} 条 gate 历史决策")
    if not events:
        return {}

    # 时间窗口
    ts_list = [datetime.fromisoformat(e["ts"]).date() for e in events]
    print(f"覆盖: {min(ts_list)} → {max(ts_list)} ({(max(ts_list)-min(ts_list)).days} 天)")

    # 按 verdict 分桶, 再算 5d/10d 前向收益
    for e in events:
        e["date"] = datetime.fromisoformat(e["ts"]).date()
        for offset in (5, 10):
            fwd = _forward_close(e["ticker"], e["date"], offset)
            e[f"ret_{offset}d"] = ((fwd - e["price"]) / e["price"] * 100
                                    if fwd else None)

    # 全局对照
    print("\n" + "=" * 90)
    print("【全局】 按 gate verdict × 前向窗口")
    print("=" * 90)
    print(f"{'VERDICT':<10} {'N':<5} {'5d avg%':<11} {'5d win':<9} {'5d med%':<10} "
          f"{'10d avg%':<11} {'10d win':<9} {'10d med%':<10}")
    print("-" * 90)

    for verdict in ["APPROVE", "HOLD", "CAUTION"]:
        subset = [e for e in events if e["verdict"] == verdict]
        n = len(subset)
        if not n:
            continue
        line = f"{verdict:<10} {n:<5} "
        for offset in (5, 10):
            rets = [e[f"ret_{offset}d"] for e in subset
                    if e.get(f"ret_{offset}d") is not None]
            if not rets:
                line += f"{'n/a':<11} {'n/a':<9} {'n/a':<10} "
                continue
            avg = sum(rets) / len(rets)
            wins = sum(1 for r in rets if r > 0) / len(rets) * 100
            med = sorted(rets)[len(rets) // 2]
            line += f"{avg:>+8.2f}%   {wins:>5.1f}%   {med:>+6.2f}%   "
        print(line)

    # 按 regime 切
    print("\n" + "=" * 90)
    print("【按 regime】 gate 在不同市场环境的表现 (5d 前向)")
    print("=" * 90)
    by_rg = defaultdict(lambda: defaultdict(list))
    for e in events:
        r5 = e.get("ret_5d")
        if r5 is None:
            continue
        by_rg[e["regime"]][e["verdict"]].append(r5)

    print(f"{'REGIME':<18} {'APPROVE':<28} {'HOLD':<28} {'CAUTION':<28}")
    print("-" * 105)
    for regime in sorted(by_rg.keys()):
        row = f"{regime:<18} "
        for verdict in ("APPROVE", "HOLD", "CAUTION"):
            xs = by_rg[regime].get(verdict, [])
            if not xs:
                row += f"{'—':<28} "
            else:
                avg = sum(xs) / len(xs)
                win = sum(1 for r in xs if r > 0) / len(xs) * 100
                row += f"n={len(xs):<3} avg={avg:>+6.2f}% win={win:>5.1f}%   "
        print(row)

    # 关键判定
    print("\n" + "=" * 90)
    print("【核心结论】")
    print("=" * 90)
    approve_5d = [e["ret_5d"] for e in events
                  if e["verdict"] == "APPROVE" and e.get("ret_5d") is not None]
    hold_5d = [e["ret_5d"] for e in events
               if e["verdict"] == "HOLD" and e.get("ret_5d") is not None]
    if approve_5d and hold_5d:
        approve_avg = sum(approve_5d) / len(approve_5d)
        hold_avg = sum(hold_5d) / len(hold_5d)
        approve_win = sum(1 for r in approve_5d if r > 0) / len(approve_5d) * 100
        hold_win = sum(1 for r in hold_5d if r > 0) / len(hold_5d) * 100
        diff = hold_avg - approve_avg
        print(f"  APPROVE 5d: n={len(approve_5d)}, avg={approve_avg:+.2f}%, win={approve_win:.1f}%")
        print(f"  HOLD    5d: n={len(hold_5d)}, avg={hold_avg:+.2f}%, win={hold_win:.1f}%")
        print()
        if diff > 1:
            print(f"  ⚠ gate 挡错了: HOLD 桶比 APPROVE 桶反而高 {diff:+.2f}pp")
            print(f"    也就是 gate 挡下的信号 5d 里表现更好 → gate 过度保守")
        elif diff < -1:
            print(f"  ✓ gate 挡对了: HOLD 桶比 APPROVE 低 {-diff:.2f}pp")
            print(f"    gate 成功识别了差信号")
        else:
            print(f"  ~ 差异不显著 ({diff:+.2f}pp)")

    # regime 是否错的判定: 看 crisis / risk_off 的 APPROVE 表现
    print()
    print("【regime 判定检查】")
    for critical_regime in ("crisis", "risk_off", "recession_risk"):
        approve_rets = by_rg.get(critical_regime, {}).get("APPROVE", [])
        if approve_rets:
            avg = sum(approve_rets) / len(approve_rets)
            win = sum(1 for r in approve_rets if r > 0) / len(approve_rets) * 100
            note = ""
            if avg > 2:
                note = "  ← regime 可能过度悲观 (放行的仍然赚)"
            elif avg < -3:
                note = "  ← regime 判定合理 (放行的确实跌)"
            print(f"  regime={critical_regime:<15} APPROVE n={len(approve_rets):<3} "
                  f"avg={avg:+.2f}% win={win:.1f}%{note}")

    # ── 阶段 A 修复效果模拟 ───────────────────────────────────────────────
    # 用新规则对历史事件重新分类, 报告新 bucketing 下的表现. 用来验证前/后对比.
    #   A1: t10y2y<0 但 trend/zone 未确认 → 降到 risk_off
    #   A2: bull_pulling 要求 SOX 20d > 0, 否则 fallthrough 到 bull_trending
    print("\n" + "=" * 90)
    print("【阶段 A1+A2 模拟】 新 regime 规则重分类后的 5d 表现")
    print("  A1: recession_risk 需 trend=down + pct_zone∈{drop,crash}, 否则 → risk_off")
    print("  A2: bull_pulling 需 SOX 20d>0, 否则 → bull_trending")
    print("=" * 90)

    # 为 A2 拉 SOX 20d 动量: 一次性拉 SOXX 历史, 查每个事件日
    sox_20d_at: dict = {}
    try:
        import yfinance as yf
        sox_hist = yf.Ticker("SOXX").history(period="120d")
        if not sox_hist.empty:
            closes = list(sox_hist["Close"])
            dates = [ts.date() for ts in sox_hist.index]
            for i in range(20, len(closes)):
                sox_20d_at[dates[i]] = (closes[i] - closes[i-20]) / closes[i-20] * 100
    except Exception as ex:
        print(f"  warn: SOX 历史拉取失败 ({ex}), A2 模拟跳过")

    def _lookup_sox_20d(d):
        # 找不到就往前找 5 天
        for i in range(6):
            cand = d - timedelta(days=i)
            if cand in sox_20d_at:
                return sox_20d_at[cand]
        return None

    remapped = defaultdict(lambda: defaultdict(list))
    remap_events = []
    for e in events:
        r5 = e.get("ret_5d")
        if r5 is None:
            continue
        old_rg = e["regime"]
        new_rg = old_rg
        if old_rg == "recession_risk":
            if not (e["trend"] == "down" and e["pct_zone"] in ("drop", "crash")):
                new_rg = "risk_off"
        elif old_rg == "bull_pulling":
            sox_20d = _lookup_sox_20d(e["date"])
            if sox_20d is not None and sox_20d <= 0:
                new_rg = "bull_trending"
        remapped[new_rg][e["verdict"]].append(r5)
        remap_events.append({**e, "new_regime": new_rg})

    print(f"{'REGIME':<18} {'APPROVE':<28} {'HOLD':<28} {'CAUTION':<28}")
    print("-" * 105)
    for regime in sorted(set(list(remapped.keys()) + list(by_rg.keys()))):
        row = f"{regime:<18} "
        for verdict in ("APPROVE", "HOLD", "CAUTION"):
            xs = remapped.get(regime, {}).get(verdict, [])
            if not xs:
                row += f"{'—':<28} "
            else:
                avg = sum(xs) / len(xs)
                win = sum(1 for r in xs if r > 0) / len(xs) * 100
                row += f"n={len(xs):<3} avg={avg:>+6.2f}% win={win:>5.1f}%   "
        print(row)

    # 对比 recession_risk 桶新旧
    def _stat(xs):
        if not xs: return "n=0"
        avg = sum(xs) / len(xs)
        win = sum(1 for r in xs if r > 0) / len(xs) * 100
        return f"n={len(xs)}, avg={avg:+.2f}%, win={win:.1f}%"
    for bucket in ("recession_risk", "bull_pulling"):
        old = by_rg.get(bucket, {}).get("HOLD", [])
        new = remapped.get(bucket, {}).get("HOLD", [])
        old_c = by_rg.get(bucket, {}).get("CAUTION", [])
        new_c = remapped.get(bucket, {}).get("CAUTION", [])
        print(f"\n  {bucket} HOLD    旧: {_stat(old)}    新: {_stat(new)}")
        if old_c or new_c:
            print(f"  {bucket} CAUTION 旧: {_stat(old_c)}    新: {_stat(new_c)}")

    # ── 阶段 B3 模拟 ─────────────────────────────────────────────────────
    # B3: bull_trending/bull_extended regime + conf ≥ 3 → bypass gate (自动 APPROVE)
    # 用历史事件模拟: 会有多少信号被自动放行, 它们的 5d/10d 收益如何
    print("\n" + "=" * 90)
    print("【阶段 B3 模拟】 bull_trending/bull_extended + conf≥4 bypass gate")
    print("  阈值 4 是回测出的甜蜜点: conf≥3 太松 (win 42%), conf≥5 样本太小")
    print("=" * 90)
    bypass_events = [e for e in events
                     if e["regime"] in ("bull_trending", "bull_extended")
                     and e["confidence"] >= 4
                     and e.get("ret_5d") is not None]
    remain_events = [e for e in events
                     if not (e["regime"] in ("bull_trending", "bull_extended")
                             and e["confidence"] >= 4)
                     and e.get("ret_5d") is not None]
    bypass_5d = [e["ret_5d"] for e in bypass_events]
    remain_5d = [e["ret_5d"] for e in remain_events]
    if bypass_5d:
        print(f"  bypass (auto-APPROVE): {_stat(bypass_5d)}")
        # 按原 gate verdict 拆分, 看被 bypass 的信号原本会怎样
        by_verdict = defaultdict(list)
        for e in bypass_events:
            by_verdict[e["verdict"]].append(e["ret_5d"])
        for v, xs in by_verdict.items():
            print(f"    原被 gate 判 {v}: {_stat(xs)}")
    else:
        print(f"  bypass: n=0 (69天内无 bull_trending/bull_extended 高 conf 信号)")
    if remain_5d:
        print(f"  仍走 gate: {_stat(remain_5d)}")

    # 合并模拟: bypass 桶算入 APPROVE, 其余保持原判定
    print(f"\n  假设 gate CLI 保持当前行为不变, 只加 B3 bypass:")
    new_approve = bypass_5d
    new_hold = [e["ret_5d"] for e in remain_events if e["verdict"] == "HOLD"]
    new_caution = [e["ret_5d"] for e in remain_events if e["verdict"] == "CAUTION"]
    total = len(new_approve) + len(new_hold) + len(new_caution)
    if total:
        print(f"    APPROVE (bypass 新增): {_stat(new_approve)}  → 占 {len(new_approve)/total*100:.1f}%")
        print(f"    HOLD:                  {_stat(new_hold)}  → 占 {len(new_hold)/total*100:.1f}%")
        print(f"    CAUTION:               {_stat(new_caution)}  → 占 {len(new_caution)/total*100:.1f}%")

    # ── C2: alert 阈值 ──────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("【监控 alert】 (weekly 跑时若触发, 应人工回看 gate 行为)")
    print("=" * 90)
    all_events = [e for e in events if e.get("ret_5d") is not None]
    total = len(all_events)
    approve = sum(1 for e in all_events if e["verdict"] == "APPROVE")
    approve_rate = approve / total * 100 if total else 0
    hold_5d = [e["ret_5d"] for e in all_events if e["verdict"] == "HOLD"]
    hold_median = sorted(hold_5d)[len(hold_5d)//2] if hold_5d else 0
    print(f"  APPROVE 率: {approve_rate:.1f}%  (阈值: ≥ 5%)")
    print(f"  HOLD 桶 5d median: {hold_median:+.2f}%  (阈值: ≤ 0%)")
    if approve_rate < 5:
        print(f"  [ALERT] gate 过度否决 (APPROVE {approve_rate:.1f}% < 5%)")
    if hold_median > 0:
        print(f"  [ALERT] gate 挡下了赚钱信号 (HOLD median {hold_median:+.2f}% > 0)")
    if approve_rate >= 5 and hold_median <= 0:
        print(f"  ✓ gate 行为在健康范围")

    return {"events": len(events), "start": str(min(ts_list)),
            "end": str(max(ts_list)),
            "approve_rate": round(approve_rate, 1),
            "hold_5d_median": round(hold_median, 2)}


if __name__ == "__main__":
    run()
