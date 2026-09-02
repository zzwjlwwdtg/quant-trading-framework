"""Claude pre-trade gate.

The rule engine still proposes the trade. Claude is only allowed to approve,
hold, or downgrade the proposed action before paper_trader.execute() can place
an order.
"""
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from config import SIGNALS_DIR, is_sim_active_trading
from notifier import logger
from trading_contracts import (
    BUY_ACTIONS,
    ORDER_ACTIONS,
    TRADE_WINDOWS,
    confidence_min,
    extended_chase_signals,
)


ALLOWED_VERDICTS = {"APPROVE", "HOLD", "CAUTION"}


def _enabled() -> bool:
    return os.environ.get("CLAUDE_DECISION_GATE", "0") == "1"


def _timeout_sec() -> int:
    try:
        return int(os.environ.get("CLAUDE_DECISION_TIMEOUT_SEC", "180"))
    except ValueError:
        return 180


def _fail_closed() -> bool:
    return os.environ.get("CLAUDE_DECISION_FAIL_CLOSED", "1") != "0"


def _current_conf_scale() -> int:
    try:
        from decision_agent import _conf_scale
        return _conf_scale()
    except Exception:
        return 10


def _clip_text(value: str, limit: int = 600) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "..."


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:80]:
            if str(k).lower() in {"raw", "history", "df", "dataframe"}:
                continue
            out[str(k)] = _jsonable(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:20]]
    try:
        return float(value)
    except Exception:
        return str(value)


def _compact_events(events: dict | None) -> dict:
    src = dict(events or {})
    headlines = src.get("top_headlines")
    if isinstance(headlines, list):
        src["top_headlines"] = [
            {
                "title": h.get("title"),
                "url": h.get("url"),
                "source": h.get("source"),
            }
            for h in headlines
            if isinstance(h, dict)
        ][:3]
    return _jsonable(src)


