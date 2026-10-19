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
from datetime import datetime, timezone
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

# ── EVO-20261015-1: AI Gate 故障分类与健康状态 ─────────────────────────
# 基础设施不可用必须和"模型给出 HOLD"分开记录, 否则 dashboard / 复盘会把
# 服务故障误读成高置信度观望. 这里统一一个只读的分类器, _ai_cli_diag 与
# notifications 共享, 保证日志与告警用同一套标签.
FAILURE_CLASSES = ("quota", "protocol", "network", "unavailable", "unknown")

HEALTH_STATE_PATH = Path(SIGNALS_DIR) / "ai_gate_health.json"


def classify_cli_failure(status: str) -> str:
    """把 ai_prompt 返回的 status 字符串归一到故障分类之一.

    返回 FAILURE_CLASSES 之一: quota / protocol / network / unavailable / unknown.
    空输入或完全未知的 status → "unknown"; 不抛异常, 调用方可安全存盘.
    """
    s = (status or "").lower()
    if not s:
        return "unknown"
    # 配额 / 账户限额 — 复用 ai_prompt._is_claude_quota_status 的关键词
    quota_kw = (
        "quota", "usage limit", "limit reached", "rate limit", "rate_limit",
        "too many requests", "429", "insufficient_quota", "insufficient credits",
        "credit balance", "billing", "exceeded", "hit your limit", "your limit",
        "resets",
    )
    if any(k in s for k in quota_kw):
        return "quota"
    # 协议 / 解码: JSON 解析失败、verdict 字段非法、codex 模型 cache 结构不兼容
    protocol_kw = (
        "invalid_json", "invalid_verdict", "models cache", "unknown variant",
        "could not parse", "parse error", "decoding", "protocol",
        "unexpected eof", "schema",
    )
    if any(k in s for k in protocol_kw):
        return "protocol"
    # 网络 / 超时 / 连接问题
    network_kw = (
        "timeout", "timed out", "connection", "connect error", "connrefused",
        "dns", "network", "unreachable", "handshake", "tls", "ssl",
        "econnreset", "socket", "resolve",
    )
    if any(k in s for k in network_kw):
        return "network"
    # CLI 未安装 / 二进制找不到 / import 失败 — 直接当 unavailable, 不是模型意见
    if "not_installed" in s or "no such file" in s or "import_error" in s or "import error" in s:
        return "unavailable"
    return "unknown"


def _channel_name(provider: str) -> str:
    p = (provider or "").strip().lower()
    if "claude" in p:
        return "claude"
    if "codex" in p:
        return "codex"
    return p or "unknown"


def _load_health_state() -> dict:
    try:
        if HEALTH_STATE_PATH.exists():
            return json.loads(HEALTH_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_health_state(state: dict) -> None:
    try:
        HEALTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _recompute_overall(state: dict) -> None:
    """根据各 channel 当前 status 更新 overall 字段.

    双通道都 ok → ok; 都不可用 → down; 单边故障 → degraded; 全未知 → unknown.
    """
    channels = state.get("channels") or {}
    statuses = [c.get("status") for c in channels.values() if isinstance(c, dict)]
    if not statuses:
        state["overall"] = "unknown"
    elif all(s == "ok" for s in statuses):
        state["overall"] = "ok"
    elif all(s == "unavailable" for s in statuses):
        state["overall"] = "down"
    else:
        state["overall"] = "degraded"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_since(iso_ts: str | None, now: datetime | None = None) -> int:
    if not iso_ts:
        return 0
    try:
        start = datetime.fromisoformat(iso_ts)
    except Exception:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - start).total_seconds()))


def record_cli_failure(provider: str, status: str, failure_class: str | None = None,
                       *, now: datetime | None = None) -> dict:
    """把一次 CLI 失败落到 signals/ai_gate_health.json.

    返回包含: channel, failure_class, duration_s, is_new_failure, previous_status.
    "新故障" = 通道之前不是 unavailable, 或 class 变了 — 用于触发告警 (非新则合并).
    """
    failure_class = failure_class or classify_cli_failure(status)
    state = _load_health_state()
    channels = state.setdefault("channels", {})
    channel = _channel_name(provider)
    ch = channels.setdefault(channel, {})
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    prev_status = ch.get("status", "unknown")
    prev_class = ch.get("failure_class")
    is_new = prev_status != "unavailable" or prev_class != failure_class
    if is_new:
        ch["since"] = now_iso
        ch["failure_count"] = 1
    else:
        ch["failure_count"] = int(ch.get("failure_count", 0)) + 1

    ch["status"] = "unavailable"
    ch["failure_class"] = failure_class
    ch["last_failure_status"] = str(status or "")[:300]
    ch["last_failure_at"] = now_iso

    duration_s = _duration_since(ch.get("since"), now=now)
    _recompute_overall(state)
    state["updated_at"] = now_iso
    _save_health_state(state)

    return {
        "channel": channel,
        "failure_class": failure_class,
        "duration_s": duration_s,
        "is_new_failure": is_new,
        "previous_status": prev_status,
        "overall": state.get("overall"),
    }


