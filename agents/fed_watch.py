"""fed_watch.py — CME FedWatch 加息/降息预期抓取 via Claude CLI + WebFetch.

CME FedWatch Tool 显示市场对未来 FOMC 会议 rate action 的概率分布，
数据来自 Fed Fund Futures 反推。JS 后台 AJAX 数据难直接爬，用 Claude
CLI 的 WebFetch 抓页面文本 → 解析成结构化 JSON。

缓存 2h（rate expectations 变化慢）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional


CME_FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"


def _fetch_fed_watch_via_claude() -> Optional[dict]:
    """Claude CLI + WebFetch 抓 CME FedWatch, 解析成结构化 JSON."""
    try:
        from ai_prompt import query_claude_cli, query_codex_cli, _is_claude_quota_status
    except Exception:
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""你是宏观金融数据抓取员。任务：从 CME FedWatch Tool 提取市场对未来 3-4 次 FOMC 会议的联邦基金利率行动概率。

**今天日期: {today_str}**（数据必须在此日期或最近 1 交易日之内，否则视为 stale）。

数据源:
  官方: {CME_FEDWATCH_URL}
  备选: fedfundfutures.com / bloomberg.com/quote/fedfundfutures / cnbc.com/quotes/@FF.1 的最新报告

要求：
1. 使用 WebFetch 或 WebSearch 拉最新数据 —— **必须**明确写出数据 snapshot 日期
2. **鉴别关键近期事件是否已反映**：查最近 7 天内是否有 NFP / CPI / PPI / FOMC / Powell 讲话，若有则数据必须在该事件**之后**发布
3. 若能证明数据 >2 天旧且期间发生过就业/通胀数据 → 返 confidence="low" 并在 commentary 里明确 "数据滞后" 警告
4. 提取未来 3-4 次 FOMC meeting 日期 + 每种 rate action 的市场隐含概率
5. Actions 用简化标签：
   · "hike_25" (加 25bps) / "hike_50" (加 50bps)
   · "hold" (维持)
   · "cut_25" (降 25bps) / "cut_50" (降 50bps)
6. 概率总和每个 meeting 应该 ≈100%
7. 附带隐含终点利率（terminal rate）如果能找到
8. commentary 里必须写出 data snapshot 的实际日期 + 若有近期事件（NFP/CPI）说明其对预期的影响

严格 JSON 输出（无 markdown 围栏）：
{{
  "current_target_range": "4.00-4.25",
  "meetings": [
    {{
      "date": "2026-09-17",
      "days_until": 42,
      "probabilities": {{
        "hold": 68,
        "cut_25": 30,
        "hike_25": 2
      }},
      "most_likely": "hold",
      "consensus_direction": "dovish_lean"
    }}
  ],
  "implied_terminal_rate": "3.25-3.50",
  "implied_terminal_date": "2027-Q1",
  "data_snapshot_date": "2026-08-08",
  "recent_event_impact": "8/7 NFP +73K (低于预期 100K) → 降息预期 +8pp",
  "commentary": "1-2 sentence 市场预期总结，**必须**引用最近 NFP/CPI/PPI 数据对预期的影响",
  "sources": [
    {{"url": "...", "type": "cme_fedwatch"}}
  ],
  "confidence": "high" | "medium" | "low"
}}

如果找不到最新数据，返 {{"error": "no_recent_data", "confidence": "low"}}
"""
    to = int(os.environ.get("FED_WATCH_TIMEOUT", "180"))
    provider = "claude"
    out, status = query_claude_cli(prompt, timeout=to)
    if not out and _is_claude_quota_status(status):
        provider = "codex"
        out, status = query_codex_cli(prompt, timeout=to)
    if not out:
        return {"error": f"{provider}_{status[:120]}"}

    # 解析 JSON
    txt = out.strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```\s*$", "", txt)
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        txt = m.group(0)
    try:
        data = json.loads(txt)
    except Exception as e:
        return {"error": f"parse_{str(e)[:80]}", "raw_head": out[:200]}
    if not isinstance(data, dict):
        return {"error": "not_dict"}
    data["source_via"] = f"{provider}_cli_websearch"
    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


def get_fed_watch() -> dict:
    """入口。可选 env: FED_WATCH_ENABLED=0 完全关掉。"""
    if os.environ.get("FED_WATCH_ENABLED", "1") == "0":
        return {"error": "disabled_by_env", "confidence": "unknown"}
    result = _fetch_fed_watch_via_claude()
    if not result:
        return {"error": "cli_unavailable"}
    return result


if __name__ == "__main__":
    print(json.dumps(get_fed_watch(), ensure_ascii=False, indent=2, default=str))