def _prompt(
    ticker: str,
    market: dict | None,
    events: dict | None,
    decision: dict,
    macro: dict | None,
    window: str,
) -> str:
    payload = {
        "timestamp_local": datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "window": window,
        "market": _jsonable(market or {}),
        "events": _compact_events(events),
        "macro": _jsonable(macro or {}),
        "rule_decision": _jsonable(decision),
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""You are the final pre-trade risk gate for this local trading system.

Task:
- Review ONLY the rule_decision below. Do not create a new trade idea.
- Apply BALANCED judgment. The rule engine has already filtered for confluence;
  your job is to catch late-breaking risk, not to second-guess every weak signal.

Verdict rubric:
- APPROVE — signal is internally consistent AND no immediate risk flag applies.
  Explicitly acceptable: rule confidence 3-5 in bull_trending/bull_pulling/bull_extended
  regime; WATCH_BUY in neutral without conflicting indicators; probe-size buys.
- HOLD — hard blockers only: stale data (age > 30 min), contradictory indicators
  (rule says BUY but M15 death cross + daily bear cross), falling knife (>10% down
  in 3 days with no support level nearby), earnings T-1/T-0, RSI >85 on leveraged
  ETF, or crisis regime with pct_chg < -3%.
- CAUTION — signal is notable but should be recorded as no-order (rare; use HOLD
  or APPROVE preferentially).

Historical calibration (69-day backtest, 336 gate decisions):
- Prior version of this prompt returned APPROVE=0/336 (100% veto). That is over-conservative.
- Target APPROVE rate: 20-40% for consistent WATCH_BUY signals.
- Target HOLD rate: reserved for actual risk (~40%).

Output JSON only, with this schema:
{{"verdict":"APPROVE|HOLD|CAUTION","confidence":1-10,"reason":"short reason","risk_flags":["..."]}}

Decision packet:
{data}
"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    candidates = [text]
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if match:
        candidates.insert(0, match.group(1))
    match = re.search(r"(\{.*\})", text, re.S)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def _audit(
    provider: str,
    verdict: str,
    status: str,
    reason: str,
    confidence: Any = None,
    risk_flags: Any = None,
    prompt_path: str | None = None,
    raw_path: str | None = None,
) -> dict:
    return {
        "provider": provider,
        "verdict": verdict,
        "status": status,
        "reason": _clip_text(str(reason or ""), 500),
        "confidence": confidence,
        "risk_flags": risk_flags if isinstance(risk_flags, list) else [],
        "prompt_path": prompt_path,
        "raw_path": raw_path,
    }


def _demote(decision: dict, verdict: str, audit: dict) -> dict:
    out = deepcopy(decision)
    original_action = out.get("action")
    out["action"] = verdict
    out["demoted_from"] = original_action
    out["reason"] = f"claude_gate_{verdict.lower()}: {audit.get('reason') or audit.get('status')}"
    out["claude_gate"] = audit | {"demoted_from": original_action}
    return out


def _sim_active_probe(decision: dict, market: dict | None, audit: dict) -> dict | None:
    """Convert an AI-vetoed simulated BUY into a bounded probe when safe enough.

    This never creates a new direction: the deterministic rule engine must already
    have proposed a BUY action. A valid broker stop below the current price is
    mandatory, so stale/miswired stop data remains fail-closed.
    """
    if not is_sim_active_trading() or (decision or {}).get("action") not in BUY_ACTIONS:
        return None
    try:
        price = float((market or {}).get("price") or 0)
        stop = float((decision or {}).get("stop_ref") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or stop <= 0 or stop >= price:
        return None
    stop_distance = (price - stop) / price
    if stop_distance > 0.18:
        return None
    chase_signals = extended_chase_signals(market)
    if len(chase_signals) >= 2:
        logger.warning(
            "[claude-gate] SIM_ACTIVE probe blocked: extended BUY chase "
            f"({', '.join(chase_signals)})"
        )
        return None

    out = deepcopy(decision)
    original_action = out.get("action")
    out["action"] = "WATCH_BUY_PROBE"
    out["demoted_from"] = original_action
    out["reason"] = (
        "sim_active_probe_after_claude_veto: "
        f"{audit.get('reason') or audit.get('status')}"
    )
    out["claude_gate"] = audit | {
        "demoted_from": original_action,
        "execution_override": "SIM_ACTIVE_PROBE",
        "stop_distance_pct": round(stop_distance * 100, 2),
    }
    logger.info(
        f"[claude-gate] SIM_ACTIVE {out.get('action')} {original_action}→probe "
        f"stop={stop:.2f} risk={stop_distance*100:.1f}%"
    )
    return out


def _fail_decision(
    decision: dict,
    status: str,
    prompt_path: str | None = None,
    provider: str = "Codex",
) -> dict:
    audit = _audit(
        provider,
        "HOLD",
        status,
        "AI gate unavailable; fail-closed to no-order.",
        prompt_path=prompt_path,
    )
    if _fail_closed():
        return _demote(decision, "HOLD", audit)
    out = deepcopy(decision)
    out["claude_gate"] = audit | {"verdict": "BYPASS_FAIL_OPEN"}
    return out


def apply_claude_gate(
    ticker: str,
    market: dict | None,
    events: dict | None,
    decision: dict,
    macro: dict | None,
    window: str | None,
) -> dict:
    """Return the final decision after AI CLI approval, if the gate applies.

    The function and persisted ``claude_gate`` key retain their legacy names for
    backward compatibility; provider routing is handled centrally by ai_prompt.
    """
    if not _enabled():
        return decision
    if window not in TRADE_WINDOWS:
        return decision
    if (decision or {}).get("action") not in ORDER_ACTIONS:
        return decision
    try:
        conf = int((decision or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0
    if conf < confidence_min(window, _current_conf_scale()):
        return decision

    # Bull regime bypass: 只在 bull_trending/bull_extended + conf ≥ 4 时自动 APPROVE.
    # 回测证据 (69 天 336 事件):
    #   bull_trending conf ≥ 3: n=24 avg -2.59%   win 41.7%  ← 阈值太松, 无效
    #   bull_trending conf ≥ 4: n=10 avg +0.10%   win 80.0%  ← ★ 甜蜜点
    #   bull_trending conf ≥ 5: n=6  avg -0.52%   win 83.3%  ← 样本略小
    # 4 是分水岭: rule engine 的 conf 4+ 在 bull regime 下 5d 胜率 80%,
    # gate 走 CLI 只会引入延迟和错拒. bull_pulling 单独排除: HOLD 100% 正确.
    regime = (decision or {}).get("regime", "")
    if regime in ("bull_trending", "bull_extended") and conf >= 4:
        approved = deepcopy(decision)
        approved["claude_gate"] = {
            "verdict": "APPROVE",
            "provider": "regime_bypass",
            "reason": f"bull regime bypass: {regime} + conf {conf} ≥ 4",
            "status": "bypass_ok",
        }
        return approved

    out_dir = Path(SIGNALS_DIR)
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ticker = ticker.replace(".", "-")
    prompt_text = _prompt(ticker, market, events, decision, macro, str(window))
    prompt_path = out_dir / f"claude_gate_prompt_{safe_ticker}_{stamp}.md"
    raw_path = out_dir / f"claude_gate_raw_{safe_ticker}_{stamp}.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    try:
        from ai_prompt import query_ai_cli
    except Exception as exc:
        logger.error(f"[claude-gate] import failed for {ticker}: {exc}")
        return _fail_decision(decision, f"import_error: {exc}", str(prompt_path))

    # 每 trade 都调, 打市场决策级 → medium (需 regime+confluence+risk 联动推理)
    output, status, provider, _ = query_ai_cli(prompt_text, timeout=_timeout_sec(),
                                                complexity="medium")

    if not output:
        logger.warning(f"[claude-gate] {ticker} unavailable: {status}")
        return _fail_decision(decision, status, str(prompt_path), provider)

    raw_path.write_text(output + "\n", encoding="utf-8")
    parsed = _extract_json(output)
    if not parsed:
        logger.warning(f"[claude-gate] {ticker} invalid JSON; raw={raw_path}")
        return _fail_decision(decision, "invalid_json", str(prompt_path))

    verdict = str(parsed.get("verdict", "")).upper().strip()
    if verdict not in ALLOWED_VERDICTS:
        logger.warning(f"[claude-gate] {ticker} invalid verdict={verdict!r}")
        return _fail_decision(decision, f"invalid_verdict: {verdict}", str(prompt_path))

    audit = _audit(
        provider=provider,
        verdict=verdict,
        status=status,
        reason=str(parsed.get("reason") or ""),
        confidence=parsed.get("confidence"),
        risk_flags=parsed.get("risk_flags"),
        prompt_path=str(prompt_path),
        raw_path=str(raw_path),
    )

    if verdict == "APPROVE":
        approved = deepcopy(decision)
        approved["claude_gate"] = audit
        logger.info(f"[claude-gate] {ticker} APPROVE: {audit['reason']}")
        return approved

    logger.info(f"[claude-gate] {ticker} {verdict}: {audit['reason']}")
    active_probe = _sim_active_probe(decision, market, audit)
    return active_probe if active_probe is not None else _demote(decision, verdict, audit)
