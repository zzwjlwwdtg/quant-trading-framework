"""
_backtest_missed_signals.py
───────────────────────────
最近几天信号 vs 实际价格：conf_min bug（TECH_ONLY 下 conf 5/5 < min 6 被静默跳过）
让我们错过了几个买/卖点？如果放行，实际盈亏如何？

方法：
  1. 从日志抓最近 N 天的 [ticker] 信号 + 置信度 + regime
  2. 用 yfinance 拉当日+后续价格
  3. 假设"如果没被 bug 挡住"会在信号触发日以当日收盘价开仓/加仓
  4. 计算 1d / 3d / 5d 实际收益
  5. 汇总：漏了多少 alpha
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import yfinance as yf
import pandas as pd

LOG_DIR = Path(__file__).parent / "logs"


def parse_recent_signals(days_back: int = 5) -> pd.DataFrame:
    """扫最近 N 天日志，抓每个 ticker 的信号 + 置信度。"""
    rows = []
    signal_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+"
        r"信号: (?P<icon>\S+)\s+(?P<action>\S+)\s+"
        r"置信度:(?P<conf>\d+)/(?P<scale>\d+)\s+"
        r"依据:(?P<reason>[^行]+)\s+"
        r"行情:(?P<regime>\S+)"
    )
    ticker_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+【(\w+)】")

    today = datetime.now().date()
    for d in range(days_back):
        date = today - timedelta(days=d)
        log_path = LOG_DIR / f"run_{date.strftime('%Y%m%d')}.log"
        if not log_path.exists():
            continue
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        last_ticker = None
        for line in lines:
            m_tk = ticker_re.match(line)
            if m_tk:
                last_ticker = m_tk.group(1)
                continue
            m_sig = signal_re.match(line)
            if m_sig and last_ticker:
                action_str = m_sig.group("action")
                # 解析 action 中文 → 英文
                action_map = {
                    "关注买入": "WATCH_BUY", "买入": "BUY",
                    "卖出": "SELL", "减仓": "REDUCE",
                    "警示": "CAUTION", "持仓观望": "HOLD",
                }
                action = action_map.get(action_str, action_str)
                rows.append({
                    "date":   m_sig.group(1),
                    "time":   m_sig.group(2),
                    "ticker": last_ticker,
                    "action": action,
                    "conf":   int(m_sig.group("conf")),
                    "scale":  int(m_sig.group("scale")),
                    "regime": m_sig.group("regime"),
                    "reason": m_sig.group("reason").strip()[:40],
                })
                last_ticker = None  # 一个 [ticker] 块只对应一个 signal
    return pd.DataFrame(rows).sort_values(["ticker", "date", "time"])


def evaluate_forward(df_signals: pd.DataFrame) -> pd.DataFrame:
    """对每个信号，拉 yfinance 价格算后续 1d/3d/5d 实际收益。"""
    out = []
    for tk, grp in df_signals.groupby("ticker"):
        try:
            hist = yf.Ticker(tk).history(period="30d", interval="1d", auto_adjust=True)
        except Exception:
            continue
        if hist.empty:
            continue
        hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index

        for _, row in grp.iterrows():
            sig_date = pd.to_datetime(row["date"]).date()
            # 找信号日或紧邻的下一个交易日
            avail = hist.index[hist.index.date >= sig_date]
            if len(avail) == 0:
                continue
            entry_dt = avail[0]
            entry_px = float(hist.loc[entry_dt, "Close"])
            fwd = {}
            for h in [1, 3, 5]:
                fwd_dates = hist.index[hist.index > entry_dt][:h]
                if len(fwd_dates) < h:
                    fwd[h] = None
                else:
                    exit_px = float(hist.loc[fwd_dates[-1], "Close"])
                    fwd[h] = (exit_px - entry_px) / entry_px * 100
            out.append({
                **row.to_dict(),
                "entry_dt":  entry_dt.strftime("%Y-%m-%d"),
                "entry_px":  round(entry_px, 2),
                "fwd_1d":    round(fwd[1], 2) if fwd[1] is not None else None,
                "fwd_3d":    round(fwd[3], 2) if fwd[3] is not None else None,
                "fwd_5d":    round(fwd[5], 2) if fwd[5] is not None else None,
            })
    return pd.DataFrame(out)


def main():
    print("=" * 100)
    print("  最近信号 vs 实际价格（bug 修复前那几天被静默跳过的信号）")
    print("=" * 100)
    df = parse_recent_signals(days_back=7)
    if df.empty:
        print("❌ 没抓到信号")
        return 1
    print(f"\n最近 7 天日志抓到 {len(df)} 个 [ticker] 信号\n")

    result = evaluate_forward(df)
    if result.empty:
        print("❌ 无法拉价格")
        return 1

    # 只看 BUY 类信号（bug 时段实际应下未下的）
    buys = result[result["action"].isin(["WATCH_BUY", "BUY"])].copy()
    print(f"\n{'─'*100}")
    print(f"  BUY 类信号（{len(buys)} 个）— 若 bug 未挡，会以这些价开仓")
    print(f"{'─'*100}")
    cols = ["date","time","ticker","action","conf","scale","regime","entry_px","fwd_1d","fwd_3d","fwd_5d"]
    print(buys[cols].to_string(index=False))

    # 汇总：漏了多少收益
    print(f"\n{'='*100}")
    print("  漏掉的机会（若信号触发日开仓，1d/3d/5d 平均收益）")
    print(f"{'='*100}")
    for h_col in ["fwd_1d", "fwd_3d", "fwd_5d"]:
        sub = buys[buys[h_col].notna()]
        if sub.empty: continue
        avg = sub[h_col].mean()
        up  = (sub[h_col] > 0).mean() * 100
        print(f"  {h_col}: 平均 {avg:+.2f}%   上涨率 {up:.0f}%  ({len(sub)} 个样本)")

    # 分标的
    print(f"\n{'─'*100}")
    print("  分标的：")
    print(f"{'─'*100}")
    by_tk = buys.groupby("ticker").agg(
        n=("action", "count"),
        avg_1d=("fwd_1d", "mean"),
        avg_5d=("fwd_5d", "mean"),
    ).round(2)
    print(by_tk.to_string())

    # SELL 信号（如果漏了平仓）
    sells = result[result["action"].isin(["SELL", "REDUCE"])].copy()
    if not sells.empty:
        print(f"\n{'─'*100}")
        print(f"  SELL/REDUCE 信号（{len(sells)} 个）— 若 bug 未挡，会以这些价平仓")
        print(f"{'─'*100}")
        print(sells[cols].to_string(index=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