def record_cli_success(provider: str, *, probe: bool = False,
                        now: datetime | None = None) -> dict:
    """标记一个 channel 当前可用. probe=True 表示来自 _ai_cli_diag 的实际成功探测.

    恢复必须有探测证据 — 普通业务路径调用 (probe=False) 只刷新 last_ok, 不发恢复告警;
    _ai_cli_diag 显式 probe=True 时, 若之前是 unavailable 则返回 recovered=True, 由
    调用方发出 recovery 告警 (单源, 不会被日常业务请求伪造).
    """
    state = _load_health_state()
    channels = state.setdefault("channels", {})
    channel = _channel_name(provider)
    ch = channels.setdefault(channel, {})
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    prev_status = ch.get("status", "unknown")
    prev_class = ch.get("failure_class")
    prev_duration_s = _duration_since(ch.get("since"), now=now) if prev_status == "unavailable" else 0
    recovered = probe and prev_status == "unavailable"

    ch["status"] = "ok"
    if probe:
        ch["last_probe_ok_at"] = now_iso
    ch["last_ok_at"] = now_iso
    if recovered:
        ch["last_recovery_at"] = now_iso
        ch["since"] = now_iso
        ch["failure_class"] = None
        ch["failure_count"] = 0

    _recompute_overall(state)
    state["updated_at"] = now_iso
    _save_health_state(state)

    return {
        "channel": channel,
        "recovered": recovered,
        "previous_status": prev_status,
        "previous_failure_class": prev_class,
        "previous_duration_s": prev_duration_s,
        "overall": state.get("overall"),
    }


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
    try:   # 2026-10-08 用户设定交易主线 (强美元): 只作背景, 不单独构成否决理由
        from usd_theme import brief as _theme_brief
        theme = _theme_brief()
    except Exception:
        theme = ""
    theme_block = (f"\nPortfolio theme set by the user (context only; do NOT veto solely because a "
                   f"trade does not fit the theme):\n{theme}\n") if theme else ""
    return f"""You are the final pre-trade risk gate for this local trading system.
{theme_block}
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


def split_cli_failures(status: str, provider: str, fallback_reason: str = "") -> list[tuple[str, str]]:
    """把 ai_prompt.query_ai_cli 的结果拆成逐通道的 (provider, status).

    2026-10-19 用户要求: Claude 没额度时先看 Codex, 两边都不行才如实回报.
    query_ai_cli 在主通道失败、备用也失败时, status = "<主>: <主状态>; <备>=<备状态>",
    fallback_reason = "<主>: <主状态>". 以前只按备用通道记一笔, 且用合并字符串分类
    (例如 Codex 协议错误被 "claude: quota" 关键词误判成 quota). 这里逐通道拆开.
    """
    status = str(status or "")
    fb = str(fallback_reason or "").strip()
    if not fb or ":" not in fb:
        return [(provider, status)]
    prefix, rest = fb.split(":", 1)
    if prefix.strip().lower() in ("claude", "codex"):
        primary, primary_status = prefix.strip(), rest.strip()
    else:   # 格式不带通道名: 主通道就是另一边
        primary = "claude" if str(provider or "").strip().lower() == "codex" else "codex"
        primary_status = fb
    marker = f"; {str(provider or '').strip().lower()}="
    fallback_status = status.split(marker, 1)[1] if marker in status else status
    return [(primary, primary_status), (provider, fallback_status.strip())]


def _fail_decision(
    decision: dict,
    status: str,
    prompt_path: str | None = None,
    provider: str = "Codex",
    fallback_reason: str = "",
) -> dict:
    """基础设施不可用时的 fail-closed 兜底.

    EVO-20261015-1 补强:
      1. 对 status 做故障分类并写入 ai_gate_health.json (双通道);
      2. 显式标记 ai_gate_available=False, model_confidence='N/A' — 下游不要再把
         这种"无订单"读成模型给出的 HOLD;
      3. 保留原始规则信号 (original_rule_action/original_rule_confidence);
      4. 新故障 (或分类切换) 时合并一次告警, 同类重复故障会被 dedup 合并;
         恢复告警交给 _ai_cli_diag 的实际探测, 这里不发.
    下单许可仍按 fail-closed 处理, 本次改动不改变任何 order gating.
    """
    # 2026-10-19: 逐通道记录 (主通道 Claude 失败 → 备用 Codex 也失败 → 两条都如实记)
    channel_failures = []
    health = None
    for prov, st in split_cli_failures(status, provider, fallback_reason):
        cls = classify_cli_failure(st)
        h = record_cli_failure(prov, st, cls)
        channel_failures.append({"channel": h["channel"], "failure_class": cls,
                                 "status": str(st)[:300], "duration_s": h["duration_s"],
                                 "is_new_failure": h["is_new_failure"]})
        health = h
    failure_class = channel_failures[-1]["failure_class"]   # 最后尝试的通道
    summary = ", ".join(f"{c['channel']}={c['failure_class']}" for c in channel_failures)
    all_tried_down = len(channel_failures) > 1

    audit = _audit(
        provider,
        "HOLD",
        status,
        (f"ai_gate_unavailable ({summary}); "
         + ("Claude 与 Codex 均不可用, " if all_tried_down else "")
         + "fail-closed, 非模型给出的 HOLD"),
        confidence=None,
        prompt_path=prompt_path,
    )
    audit["ai_gate_available"] = False
    audit["failure_class"] = failure_class
    audit["channel_failures"] = channel_failures
    audit["all_tried_channels_unavailable"] = all_tried_down
    audit["failure_duration_s"] = max(c["duration_s"] for c in channel_failures)
    audit["model_confidence"] = "N/A"
    audit["health_channel"] = "+".join(c["channel"] for c in channel_failures)
    audit["health_overall"] = health.get("overall")

    original_action = (decision or {}).get("action")
    try:
        original_conf = (decision or {}).get("confidence")
    except Exception:
        original_conf = None

    # 只在"新故障"或 class 切换时推送告警, 否则合并. 推送失败不影响交易路径.
    if any(c["is_new_failure"] for c in channel_failures):
        try:
            from notifications import notify_ai_gate_unavailable
            notify_ai_gate_unavailable(
                classification=summary if all_tried_down else failure_class,
                duration_s=audit["failure_duration_s"],
                channel=audit["health_channel"],
                status_text=status,
                overall=health.get("overall"),
            )
        except Exception as exc:
            logger.warning(f"[claude-gate] notify_ai_gate_unavailable failed: {exc}")

    if _fail_closed():
        out = _demote(decision, "HOLD", audit)
        out["ai_gate_available"] = False
        out["ai_gate_failure_class"] = failure_class
        out["original_rule_action"] = original_action
        out["original_rule_confidence"] = original_conf
        # claude_gate 内也带上原始规则信号, dashboard/复盘可直接读
        out["claude_gate"]["original_rule_action"] = original_action
        out["claude_gate"]["original_rule_confidence"] = original_conf
        return out
    out = deepcopy(decision)
    bypass = dict(audit)
    bypass["verdict"] = "BYPASS_FAIL_OPEN"
    bypass["original_rule_action"] = original_action
    bypass["original_rule_confidence"] = original_conf
    out["claude_gate"] = bypass
    out["ai_gate_available"] = False
    out["ai_gate_failure_class"] = failure_class
    out["original_rule_action"] = original_action
    out["original_rule_confidence"] = original_conf
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
            "ai_gate_available": True,
        }
        approved["ai_gate_available"] = True
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
    output, status, provider, fb_reason = query_ai_cli(prompt_text, timeout=_timeout_sec(),
                                                       complexity="medium")

    if not output:
        logger.warning(f"[claude-gate] {ticker} unavailable: {status}")
        return _fail_decision(decision, status, str(prompt_path), provider,
                              fallback_reason=fb_reason)

    # 主通道失败、备用通道接手成功: 主通道的故障也如实记下 (健康状态 → degraded)
    if fb_reason:
        try:
            primary, primary_status = split_cli_failures(status, provider, fb_reason)[0]
            record_cli_failure(primary, primary_status)
        except Exception:
            pass

    raw_path.write_text(output + "\n", encoding="utf-8")
    parsed = _extract_json(output)
    if not parsed:
        logger.warning(f"[claude-gate] {ticker} invalid JSON; raw={raw_path}")
        return _fail_decision(decision, "invalid_json", str(prompt_path), provider)

    verdict = str(parsed.get("verdict", "")).upper().strip()
    if verdict not in ALLOWED_VERDICTS:
        logger.warning(f"[claude-gate] {ticker} invalid verdict={verdict!r}")
        return _fail_decision(decision, f"invalid_verdict: {verdict}", str(prompt_path), provider)

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

    # 走到这里说明 CLI 实际回复了结构合法的 verdict → 当前通道健康, 刷新 health
    try:
        record_cli_success(provider)
    except Exception:
        pass
    audit["ai_gate_available"] = True

    if verdict == "APPROVE":
        approved = deepcopy(decision)
        approved["claude_gate"] = audit
        approved["ai_gate_available"] = True
        logger.info(f"[claude-gate] {ticker} APPROVE: {audit['reason']}")
        return approved

    logger.info(f"[claude-gate] {ticker} {verdict}: {audit['reason']}")
    active_probe = _sim_active_probe(decision, market, audit)
    return active_probe if active_probe is not None else _demote(decision, verdict, audit)
