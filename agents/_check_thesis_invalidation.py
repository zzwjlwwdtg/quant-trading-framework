"""_check_thesis_invalidation.py — 每日检查 thesis 是否被 macro 数据 invalidate

流程:
    1. 拉 FRED CPI (CPIAUCSL) → 算 MoM %
    2. (未来: 拉 GOOGL/AMZN 最新财报 capex — 目前 manual, log 提醒)
    3. 交给 thesis_config.check_invalidation(macro) 判是否触发
    4. 若触发 → append signals/thesis_invalidation_log.jsonl + 发 notification
    5. 若 auto_clear_blacklist_on_invalidation=true → 清空 blacklist (默认 False, 需人工)

设计不动 blacklist (默认 False), 是为了避免 CPI 单月热就把 thesis 全清空.
让人工看到 alert 再决定. 记 log 是关键 — 系统 memory 会用它做归因.

CLI: python _check_thesis_invalidation.py  (weekly.bat / orchestrator daily 调)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from config import FRED_API_KEY, SIGNALS_DIR
from atomic_io import append_jsonl
from thesis_config import check_invalidation, summary as thesis_summary

_LOG_PATH = Path(SIGNALS_DIR) / "thesis_invalidation_log.jsonl"


def _fetch_cpi_mom() -> float | None:
    """FRED CPIAUCSL 最近 2 个月 MoM %."""
    if not FRED_API_KEY:
        print("  [cpi] FRED_API_KEY 未设, 跳过 CPI 拉取")
        return None
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id=CPIAUCSL&api_key={FRED_API_KEY}&file_type=json"
           f"&sort_order=desc&limit=3")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as ex:
        print(f"  [cpi] FRED 拉取失败: {ex}")
        return None
    obs = data.get("observations", [])
    valid = [o for o in obs if o.get("value") not in ("", ".", None)]
    if len(valid) < 2:
        return None
    try:
        curr = float(valid[0]["value"])
        prev = float(valid[1]["value"])
        mom_pct = (curr / prev - 1) * 100
        print(f"  [cpi] CPIAUCSL: {valid[0]['date']}={curr:.3f}, {valid[1]['date']}={prev:.3f}, MoM={mom_pct:+.3f}%")
        return round(mom_pct, 3)
    except Exception:
        return None


def _fetch_googl_amzn_capex_upgraded() -> bool | None:
    """检查最新 GOOGL/AMZN capex 是否上调.
    暂 stub: 需读财报 API 或人工 flag. 返 None (unknown)."""
    # TODO: 将来接 SEC EDGAR / Yahoo Finance 财报 API 自动化
    return None


def main():
    print("=" * 70)
    print(f"Thesis Invalidation Check @ {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 70)

    ts = thesis_summary()
    if not ts.get("ok"):
        print("  ! thesis_config 缺失, 退出")
        return
    print(f"\ncurrent thesis: {ts['version']}  (last reviewed {ts['last_reviewed_at']})")
    print(f"blacklist: {ts['blacklist_count']} tickers · whitelist: {ts['whitelist_count']} · invalidation rules: {ts['invalidation_count']}")

    # 拉 macro
    macro = {}
    cpi_mom = _fetch_cpi_mom()
    if cpi_mom is not None:
        macro["cpi_mom_pct"] = cpi_mom
    capex_up = _fetch_googl_amzn_capex_upgraded()
    if capex_up is not None:
        macro["googl_amzn_capex_ttm_upgraded"] = capex_up

    # 检查
    print("\n检查 invalidation conditions...")
    triggered = check_invalidation(macro)
    if not triggered:
        print("  ✓ 无触发. thesis 保持有效.")
        return

    print(f"\n⚠ 触发 {len(triggered)} 个 invalidation condition:")
    for t in triggered:
        print(f"  [{t['id']}] {t['metric']} {t['operator']} {t['threshold']}: actual={t['actual']}")
        print(f"       {t['description']}")

    # log
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "thesis_version": ts["version"],
        "triggered": triggered,
        "macro": macro,
    }
    try:
        append_jsonl(_LOG_PATH, entry)
        print(f"\n[log] 已写 {_LOG_PATH.name}")
    except Exception as ex:
        print(f"[log] 写失败: {ex}")

    # notify (best-effort)
    try:
        from notifications import send_alert
        msg = (f"⚠ **THESIS INVALIDATION TRIGGERED** ({ts['version']})\n"
               f"触发条件: {', '.join(t['id'] for t in triggered)}\n"
               f"人工 review: 是否更新 thesis_config.json 或清 blacklist")
        r = send_alert(msg, level="crisis", dedup=True)
        print(f"[notify] sent: {r.get('sent', [])}")
    except Exception as ex:
        print(f"[notify] 失败: {ex}")

    print("\n**行动**: 打开 signals/thesis_config.json 决定是否 (a) 更新 blacklist "
          "(b) 改 version (c) 忽略并等下次数据")


if __name__ == "__main__":
    main()
