"""强美元主线追踪 (2026-10-08 用户设定交易主线: 强美元, 可能抽干世界其他地区的流动性).

每天 (job usd_theme, 收盘后) 计算:
  · 美元强度: DXY 水平 / 20 日 / 60 日涨幅 + 在过去一年里的百分位
  · 扩散: USDJPY 及距 60 日高点回撤, EEM/FXI/EWJ 相对 SPY 的 20 日超额收益, 亚洲主要指数 5 日涨跌
  · 主线看错条件: 用 thesis_config.check_invalidation 检查当前 thesis 里的条件
写 signals/usd_theme.json, 供看板和 AI 提示词读取. 只读, 不下单.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "signals" / "usd_theme.json"
THEME = ("强美元: 美元走强可能把全球其他地区的流动性抽回美国 "
         "(资本回流美元资产, 非美市场与非美货币承压, 美元融资成本上升)")


def _closes(tk: str, period: str = "2y"):
    import yfinance as yf
    df = yf.Ticker(tk).history(period=period, interval="1d", auto_adjust=True)
    s = df["Close"].dropna()
    s.index = s.index.tz_localize(None) if s.index.tz else s.index
    return s


def _pct(s, n):
    return round(float(s.iloc[-1] / s.iloc[-1 - n] - 1) * 100, 2) if len(s) > n else None


def _rank(s, n, win=252):
    chg = s.pct_change(n).dropna()
    if len(chg) < win:
        return None
    w = chg.iloc[-win:]
    return round(float((w < w.iloc[-1]).mean()), 3)


def compute_metrics(fetch=_closes) -> dict:
    m: dict = {}
    dxy = fetch("DX-Y.NYB")
    m["dxy"] = round(float(dxy.iloc[-1]), 2)
    m["dxy_20d_pct"], m["dxy_60d_pct"] = _pct(dxy, 20), _pct(dxy, 60)
    m["dxy_20d_rank_1y"], m["dxy_60d_rank_1y"] = _rank(dxy, 20), _rank(dxy, 60)
    jpy = fetch("JPY=X")
    m["usdjpy"] = round(float(jpy.iloc[-1]), 2)
    m["usdjpy_20d_pct"] = _pct(jpy, 20)
    hi60 = float(jpy.iloc[-60:].max())
    m["usdjpy_pullback_from_60d_high_pct"] = round((float(jpy.iloc[-1]) / hi60 - 1) * 100, 2)
    spy = fetch("SPY")
    spy20 = _pct(spy, 20)
    for tk in ("EEM", "FXI", "EWJ"):
        try:
            r = _pct(fetch(tk), 20)
            m[f"{tk.lower()}_vs_spy_20d_pp"] = round(r - spy20, 2) if (r is not None and spy20 is not None) else None
        except Exception:
            m[f"{tk.lower()}_vs_spy_20d_pp"] = None
    asia = {}
    for key, sym in (("N225", "^N225"), ("HSI", "^HSI"), ("SSE", "000001.SS"), ("KOSPI", "^KS11")):
        try:
            asia[key] = _pct(fetch(sym, "3mo"), 5)
        except Exception:
            asia[key] = None
    m["asia_5d_pct"] = asia
    return m


def theme_strength(m: dict) -> str:
    """主线现在是否在兑现 (只作描述, 不进交易)."""
    r20, r60 = m.get("dxy_20d_rank_1y"), m.get("dxy_60d_rank_1y")
    drain = [v for v in (m.get("eem_vs_spy_20d_pp"), m.get("fxi_vs_spy_20d_pp")) if v is not None]
    if r20 is None:
        return "数据不足"
    if (r20 >= 0.9 or (r60 or 0) >= 0.8) and drain and min(drain) < 0:
        return "兑现中: 美元强势且非美市场跑输"
    if r20 >= 0.9 or (r60 or 0) >= 0.8:
        return "美元强势, 但非美市场尚未明显跑输"
    if (m.get("dxy_20d_pct") or 0) < 0:
        return "美元回落, 主线暂未兑现"
    return "中性"


def run(fetch=_closes) -> dict:
    m = compute_metrics(fetch)
    try:
        from thesis_config import check_invalidation, get_thesis_version
        triggered = check_invalidation(m)
        version = get_thesis_version()
    except Exception as e:  # noqa: BLE001
        triggered, version = [{"id": "check_failed", "description": str(e)}], None
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "theme": THEME,
           "thesis_version": version, "metrics": m, "strength": theme_strength(m),
           "invalidation_triggered": triggered}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def brief(max_age_h: float = 48) -> str:
    """给 AI 提示词用的一段主线说明 (读 signals/usd_theme.json; 过期则只给主线本身)."""
    line = f"【当前交易主线 (用户设定)】{THEME}。分析与建议请围绕这条主线展开, 但结论仍以数据为准。"
    try:
        d = json.loads(OUT.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600
        if age > max_age_h:
            return line + " (美元读数已过期)"
        m = d["metrics"]
        line += (f" 读数: DXY {m.get('dxy')} (20日 {m.get('dxy_20d_pct')}%, 一年百分位 {m.get('dxy_20d_rank_1y')}), "
                 f"USDJPY {m.get('usdjpy')}, EEM 相对 SPY 20日 {m.get('eem_vs_spy_20d_pp')}pp; 判断: {d.get('strength')}.")
        if d.get("invalidation_triggered"):
            line += " ⚠ 主线看错条件已触发: " + "; ".join(t.get("id", "") for t in d["invalidation_triggered"])
    except Exception:
        pass
    return line


if __name__ == "__main__":
    res = run()
    print(json.dumps({k: v for k, v in res.items() if k != "theme"}, ensure_ascii=False, indent=1))
    sys.exit(0)
