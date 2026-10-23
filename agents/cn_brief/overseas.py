"""Compare the two last completed source sessions, NOT a predicted A-share opening gap.

The former domestic-date anchor could mix two days and reverse yield changes.
Old research constants remain for historical scripts, not the current forecast.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
DRIVERS = {"^GSPC": "标普500", "^IXIC": "纳指", "^HXC": "中概金龙", "CNH=X": "美元/人民币",
           "^TNX": "美10年期", "DX-Y.NYB": "美元指数"}
LEVEL_DIFF = {"^TNX"}
A_PROXY = "510300.SS"
# Yahoo 上 ^HXC / CNH=X 历史很短或缺失 → 按顺序用备用代码 (回测脚本用同一张表, 口径一致)
ALTERNATES = {"^HXC": ["KWEB"], "CNH=X": ["USDCNH=X", "CNY=X"]}
VERDICT = Path(__file__).resolve().parents[1] / "signals" / "backtest_verdicts" / "overnight_ashare.json"


def _hist(tk: str, days: int = 20):
    import yfinance as yf
    import pandas as pd
    df = yf.Ticker(tk).history(period=f"{days}d", auto_adjust=True)
    if df is None or df.empty or "Close" not in df:
        return pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    return df[["Close"]].dropna()


def overnight(today_bjt: date | None = None) -> dict:
    import pandas as pd
    today_bjt = today_bjt or datetime.now(BJT).date()
    out = {"items": [], "errors": [], "last_a_close": None,
           "change_scope": "PREVIOUS_COMPLETED_SOURCE_SESSION_NOT_A_CLOSE"}
    for tk, name in DRIVERS.items():
        try:
            d, used = None, tk
            alternatives = ["USDCNH=X"] if tk == "CNH=X" else ALTERNATES.get(tk, [])
            for cand in [tk] + alternatives:
                d = _hist(cand)
                if len(d) >= 2:
                    used = cand
                    break
            c = d["Close"]
            now_s = c[c.index < pd.Timestamp(today_bjt)]
            if len(now_s) < 2 or now_s.index.has_duplicates or not now_s.index.is_monotonic_increasing:
                out["errors"].append(f"{name}: completed session pair unavailable")
                continue
            c1, c0 = float(now_s.iloc[-1]), float(now_s.iloc[-2])
            if not all(math.isfinite(x) and x > 0 for x in (c0,c1)):
                out["errors"].append(f"{name}: invalid completed prices")
                continue
            chg = (c1 - c0) if tk in LEVEL_DIFF else (c1 / c0 - 1)
            label = name if used == tk else "中概互联网ETF代理(KWEB)" if used == "KWEB" else f"{name}({used})"
            out["items"].append({"ticker": tk, "name": label, "symbol": used, "level": round(c1, 4), "change": chg,
                                 "base_as_of": str(now_s.index[-2].date()), "is_proxy": used != tk,
                                 "as_of": str(now_s.index[-1].date()), "kind": "diff" if tk in LEVEL_DIFF else "pct"})
        except Exception as e:      # noqa: BLE001
            out["errors"].append(f"{name}: {e}")
    return out


def fmt_item(it: dict) -> str:
    if it["kind"] == "diff":                        # ^TNX 报价单位是 %, 差值 ×100 = bp
        return f"{it['name']} {it['level']:.2f}%（{it['change'] * 100:+.0f}bp）"
    if it["ticker"] == "CNH=X":                     # 美元兑人民币, 数字变大 = 人民币走弱
        return f"{it['name']} {it['level']:.4f}（{it['change'] * 100:+.2f}%）"
    return f"{it['name']} {it['change'] * 100:+.2f}%"


def load_verdict() -> dict:
    try:
        return json.loads(VERDICT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def implied_open(items: list[dict], verdict: dict | None = None, target: str = A_PROXY) -> dict | None:
    """Retired: legacy coefficients do not validate the new observation basis or 1/3/5-day forecasts."""
    return None


def fade_note(verdict: dict | None = None, target: str = A_PROXY) -> str | None:
    """跳空后日内延续还是回落: 两期同号且 |t_r|≥2 才说."""
    verdict = verdict if verdict is not None else load_verdict()
    f = ((verdict.get("metrics") or {}).get("gap_vs_intraday") or {}).get(target) or {}
    tr, oo = f.get("train") or {}, f.get("oos") or {}
    if all(k in tr and k in oo for k in ("r", "t_r")) and (tr["r"] > 0) == (oo["r"] > 0) \
            and abs(tr["t_r"]) >= 2 and abs(oo["t_r"]) >= 2:
        return ("历史上高开后日内倾向继续走高、低开后倾向继续走低" if oo["r"] > 0
                else "历史上高开后日内倾向回落、低开后倾向回升（别追开盘）") + f"（样本外 r={oo['r']}）"
    return None
