"""decision_context.py — 单一 immutable snapshot 供决策消费.

WP04 (audit 2026-09-19) 深度重构第一步. 目标: 消灭"决策过程读几处 live cache"
的模式, 让每次决策的所有输入被 DecisionContext 冻结, 保证:

- **可重放**: 同一 as_of + 同一版本 → 同一输出
- **无 look-ahead**: backtest 不能意外读到当前 thesis / 当前 HMM
- **可审计**: version 字段能回答"这个决定用了哪个 code / 哪份 data"

## 设计原则

DecisionContext 是**冻结的读**, 不是**mutable state**. 所有字段在 __init__ 后
不改; 修改需要构造新对象 (dataclass frozen=True).

## 目前 (2026-09-20) 的移植状态

- ✅ context 模块 + builder + tests
- ✅ 一个 sample migration: `_apply_thesis_filter(result, ticker, context=None)`
     若 context 提供 → 用 context.thesis_snapshot; 否则 fallback 读 live thesis_config
- ⏳ 其他 call site 逐步迁移 (get_decision, cohort_tracker, top_picks 等)
     每次迁移都保留旧签名兼容, 直到全部 done 才 deprecate

## 与 F05 BACKTEST_MODE flag 的关系

F05 用 env var 简单跳过 thesis filter. DecisionContext 是 proper 替代:
backtest 传显式 context (thesis_snapshot={}), 不需要 flag hack. flag 保留
作为渐进期兼容, 全部 migrate 后删除.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional


# EVO-20261013-2 (2026-10-13): 决策/日志/成交三端价差阈值.
# |live_quote - ref_price| / ref_price 超过该阈值 → log/dashboard 打
# PRICE_DIVERGENCE 标. 不改规则下单口径, 仅做基础设施级一致性校验.
PRICE_DIVERGENCE_THRESHOLD = 0.006   # 0.6%


def compute_price_divergence(ref_price, live_quote) -> Optional[float]:
    """返回 (live_quote - ref_price) / ref_price. 任一无效或 ≤0 → None."""
    try:
        r = float(ref_price) if ref_price is not None else 0.0
        q = float(live_quote) if live_quote is not None else 0.0
    except (TypeError, ValueError):
        return None
    if r <= 0 or q <= 0:
        return None
    return (q - r) / r


def is_price_diverged(ref_price, live_quote,
                      threshold: float = PRICE_DIVERGENCE_THRESHOLD) -> bool:
    """便捷判断: 超阈值 True; 任一无效 False."""
    pct = compute_price_divergence(ref_price, live_quote)
    return pct is not None and abs(pct) >= threshold


def _to_immutable(value):
    """R04: deep copy first (isolate from source), then recursively wrap
    dict → MappingProxyType, list → tuple. Result: reads work like dict/list,
    writes raise TypeError."""
    if value is None:
        return None
    if isinstance(value, MappingProxyType):
        # Already immutable - unwrap for deepcopy, then re-wrap
        return _to_immutable(dict(value))
    if isinstance(value, dict):
        # deep copy leaves
        return MappingProxyType({k: _to_immutable(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_to_immutable(v) for v in value)
    # Primitives (int, float, str, bool, datetime, etc.) are already immutable
    return value


def _mutable_copy(value):
    """R04: convert immutable-wrapped value back to plain dict/list so it can
    be passed into a new DecisionContext (which will re-wrap via __post_init__).
    Used by with_updates() to break sharing."""
    if value is None:
        return None
    if isinstance(value, MappingProxyType):
        return {k: _mutable_copy(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {k: _mutable_copy(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_mutable_copy(v) for v in value]
    if isinstance(value, list):
        return [_mutable_copy(v) for v in value]
    return value


# Back-compat alias
_immutable = _to_immutable


@dataclass(frozen=True)
class DecisionContext:
    """Immutable decision-time snapshot.

    Fields:
      as_of                  : 决策时点 (决定 "现在是什么时候" 的单一源)
      market                 : ticker snapshot (price/indicators/ts) - IMMUTABLE
      events                 : earnings/econ calendar 已按 as_of 过滤 - IMMUTABLE
      macro                  : vix/rates/etc at as_of - IMMUTABLE
      thesis_snapshot        : blacklist/whitelist/soft_blacklist at as_of
                                (None → mean live; {} → mean 无 thesis, e.g. historical)
      calibration_snapshot   : 校准参数 (若 as_of 有对应版本)
      board_regime           : regime label at as_of
      hmm_state              : HMM meta state (R04 2026-09-20 followup, 从 live cache 迁走)
      sector_regime_snapshot : per-ticker sector regime map (R04)
      strategy_version       : code version tag (git SHA / manifest hash)
      data_version           : data snapshot version tag
      is_backtest            : True = 历史重放; False = live decision
      builder                : "live" / "snapshot" / "explicit" — 追溯来源

    R04 followup fix (2026-09-20 audit v2): dict/list fields wrapped in
    MappingProxyType/tuple at __post_init__ (via object.__setattr__ since
    frozen=True). External mutation of the source dict does not leak in;
    caller mutation of ctx.market["price"] = X raises TypeError.
    """
    as_of:                    datetime
    market:                   Any                   = field(default_factory=dict)
    events:                   Any                   = field(default_factory=dict)
    macro:                    Any                   = field(default_factory=dict)
    thesis_snapshot:          Optional[Any]         = None
    calibration_snapshot:     Optional[Any]         = None
    board_regime:             Optional[str]         = None
    hmm_state:                Optional[str]         = None
    sector_regime_snapshot:   Optional[Any]         = None
    strategy_version:         str                   = "unknown"
    data_version:             str                   = "unknown"
    is_backtest:              bool                  = False
    builder:                  str                   = "explicit"
    # EVO-20261013-2: 决策时点的实时报价快照 (与 market.price 可能对不齐).
    # 规则引擎仍用 market.price (日 K / 盘前参考); 这里只是旁路存证, 便于日志
    # 和复盘脚本还原真实触发价差. 任一字段 None = 当时未成功抓到 live quote.
    live_quote:               Optional[float]       = None
    quote_source:             Optional[str]         = None
    staleness_sec:            Optional[float]       = None

    def __post_init__(self):
        # R04: deep-immutable wrap for dict/list-like fields.
        # frozen=True disallows normal assignment; use object.__setattr__.
        for name in ("market", "events", "macro", "thesis_snapshot",
                      "calibration_snapshot", "sector_regime_snapshot"):
            raw = object.__getattribute__(self, name)
            if raw is None:
                continue
            wrapped = _to_immutable(raw)
            object.__setattr__(self, name, wrapped)

    def with_updates(self, **kwargs) -> "DecisionContext":
        """Return an independent snapshot with overrides. All fields
        (updated or not) get fresh deep copies, then re-wrapped as immutable
        by __post_init__ on the new instance. Nothing shares references
        with the parent context.
        """
        base = {
            "as_of":                  self.as_of,
            "market":                 _mutable_copy(self.market),
            "events":                 _mutable_copy(self.events),
            "macro":                  _mutable_copy(self.macro),
            "thesis_snapshot":        _mutable_copy(self.thesis_snapshot),
            "calibration_snapshot":   _mutable_copy(self.calibration_snapshot),
            "board_regime":           self.board_regime,
            "hmm_state":              self.hmm_state,
            "sector_regime_snapshot": _mutable_copy(self.sector_regime_snapshot),
            "strategy_version":       self.strategy_version,
            "data_version":           self.data_version,
            "is_backtest":            self.is_backtest,
            "builder":                self.builder,
            "live_quote":             self.live_quote,
            "quote_source":           self.quote_source,
            "staleness_sec":          self.staleness_sec,
        }
        base.update(kwargs)
        return DecisionContext(**base)

    # EVO-20261013-2: 三端价差辅助 ────────────────────────────────────────
    def ref_price(self) -> Optional[float]:
        """从 market 快照里取当前使用的参考价 (日 K / 盘前参考)."""
        if not self.market:
            return None
        try:
            p = self.market.get("price") if hasattr(self.market, "get") else None
            return float(p) if p is not None else None
        except (TypeError, ValueError):
            return None

    def price_divergence_pct(self) -> Optional[float]:
        """ref_price 与 live_quote 的相对价差. 任一缺失 → None."""
        return compute_price_divergence(self.ref_price(), self.live_quote)

    def is_price_diverged(self,
                          threshold: float = PRICE_DIVERGENCE_THRESHOLD) -> bool:
        pct = self.price_divergence_pct()
        return pct is not None and abs(pct) >= threshold

    def price_snapshot(self) -> dict:
        """三端一致性校验的标准输出 (log / dashboard / postmortem 共用)."""
        ref = self.ref_price()
        pct = compute_price_divergence(ref, self.live_quote)
        flagged = pct is not None and abs(pct) >= PRICE_DIVERGENCE_THRESHOLD
        return {
            "ref_price":       None if ref is None else round(float(ref), 4),
            "live_quote":      None if self.live_quote is None else round(float(self.live_quote), 4),
            "quote_source":    self.quote_source,
            "staleness_sec":   None if self.staleness_sec is None else round(float(self.staleness_sec), 2),
            "divergence_pct":  None if pct is None else round(pct, 6),
            "divergence_flag": "PRICE_DIVERGENCE" if flagged else None,
            "threshold":       PRICE_DIVERGENCE_THRESHOLD,
        }

    def is_ticker_blacklisted(self, ticker: str) -> tuple[bool, str]:
        """Read blacklist from thesis_snapshot (if provided) instead of live.

        - snapshot is None → fallback to live thesis_config (兼容旧路径)
        - snapshot is {}   → 显式无 thesis, 不 block 任何 ticker (backtest 用)
        - snapshot has data → 按 snapshot 判
        """
        snap = self.thesis_snapshot
        if snap is None:
            # Fallback to live source (兼容, 待所有 caller 都传 context 后再拆)
            try:
                from thesis_config import is_ticker_blacklisted
                return is_ticker_blacklisted(ticker)
            except Exception:
                return False, ""
        # Snapshot mode: 只查 snapshot 里的 blacklist
        blacklist = snap.get("blacklist_tickers") or []
        norm_target = _norm(ticker)
        for t in blacklist:
            if _norm(t) == norm_target:
                return True, snap.get("blacklist_reason", "thesis_blacklist")
        return False, ""

    def is_ticker_soft_blacklisted(self, ticker: str) -> tuple[bool, str, dict]:
        """Same pattern for soft blacklist."""
        snap = self.thesis_snapshot
        if snap is None:
            try:
                from thesis_config import is_ticker_soft_blacklisted
                return is_ticker_soft_blacklisted(ticker)
            except Exception:
                return False, "", {}
        soft = snap.get("soft_blacklist") or {}
        if not isinstance(soft, (dict, MappingProxyType)):
            return False, "", {}
        norm_target = _norm(ticker)
        for key, meta in soft.items():
            if _norm(key) == norm_target:
                if not isinstance(meta, (dict, MappingProxyType)):
                    return True, snap.get("soft_blacklist_reason", "soft_thesis_block"), {
                        "min_confidence": 7, "since": "",
                    }
                try:
                    mc = int(meta.get("min_confidence", 7))
                except (TypeError, ValueError):
                    mc = 7
                return True, meta.get("reason", snap.get("soft_blacklist_reason", "soft_thesis_block")), {
                    "min_confidence": mc,
                    "since":          meta.get("since", ""),
                }
        return False, "", {}


def _norm(ticker: str) -> str:
    """Normalize US./HK./JP. prefix + uppercase (matches thesis_config)."""
    t = (ticker or "").upper().strip()
    for pfx in ("US.", "HK.", "JP."):
        if t.startswith(pfx):
            t = t[len(pfx):]
    return t


# ─── Builders ────────────────────────────────────────────────────────────

def from_live_now(
    ticker: str,
    market: Optional[dict] = None,
    events: Optional[dict] = None,
    macro: Optional[dict] = None,
    board_regime: Optional[str] = None,
    live_quote: Optional[float] = None,
    quote_source: Optional[str] = None,
    staleness_sec: Optional[float] = None,
) -> DecisionContext:
    """Build a live-mode context from current module state.

    Reads current thesis_config, calibration, and version fingerprint.
    For live decisions where the "now" perspective is authoritative.
    """
    try:
        import json as _json
        import thesis_config as _tc
        thesis_snap = _json.loads(_json.dumps(_tc._load() or {}))
        # 2026-10-03: 半导体风险开关是 live 状态 → 冻结进快照的 soft_blacklist
        g = _tc.semi_risk_guard_config()
        if g and _tc.semi_risk_guard_state().get("active"):
            soft = thesis_snap.setdefault("soft_blacklist", {})
            for tk in g.get("tickers", []):
                old = soft.get(tk) if isinstance(soft.get(tk), dict) else {}
                mc = max(int(old.get("min_confidence", 0) or 0), int(g.get("min_confidence", 10)))
                soft[tk] = {**old, "min_confidence": mc, "reason": "semi_risk_guard"}
    except Exception:
        thesis_snap = None
    try:
        from decision_agent import get_calibration_info
        calib = get_calibration_info()
    except Exception:
        calib = None
    # Version tag (git SHA + dirty flag)
    version = "unknown"
    try:
        from _baseline_manifest import _git_status
        gs = _git_status()
        version = gs.get("head", "unknown")[:12]
        if gs.get("is_dirty"):
            version = f"{version}-dirty"
    except Exception:
        pass
    return DecisionContext(
        as_of=datetime.now(timezone.utc),
        market=market or {},
        events=events or {},
        macro=macro or {},
        thesis_snapshot=thesis_snap,
        calibration_snapshot=calib,
        board_regime=board_regime,
        strategy_version=version,
        data_version="live",
        is_backtest=False,
        builder="live",
        live_quote=live_quote,
        quote_source=quote_source,
        staleness_sec=staleness_sec,
    )


def from_snapshot(
    as_of: datetime,
    market: dict,
    events: Optional[dict] = None,
    macro: Optional[dict] = None,
    thesis_snapshot: Optional[dict] = None,
    board_regime: Optional[str] = None,
    strategy_version: str = "historical",
    live_quote: Optional[float] = None,
    quote_source: Optional[str] = None,
    staleness_sec: Optional[float] = None,
) -> DecisionContext:
    """Build a backtest-mode context. thesis_snapshot={} 表示无 thesis 干预,
    避免历史 backtest 被今日 thesis 污染."""
    return DecisionContext(
        as_of=as_of,
        market=market,
        events=events or {},
        macro=macro or {},
        thesis_snapshot=thesis_snapshot if thesis_snapshot is not None else {},
        calibration_snapshot=None,
        board_regime=board_regime,
        strategy_version=strategy_version,
        data_version=as_of.strftime("%Y%m%d"),
        is_backtest=True,
        builder="snapshot",
        live_quote=live_quote,
        quote_source=quote_source,
        staleness_sec=staleness_sec,
    )
