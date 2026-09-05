"""
auto_rebalance.py — 每日 pre-close 一次的自动组合再平衡

设计原则:
1. **只在 pre-close 跑一次/天**, 避免日内噪音
2. **regime gate**: crisis / 深回撤 → 跳过, 不 rebalance 进恐慌
3. **只跑 |current - target| > 3% 的仓位**, 小抖动不动
4. **卖优先, 买后置**: 先把超配减到目标, 再用释放现金买欠配
5. **thesis-anchored target 权重表**: 见 `_TARGET_TEMPLATE`
6. **信号 conf 调档**: 目标权重 × f(conf) — conf=5 拿满配, conf=2 只拿 40%
7. **所有订单落 signals/rebalance_plan.jsonl** 便于回溯

CLI:
    python auto_rebalance.py --dry-run   # 打印计划不下单
    python auto_rebalance.py             # 真跑 (受 AUTO_REBALANCE_ENABLED 控制)
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config import SIGNALS_DIR, MOVE_WARN, MOVE_CRISIS, MOVE_EXTREME, HY_OAS_WARN_BPS
from notifier import logger


# ── Thesis 目标权重模板 ──────────────────────────────────────────────────────
# 从 project_thesis_2026Q3.md: bond+cloud long, avoid semi
# 每个 ticker 给一个"满配"目标 (信号 conf ≥ 5 时的上限), 系统按当前 conf 缩放.
# 总和 ≥ 100% 是故意的: 让 conf 权重决定实际 sum, 通常落在 70-85%.
_TARGET_TEMPLATE = {
    # === Bond 长仓 (thesis 主力) ===
    "SHY":  {"class": "bond", "max_pct": 25.0, "min_conf": 2, "note": "1-3Y Treasury"},
    "IEI":  {"class": "bond", "max_pct": 25.0, "min_conf": 2, "note": "3-7Y Treasury"},

    # === Cloud/AI 长仓 (thesis 主力) ===
    "MSFT": {"class": "cloud", "max_pct": 10.0, "min_conf": 3, "note": "Azure cloud + AI"},
    "GOOGL": {"class": "cloud", "max_pct": 8.0, "min_conf": 3, "note": "GCP cloud"},
    "NBIS": {"class": "cloud", "max_pct": 5.0, "min_conf": 2, "note": "Nebius AI cloud pure-play"},

    # === 防御 / 通胀 hedge ===
    "GLD":  {"class": "hedge", "max_pct": 8.0,  "min_conf": 2, "note": "gold hedge"},
    "XLV":  {"class": "hedge", "max_pct": 4.0,  "min_conf": 3, "note": "healthcare defensive"},
    "USO":  {"class": "hedge", "max_pct": 3.0,  "min_conf": 3, "note": "oil / inflation proxy"},

    # === Probe (thesis 之外的探仓, 严格限量) ===
    "CBRS": {"class": "probe", "max_pct": 2.0,  "min_conf": 4, "note": "Cerebras AI chip"},
    "LITE": {"class": "probe", "max_pct": 2.0,  "min_conf": 4, "note": "optical/AI DC"},

    # === Avoid (thesis 明确避开: 半导体) ===
    # 不列在 target 里 = 目标权重 0, 有仓位就减
}
# 注: TQQQ / SOXL / DRAM / MULL / NVDA / AMAT / KLAC 等半导体系不在 target
#     → 有仓位就 rebalance 减到 0

_CASH_FLOOR_PCT = 15.0      # 现金底线, 不允许一次 rebalance 用光
_MIN_ORDER_DIFF_PCT = 3.0   # 偏差 < 3% 不动 (避免小抖动)
_MIN_ORDER_USD = 5000       # 最小订单金额

# 传导链断点 → duration/敞口 联动调整
# 根据 bond_ai_interpret 的 chain_blocked_at 动态改 target max_pct.
# 逻辑: 当断点在 nfci (Fed QT 实质停止 / nfci loose baseline), 长端 term
# premium 抬升压力持续, 中/长久期债 (IEI 3-7Y, TLT 20+Y) 承压 → 减仓,
# 前端 (SHY 1-3Y) 因曲线陡峭化利好 → 允许更满仓.
# 流动性危机预警 → 全局风险敞口调整
# 读 bond_monitor 的 MOVE / SOFR-IORB / KBE-SPY 3 指标, 取 worst status,
# 按等级对 class 施加倍率. 级别越高越 defensive.
#   L1 (any warn): 轻度收缩, cloud -15%, hedge +10%
#   L2 (any bad): 明显防御, cloud -40%, hedge +30%, probe 半仓
#   L3 (any extreme): 危机模式, cloud -70%, bond +20% (flight-to-quality), probe 0
_LIQ_CRISIS_SCALER = {
    # (level, class) → scaler
    (1, "cloud"): 0.85, (1, "hedge"): 1.10, (1, "probe"): 0.75,
    (2, "cloud"): 0.60, (2, "hedge"): 1.30, (2, "probe"): 0.50, (2, "bond"): 1.10,
    (3, "cloud"): 0.30, (3, "hedge"): 1.50, (3, "probe"): 0.0,  (3, "bond"): 1.20,
}


def _liquidity_crisis_level(mc: dict) -> tuple[int, list[str]]:
    """按 MOVE / SOFR-IORB / KBE-SPY 3 指标计算流动性危机等级.
    返回 (level 0-3, [trigger 列表说明])."""
    triggers: list[str] = []
    level = 0
    move = mc.get("move_index")
    if move is not None:
        if move >= MOVE_EXTREME:
            level = max(level, 3); triggers.append(f"MOVE {move} 极端")
        elif move >= MOVE_CRISIS:
            level = max(level, 2); triggers.append(f"MOVE {move} 危机区")
        elif move >= MOVE_WARN:
            level = max(level, 1); triggers.append(f"MOVE {move} 抬升")
    sofr = mc.get("sofr_iorb_spread_bps")
    if sofr is not None:
        if sofr >= 15:
            level = max(level, 3); triggers.append(f"SOFR-IORB +{sofr}bps 2019 repo 级")
        elif sofr >= 5:
            level = max(level, 2); triggers.append(f"SOFR-IORB +{sofr}bps 破位")
        elif sofr >= 1:
            level = max(level, 1); triggers.append(f"SOFR-IORB +{sofr}bps 触及")
    kbe = mc.get("kbe_spy_20d_delta_pct")
    if kbe is not None:
        if kbe <= -10:
            level = max(level, 3); triggers.append(f"KBE/SPY {kbe}% SVB/2008 级")
        elif kbe <= -6:
            level = max(level, 2); triggers.append(f"KBE/SPY {kbe}% SVB 前 2 周")
        elif kbe <= -3:
            level = max(level, 1); triggers.append(f"KBE/SPY {kbe}% 银行走弱")
    return level, triggers


# ============================================================================
# 第 8 层 overlay: 事件驱动 triggers (impact_matrix action_triggers 落地版)
# ============================================================================
# 目的: 让 impact_matrix 从 display-only → 实际驱动 auto_rebalance
# 用户 memory: "不能每次都手动确认" - 事件当日应自动响应
#
# 5 个高价值 triggers (基于 impact_matrix 里 T+0 action_triggers 提炼):
#   1. MOVE >= 140: 债市 crisis → 减 IEI/TLT
#   2. MOVE >= 180: 极端 → 大幅减 duration + 加 GLD
#   3. KBE/SPY 20d <= -10%: panic flight-to-quality → 减股加 hedge
#   4. UST 10Y 1d 变化 >= +25bps: 长端 blow → 减 IEI
#   5. HY OAS >= 400bps: 信用 stress 累积 → 减 cloud
#
# 安全阀:
#   - 24h cool-down: 同一 trigger 一天最多触发 1 次 (查 rebalance_plan.jsonl)
#   - dry-run 优先: default trigger 只 log, AUTO_REBALANCE_EVENT_EXEC=1 才真下单
#   - 缓存 fresh check: bond_monitor cache 必须 < 12h
# ============================================================================
_EVENT_TRIGGERS = [
    {
        "name": "move_extreme",
        "check": lambda mc: (mc.get("move_index") or 0) >= 180,
        "action": {
            "scaler_by_ticker": {"IEI": 0.5, "TLT": 0.3, "TLH": 0.4},
            "scaler_by_class": {"hedge": 1.5},
            "reason": "MOVE ≥180 债市极端 (2008/2020/SVB 级)",
        },
    },
    {
        "name": "move_crisis",
        "check": lambda mc: 140 <= (mc.get("move_index") or 0) < 180,
        "action": {
            "scaler_by_ticker": {"IEI": 0.75, "TLT": 0.5},
            "scaler_by_class": {"hedge": 1.3},
            "reason": "MOVE ≥140 债市 crisis 区",
        },
    },
    {
        "name": "bank_panic",
        "check": lambda mc: (mc.get("kbe_spy_20d_delta_pct") or 0) <= -10,
        "action": {
            "scaler_by_class": {"cloud": 0.3, "hedge": 1.5, "bond": 1.2, "probe": 0.0},
            "reason": "KBE/SPY 20d ≤-10% SVB/2008 级 panic",
        },
    },
    {
        "name": "long_end_blow",
        "check": lambda mc: False,  # 需 1d 数据, 暂空 (yields dict.chg_1d_bps 待接)
        "action": {
            "scaler_by_ticker": {"IEI": 0.8, "TLT": 0.5},
            "reason": "UST 10Y 1d ≥+25bps 长端 blow",
        },
    },
    {
        "name": "hy_stress",
        "check": lambda mc: (mc.get("cdx_hy_bps") or 0) >= 400,
        "action": {
            "scaler_by_class": {"cloud": 0.6, "hedge": 1.2},
            "reason": "HY OAS ≥400bps 信用 stress",
        },
    },
]


def _load_macro_context() -> dict:
    """从 bond_monitor_v2 cache 读 macro_context (12h 内 fresh)."""
    from datetime import datetime
    cache_dir = Path(SIGNALS_DIR).parent / ".webui_cache"
    for name in ("bond_monitor_v2.json", "bond_monitor.json"):
        cache = cache_dir / name
        if not cache.exists():
            continue
        age_h = (datetime.now().timestamp() - cache.stat().st_mtime) / 3600
        if age_h > 12:
            continue
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            data = d.get("data") if "data" in d else d
            # 也补 yields.10y.chg_1d_bps 到 macro_context
            mc = dict(data.get("macro_context", {}) or {})
            y10 = ((data.get("yields") or {}).get("10y") or {})
            if y10.get("chg_1d_bps") is not None:
                mc["ust_10y_1d_bps"] = y10["chg_1d_bps"]
            return mc
        except Exception:
            continue
    return {}


def _load_event_triggers() -> list[dict]:
    """检查所有 event triggers, 返回激活的列表."""
    mc = _load_macro_context()
    # 加 ust_10y_1d_bps 到 check function 可用范围
    active = []
    for trig in _EVENT_TRIGGERS:
        try:
            # rewrite check for long_end_blow (需要 ust_10y_1d_bps)
            if trig["name"] == "long_end_blow":
                if (mc.get("ust_10y_1d_bps") or 0) >= 25:
                    active.append({**trig, "triggered_value": mc.get("ust_10y_1d_bps")})
                continue
            if trig["check"](mc):
                # 记录触发时的值
                value_key = {
                    "move_extreme": "move_index",
                    "move_crisis": "move_index",
                    "bank_panic": "kbe_spy_20d_delta_pct",
                    "hy_stress": "cdx_hy_bps",
                }.get(trig["name"])
                active.append({**trig, "triggered_value": mc.get(value_key) if value_key else None})
        except Exception:
            continue
    return active


def _check_event_trigger_cooldown(trigger_name: str, cooldown_hours: int = 24) -> bool:
    """检查过去 24h 是否已 fire 同名 trigger. 返回 True = 可以 fire, False = 冷却中."""
    from datetime import datetime, timedelta
    log = Path(SIGNALS_DIR) / "rebalance_plan.jsonl"
    if not log.exists():
        return True
    try:
        cutoff = datetime.now() - timedelta(hours=cooldown_hours)
        lines = log.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-100:]):  # 只看最近 100 条
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                ts_str = d.get("ts", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str)
                if ts < cutoff:
                    break
                # 若有该 trigger 记录 → cooldown 中
                triggers = d.get("event_triggers_fired", [])
                if trigger_name in [t.get("name") for t in triggers if isinstance(t, dict)]:
                    return False
            except Exception:
                continue
    except Exception:
        pass
    return True


def _load_stock_bond_corr() -> tuple[float | None, str]:
    """读 SPY-IEF 60d 相关性. 返回 (corr, regime_label).
    regime: hedge_ok / weakening / broken / extreme
    """
    from datetime import datetime
    cache_dir = Path(SIGNALS_DIR).parent / ".webui_cache"
    for name in ("bond_monitor_v2.json", "bond_monitor.json"):
        cache = cache_dir / name
        if not cache.exists():
            continue
        age_h = (datetime.now().timestamp() - cache.stat().st_mtime) / 3600
        if age_h > 12:
            continue
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            data = d.get("data") if "data" in d else d
            mc = data.get("macro_context", {}) if isinstance(data, dict) else {}
            corr = mc.get("spy_ief_60d_corr")
            if corr is None:
                continue
            if corr > 0.4:   return corr, "extreme"
            if corr > 0.1:   return corr, "broken"
            if corr > -0.3:  return corr, "weakening"
            return corr, "hedge_ok"
        except Exception:
            continue
    return None, ""


def _load_asia_repatriation_signal() -> tuple[bool, str]:
    """机构级 JP repatriation 信号 (BIS CIP + JP MoF 干预阈值).
    True = 应减 US long duration 敞口 (IEI/TLT).
    """
    from datetime import datetime
    cache_dir = Path(SIGNALS_DIR).parent / ".webui_cache"
    for name in ("bond_monitor_v2.json", "bond_monitor.json"):
        cache = cache_dir / name
        if not cache.exists():
            continue
        age_h = (datetime.now().timestamp() - cache.stat().st_mtime) / 3600
        if age_h > 12:
            continue
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            data = d.get("data") if "data" in d else d
            mc = data.get("macro_context", {}) if isinstance(data, dict) else {}
            hedged = mc.get("hedged_ust_10y_for_jp")
            jgb = mc.get("jgb_10y_pct")
            usdjpy = mc.get("usdjpy") or 0
            # BIS CIP 信号: hedged UST < JGB → JP 抛售 UST
            if hedged is not None and jgb is not None and hedged < jgb:
                return True, f"BIS CIP: hedged UST {hedged}% < JGB {jgb}%"
            # JP MoF 真实干预区: USDJPY >= 160
            if usdjpy >= 160:
                return True, f"USDJPY {usdjpy} >= 160 (JP MoF 真实干预)"
        except Exception:
            continue
    return False, ""


def _load_liquidity_state() -> tuple[int, list[str]]:
    """从 bond_monitor cache 读 3 指标, 算 crisis level. 12h 内 fresh 才用.
    cache 文件名带 _v2 后缀 (webui 版本化)."""
    from datetime import datetime
    cache_dir = Path(SIGNALS_DIR).parent / ".webui_cache"
    # 兼容 v1 / v2 文件名
    for name in ("bond_monitor_v2.json", "bond_monitor.json"):
        cache = cache_dir / name
        if cache.exists():
            age = datetime.now().timestamp() - cache.stat().st_mtime
            if age > 12 * 3600:
                continue
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                data = d.get("data") if "data" in d else d
                mc = data.get("macro_context", {}) if isinstance(data, dict) else {}
                return _liquidity_crisis_level(mc)
            except Exception:
                continue
    return 0, []


_CHAIN_BLOCKED_ADJUSTMENTS = {
    "nfci": {
        # nfci 松 = Fed 没实质紧 = 长端 term premium 持续, 减 duration
        "IEI":  0.72,   # 25 * 0.72 = 18
        "SHY":  1.20,   # 25 * 1.2 = 30 (曲线陡峭化利好前端)
        "MSFT": 1.0, "GOOGL": 1.0, "NBIS": 1.0,  # 股票不动
    },
    "em": {
        # 强美元但 EM 仍强 = dxy 传导失效, 通常伴随美股同步坚挺, 维持股票配置
    },
    "erp": {
        # real_rates 高但股票没跌 = 估值传导失效 = 泡沫风险, 减股票加防御
        "MSFT": 0.6, "GOOGL": 0.6, "NBIS": 0.5,
        "GLD":  1.3, "XLV": 1.3,
    },
    "credit": {
        # 极少见 (nfci 已 tight 但 credit 还 calm): 说明利差要开始扩, 提早减
        "MSFT": 0.7, "GOOGL": 0.7, "NBIS": 0.5,
        "SHY":  1.1, "IEI": 1.1,  # 债券 flight-to-quality
    },
}


REBAL_LOG = Path(SIGNALS_DIR) / "rebalance_plan.jsonl"


# ── 数据 helper ──────────────────────────────────────────────────────────────
def _load_current_positions() -> tuple[dict, float, float]:
    """从 trade_log 重建当前持仓 + 最新价. 返回 (positions, cash, nav).
    positions = {ticker: {qty, avg_cost, last_px}}
    """
    log_path = Path(SIGNALS_DIR) / "trade_log.jsonl"
    if not log_path.exists():
        return {}, 0.0, 0.0
    positions: dict = defaultdict(lambda: {"qty": 0, "cost": 0.0})
    last_px: dict = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            tk = d["ticker"].replace("US.", "")
            qty = d.get("qty", 0) or 0
            px = d.get("price", 0) or 0
            side = d.get("side", "")
            if side == "BUY":
                positions[tk]["qty"] += qty
                positions[tk]["cost"] += qty * px
            elif side == "SELL" and positions[tk]["qty"] > 0:
                avg = positions[tk]["cost"] / positions[tk]["qty"]
                positions[tk]["qty"] -= qty
                positions[tk]["cost"] -= qty * avg
            last_px[tk] = px
        except Exception:
            continue

    # 从最新信号里补最新 price (trade_log 的价格是历史成交价)
    import glob
    for p in glob.glob(f"{SIGNALS_DIR}/*_latest.json"):
        tk = Path(p).stem.replace("_latest", "")
        try:
            sig = json.loads(Path(p).read_text(encoding="utf-8"))
            mk = sig.get("market", {})
            if mk.get("price"):
                last_px[tk] = float(mk["price"])
        except Exception:
            continue

    open_pos = {tk: {"qty": p["qty"],
                     "avg_cost": p["cost"] / p["qty"] if p["qty"] else 0,
                     "last_px": last_px.get(tk, p["cost"] / p["qty"] if p["qty"] else 0)}
                for tk, p in positions.items() if p["qty"] > 0}

    # NAV 从 nav_history 拿最新
    nav_path = Path(SIGNALS_DIR) / "nav_history.jsonl"
    nav = 0.0
    if nav_path.exists():
        try:
            lines = nav_path.read_text(encoding="utf-8").strip().splitlines()
            nav = float(json.loads(lines[-1]).get("nav", 0))
        except Exception:
            pass
    mv = sum(p["qty"] * p["last_px"] for p in open_pos.values())
    cash = nav - mv
    return open_pos, cash, nav


_SIGNAL_MAX_AGE_HOURS = 48   # 周末 fresh window; > 48h 视为 stale 跳过

# P0 safeguard (2026-09-05): 至少 N 个 fresh signal 才能 rebalance.
# 少于此数 → 假设数据管道有问题, 跳过 rebalance 保护现有仓位.
# 3 = 至少 3 个 target ticker 有新数据 (通常 bond + cloud + hedge 至少 1 只覆盖).
_MIN_SIGNALS_FOR_REBALANCE = 3


def _load_signals() -> tuple[dict, dict]:
    """每 ticker 最新信号. 返回 (signals dict, prices dict).
    prices 里含所有有 market.price 的 ticker, 供未持仓 BUY 的定价.
    过滤 mtime > 48h 的文件, 防止 weekend stale / 已删 ticker 老数据驱动今日仓位.
    """
    import glob
    import time
    signals: dict = {}
    prices: dict = {}
    now = time.time()
    max_age_sec = _SIGNAL_MAX_AGE_HOURS * 3600
    skipped: list[str] = []
    for p in glob.glob(f"{SIGNALS_DIR}/*_latest.json"):
        tk = Path(p).stem.replace("_latest", "")
        try:
            age_sec = now - Path(p).stat().st_mtime
            if age_sec > max_age_sec:
                skipped.append(f"{tk}({age_sec/3600:.0f}h)")
                continue
            sig = json.loads(Path(p).read_text(encoding="utf-8"))
            dec = sig.get("decision", {})
            mkt = sig.get("market", {})
            signals[tk] = {
                "action": dec.get("action", "?"),
                "conf": int(dec.get("confidence", 0) or 0),
                "regime": dec.get("regime", "?"),
                "cum_5d": mkt.get("cum_5d_pct"),
                "cum_10d": mkt.get("cum_10d_pct"),
            }
            if mkt.get("price"):
                prices[tk] = float(mkt["price"])
        except Exception:
            continue
    if skipped:
        logger.warning(f"[auto_rebalance] 跳过 stale signals (>{_SIGNAL_MAX_AGE_HOURS}h): {', '.join(skipped)}")
    return signals, prices


# ── 目标权重计算 ─────────────────────────────────────────────────────────────
def _conf_scaler(conf: int, min_conf: int) -> float:
    """conf → 目标权重系数. min_conf 以下=0, 满仓 conf=5.
    conf=2 → 0.4, conf=3 → 0.6, conf=4 → 0.8, conf=5 → 1.0
    """
    if conf < min_conf:
        return 0.0
    return min(1.0, 0.2 + 0.2 * conf)


_REGIME_SCALER = {
    # (regime, class) → scaler; 缺省 1.0
    # bond 在 risk_off/crisis 里是避险资产, 不该跟着其它 class 一起缩
    # cloud/probe/hedge 在 risk_off 缩仓, crisis 里更低
    ("risk_off", "bond"):    1.1,
    ("risk_off", "cloud"):   0.7,
    ("risk_off", "hedge"):   1.0,
    ("risk_off", "probe"):   0.5,
    ("crisis", "bond"):      1.3,
    ("crisis", "cloud"):     0.3,
    ("crisis", "hedge"):     1.1,
    ("crisis", "probe"):     0.0,
    ("recession_risk", "bond"):  1.2,
    ("recession_risk", "cloud"): 0.6,
    ("recession_risk", "hedge"): 1.0,
    ("recession_risk", "probe"): 0.3,
    # 追涨/顶部 regime — 减 probe, 加 hedge, cloud 稍减
    ("overheated", "bond"):  1.0,
    ("overheated", "cloud"): 0.7,
    ("overheated", "hedge"): 1.2,
    ("overheated", "probe"): 0.3,
    # 强动量延续 — 保仓但 probe 谨慎
    ("bull_extended", "bond"):  0.9,
    ("bull_extended", "cloud"): 1.0,
    ("bull_extended", "hedge"): 0.9,
    ("bull_extended", "probe"): 0.7,
    # 健康回调低吸区 — cloud 加, probe 允许
    ("bull_pulling", "bond"):  1.0,
    ("bull_pulling", "cloud"): 1.1,
    ("bull_pulling", "hedge"): 0.9,
    ("bull_pulling", "probe"): 1.0,
    # 震荡 chop — 减 leverage, 稍加 hedge
    ("neutral_chop", "bond"):  1.0,
    ("neutral_chop", "cloud"): 0.8,
    ("neutral_chop", "hedge"): 1.1,
    ("neutral_chop", "probe"): 0.5,
    ("bull_chop", "bond"):  1.0,
    ("bull_chop", "cloud"): 0.9,
    ("bull_chop", "hedge"): 1.0,
    ("bull_chop", "probe"): 0.6,
}


def _load_chain_blocked_at() -> str | None:
    """从 bond_ai_interpret cache 读 chain_blocked_at (跨进程共享).
    只信 fresh (<12h) 的数据; 太老宁可不用."""
    from datetime import datetime, timedelta
    cache = Path(SIGNALS_DIR).parent / ".webui_cache" / "bond_ai_interpret.json"
    if not cache.exists():
        return None
    age = datetime.now().timestamp() - cache.stat().st_mtime
    if age > 12 * 3600:
        return None
    try:
        d = json.loads(cache.read_text(encoding="utf-8"))
        data = d.get("data") if "data" in d else d
        return data.get("chain_blocked_at")
    except Exception:
        return None


def compute_target_weights(signals: dict, regime: str,
                           drawdown_pct: float = 0.0,
                           chain_blocked_at: str | None = None,
                           liq_level: int | None = None) -> dict[str, float]:
    """按 thesis + 信号 + regime + 传导链 + 流动性危机 算目标权重.
    返回 {ticker: target_weight_pct}. 未在 template 且无仓位的 ticker 不出现.

    5 层 overlay:
      target = max_pct
             × conf_scaler(sig.conf)          # 信号强度 (0.4-1.0)
             × falling_knife_scaler            # 5/10d 落刀过滤 (0 或 0.5)
             × regime_scaler(regime, class)    # bull/bear/crisis 期
             × chain_scaler(blocked_at, tk)    # 传导链断点定向调
             × liq_scaler(liq_level, class)    # 流动性危机全局收缩

    liq_level: 0-3 (0 无, 1 warn, 2 bad, 3 extreme).
      None → 从 bond_monitor cache 自动算.
    """
    if drawdown_pct >= 30:
        return {}

    if chain_blocked_at is None:
        chain_blocked_at = _load_chain_blocked_at()
    chain_adj = _CHAIN_BLOCKED_ADJUSTMENTS.get(chain_blocked_at or "", {})

    if liq_level is None:
        liq_level, _ = _load_liquidity_state()

    # 机构级 JP repatriation 信号 (BIS CIP + JP MoF): 触发时减 US long duration
    asia_repat_trigger, asia_repat_reason = _load_asia_repatriation_signal()

    # 股债 60d 相关性: 正相关 = 债券不再对冲股票 (2022 股债双杀 regime)
    sb_corr, sb_regime = _load_stock_bond_corr()

    # 第 8 层: 事件驱动 triggers (impact_matrix action_triggers 落地版)
    # 有 cool-down 保护 (24h 同 trigger 只 fire 1 次)
    import os as _os
    event_exec = _os.environ.get("AUTO_REBALANCE_EVENT_EXEC", "0") == "1"
    active_event_triggers = []
    if event_exec:  # 默认关闭, 需显式启用
        for trig in _load_event_triggers():
            if _check_event_trigger_cooldown(trig["name"]):
                active_event_triggers.append(trig)

    targets: dict[str, float] = {}
    for tk, cfg in _TARGET_TEMPLATE.items():
        sig = signals.get(tk, {})
        conf = sig.get("conf", 0)
        scaler = _conf_scaler(conf, cfg["min_conf"])
        if scaler == 0:
            continue
        if sig.get("action") in ("SELL", "REDUCE"):
            continue
        # 落刀过滤: 若 5d 或 10d 累计跌 > 15%, 目标砍半 (或直接 0 若 conf 弱)
        # 回测证据: NBIS conf=2 + cum_5d -17% 触发 target 3%, 结果 5d 又跌 21% 亏 $9K
        cum_5d = sig.get("cum_5d") or 0
        cum_10d = sig.get("cum_10d") or 0
        if cum_5d <= -15 or cum_10d <= -20:
            if conf < 4:
                continue                # 弱信号 + 落刀 → 直接 0
            scaler *= 0.5               # 强信号 + 落刀 → 减半 (等企稳再补)
        cls = cfg["class"]
        regime_scaler = _REGIME_SCALER.get((regime, cls), 1.0)
        chain_scaler = chain_adj.get(tk, 1.0)
        liq_scaler = _LIQ_CRISIS_SCALER.get((liq_level, cls), 1.0) if liq_level > 0 else 1.0
        # Asia repatriation: 触发时减 US mid/long duration (IEI 特别中招)
        # SHY (1-3Y) 前端不太受影响; IEI (3-7Y) / TLT (20+Y) 减仓
        asia_scaler = 1.0
        if asia_repat_trigger:
            if tk == "IEI":
                asia_scaler = 0.6   # 减 40%
            elif tk in ("TLT", "TLH"):
                asia_scaler = 0.4   # 长端更严重
            elif cls == "hedge":
                asia_scaler = 1.15  # 加 GLD 因 JP 干预 = USD 弱 = 金价支持
        # 事件驱动 triggers (第 8 层): 由 impact_matrix action_triggers 提炼
        # 硬编码 5 个高价值 triggers, 触发时按 ticker/class scaler 叠加
        event_scaler = 1.0
        for trig in active_event_triggers:
            action = trig["action"]
            if "scaler_by_ticker" in action and tk in action["scaler_by_ticker"]:
                event_scaler *= action["scaler_by_ticker"][tk]
            if "scaler_by_class" in action and cls in action["scaler_by_class"]:
                event_scaler *= action["scaler_by_class"][cls]

        # 股债相关性 overlay: sb_corr 是**脆弱性 gauge** 不是 crisis predictor
        # (memory: feedback_correlation_is_regime_not_predictor.md)
        # 触发时应**加 hedge (GLD)** 而非砍 bond, 因为 corr broken 只是
        # 说"传统 60/40 对冲失效", 不代表 liquidity dry-up.
        # 真危机减债要靠 SOFR-EFFR / MOVE / HY OAS 领先指标.
        corr_scaler = 1.0
        if sb_regime == "extreme":  # corr > +0.4, 1970s 滞胀级 = 组合极脆弱
            if tk in ("IEI", "TLT", "TLH"): corr_scaler = 0.85   # 微减长/中久期
            elif cls == "hedge": corr_scaler = 1.4               # 加 GLD 多元 hedge
        elif sb_regime == "broken":  # corr > +0.1, 2022 双杀级
            if tk in ("IEI", "TLT"): corr_scaler = 0.92          # 略减
            elif cls == "hedge": corr_scaler = 1.2               # 加 hedge
        elif sb_regime == "weakening":  # corr > -0.3, 对冲弱化早期
            if cls == "hedge": corr_scaler = 1.1
        target = cfg["max_pct"] * scaler * regime_scaler * chain_scaler * liq_scaler * asia_scaler * corr_scaler * event_scaler
        targets[tk] = round(target, 2)
    return targets


# ── 订单生成 ─────────────────────────────────────────────────────────────────
def plan_rebalance(positions: dict, cash: float, nav: float,
                    targets: dict[str, float],
                    prices: dict | None = None) -> list[dict]:
    """当前权重 → 目标权重的最小订单集. 只在 |diff| ≥ 3% AND USD ≥ $5K 时产订单.
    prices: {ticker: last_px} — 用于未持仓的 BUY 目标 (从信号里补价)"""
    if nav <= 0:
        return []
    prices = prices or {}
    orders = []

    # 1) 先算所有 ticker 的 current_pct 和 target_pct
    universe = set(positions.keys()) | set(targets.keys())
    diffs = []
    for tk in universe:
        pos = positions.get(tk, {"qty": 0, "last_px": 0})
        current_mv = pos["qty"] * pos["last_px"]
        current_pct = current_mv / nav * 100
        target_pct = targets.get(tk, 0.0)  # 不在 target = 目标 0 (avoid list)
        diff_pct = target_pct - current_pct
        diff_usd = diff_pct / 100 * nav
        # 未持仓的 BUY 目标: last_px 从 prices 补 (signal 里的)
        px = pos["last_px"] or prices.get(tk, 0)
        diffs.append({
            "ticker": tk,
            "current_pct": round(current_pct, 2),
            "target_pct": round(target_pct, 2),
            "diff_pct": round(diff_pct, 2),
            "diff_usd": round(diff_usd, 0),
            "price": px,
            "qty_current": pos["qty"],
        })

    # 2) 卖优先: diff < -3% → SELL. 用释放的现金池给后续 BUY.
    projected_cash = cash
    for d in sorted(diffs, key=lambda x: x["diff_pct"]):
        if d["diff_pct"] > -_MIN_ORDER_DIFF_PCT:
            break
        if abs(d["diff_usd"]) < _MIN_ORDER_USD:
            continue
        if d["price"] <= 0:
            continue
        # 卖的数量: 减到 target 权重 (若 target=0 就全卖)
        sell_qty = int(-d["diff_usd"] / d["price"])
        sell_qty = min(sell_qty, d["qty_current"])
        if sell_qty <= 0:
            continue
        proceeds = sell_qty * d["price"]
        projected_cash += proceeds
        orders.append({
            "ticker": d["ticker"],
            "side": "SELL",
            "qty": sell_qty,
            "price": d["price"],
            "reason": f"rebalance: {d['current_pct']:.1f}% → {d['target_pct']:.1f}% (diff {d['diff_pct']:+.1f}pp)",
            "usd": round(proceeds, 0),
        })

    # 3) 买后置: 用现金池 (保留 cash floor) 从大到小满足 buy diff
    cash_floor = nav * _CASH_FLOOR_PCT / 100
    available = projected_cash - cash_floor
    for d in sorted(diffs, key=lambda x: -x["diff_pct"]):
        if d["diff_pct"] < _MIN_ORDER_DIFF_PCT:
            break
        if d["price"] <= 0:
            continue
        want_usd = min(d["diff_usd"], available)
        if want_usd < _MIN_ORDER_USD:
            continue
        buy_qty = int(want_usd / d["price"])
        if buy_qty <= 0:
            continue
        cost = buy_qty * d["price"]
        available -= cost
        orders.append({
            "ticker": d["ticker"],
            "side": "BUY",
            "qty": buy_qty,
            "price": d["price"],
            "reason": f"rebalance: {d['current_pct']:.1f}% → {d['target_pct']:.1f}% (diff {d['diff_pct']:+.1f}pp)",
            "usd": round(cost, 0),
        })
    return orders


# ── 主入口 ──────────────────────────────────────────────────────────────────
def check_and_execute_rebalance(window: str | None = None,
                                  dry_run: bool | None = None) -> dict:
    """orchestrator 每 cycle 末尾调用. 只在 pre-close 真跑."""
    if dry_run is None:
        dry_run = os.environ.get("AUTO_REBALANCE_ENABLED", "1") == "0"

    if window != "pre-close" and not dry_run:
        return {"status": "skipped_wrong_window", "window": window}

    positions, cash, nav = _load_current_positions()
    if nav <= 0:
        return {"status": "no_nav_data"}

    signals, prices = _load_signals()

    # P0 safeguard (2026-09-05): 若 fresh signals 数不足, **绝不 rebalance**.
    # 否则 target_pct 全 0 → 现有仓位 diff_pct=-current_pct → 生 SELL orders
    # 全量清仓 (48h+ 数据管道 outage 场景). See audit report P0-1.
    if len(signals) < _MIN_SIGNALS_FOR_REBALANCE:
        n_stale_positions = sum(1 for tk in positions if positions[tk].get("qty", 0) > 0)
        logger.warning(
            f"[auto_rebalance] SKIP: only {len(signals)} fresh signals "
            f"(< {_MIN_SIGNALS_FOR_REBALANCE} required). "
            f"{n_stale_positions} positions held. "
            f"Prevents phantom liquidation from data outage."
        )
        return {"status": "skipped_insufficient_signals",
                "n_signals": len(signals),
                "min_required": _MIN_SIGNALS_FOR_REBALANCE,
                "n_positions": n_stale_positions,
                "window": window,
                "dry_run": dry_run}

    # regime & drawdown
    regime = "neutral"
    try:
        rs = json.loads((Path(SIGNALS_DIR).parent / "regime_state.json").read_text(
            encoding="utf-8"))
        regime = rs.get("regime", "neutral")
    except Exception:
        # fallback: 从任意一个信号里拿
        for s in signals.values():
            if s.get("regime"):
                regime = s["regime"]
                break

    nav_path = Path(SIGNALS_DIR) / "nav_history.jsonl"
    peak = nav
    if nav_path.exists():
        try:
            for line in nav_path.read_text(encoding="utf-8").splitlines()[-30:]:
                p = float(json.loads(line).get("peak", 0))
                peak = max(peak, p)
        except Exception:
            pass
    drawdown_pct = (peak - nav) / peak * 100 if peak > 0 else 0

    liq_level, liq_triggers = _load_liquidity_state()
    chain_blocked = _load_chain_blocked_at()
    asia_repat_trigger, asia_repat_reason = _load_asia_repatriation_signal()
    sb_corr, sb_regime = _load_stock_bond_corr()
    # 事件驱动 triggers (仅当 AUTO_REBALANCE_EVENT_EXEC=1 才生效)
    import os as _os
    _event_exec = _os.environ.get("AUTO_REBALANCE_EVENT_EXEC", "0") == "1"
    event_triggers_fired = []
    if _event_exec:
        for trig in _load_event_triggers():
            if _check_event_trigger_cooldown(trig["name"]):
                event_triggers_fired.append({
                    "name": trig["name"],
                    "reason": trig["action"].get("reason"),
                    "triggered_value": trig.get("triggered_value"),
                })
    targets = compute_target_weights(signals, regime, drawdown_pct,
                                       chain_blocked_at=chain_blocked,
                                       liq_level=liq_level)
    orders = plan_rebalance(positions, cash, nav, targets, prices=prices)

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "window": window,
        "dry_run": dry_run,
        "nav": nav,
        "cash": cash,
        "drawdown_pct": round(drawdown_pct, 2),
        "regime": regime,
        "chain_blocked_at": chain_blocked,
        "liq_crisis_level": liq_level,
        "liq_triggers": liq_triggers,
        "asia_repat_trigger": asia_repat_trigger,
        "asia_repat_reason": asia_repat_reason,
        "spy_ief_60d_corr": sb_corr,
        "stock_bond_regime": sb_regime,
        "event_triggers_fired": event_triggers_fired,
        "auto_rebalance_event_exec": _event_exec,
        "targets": targets,
        "orders": orders,
        "n_orders": len(orders),
    }

    # 落盘 (即使 dry_run 也记录, 用来审计和回溯)
    try:
        with REBAL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if dry_run:
        logger.info(f"[rebalance] dry_run — {len(orders)} orders planned")
        for o in orders:
            logger.info(f"  {o['side']} {o['qty']} {o['ticker']} @ ${o['price']:.2f} "
                        f"(${o['usd']:,.0f}) — {o['reason']}")
        return result

    # 真执行: 用 paper_trader.submit_rebalance_order 直接下单, 不走 decision engine
    try:
        from paper_trader import submit_rebalance_order
    except Exception as exc:
        logger.error(f"[rebalance] paper_trader import failed: {exc}")
        return {**result, "status": "import_error"}

    submitted = []
    for o in orders:
        oid = submit_rebalance_order(
            ticker=o["ticker"], side=o["side"], qty=o["qty"],
            price=o["price"], reason=o["reason"],
        )
        if oid:
            submitted.append({**o, "order_id": oid})
        else:
            logger.warning(f"[rebalance] failed to submit {o['side']} {o['qty']} {o['ticker']}")

    return {**result, "status": "executed", "submitted": len(submitted),
            "submitted_orders": submitted}


# ── CLI ─────────────────────────────────────────────────────────────────────
def _cli_main():
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    r = check_and_execute_rebalance(window="pre-close",
                                     dry_run=dry or True)  # CLI 默认 dry-run
    print("=" * 80)
    print(f"NAV ${r['nav']:,.0f} - Cash ${r['cash']:,.0f} "
          f"({r['cash']/r['nav']*100:.1f}%) - Drawdown {r['drawdown_pct']:.1f}% - "
          f"Regime {r['regime']}")
    lvl = r.get('liq_crisis_level', 0)
    if lvl > 0:
        lvl_label = {1: '⚠ L1 warn', 2: '🔴 L2 bad', 3: '🚨 L3 extreme'}.get(lvl, '?')
        print(f"流动性危机等级: {lvl_label} · 触发: {', '.join(r.get('liq_triggers', []))}")
    if r.get('chain_blocked_at'):
        print(f"传导链断点: {r['chain_blocked_at']}")
    if r.get('asia_repat_trigger'):
        print(f"🇯🇵 Asia repatriation 触发: {r.get('asia_repat_reason')} → IEI × 0.6")
    if r.get('stock_bond_regime') in ('broken', 'extreme'):
        print(f"⚡ 股债相关 {r.get('spy_ief_60d_corr'):+.2f} = {r.get('stock_bond_regime')} (脆弱性 gauge) → 加 hedge × 1.2-1.4, 微减 bond × 0.85-0.92")
    _fired = r.get('event_triggers_fired', [])
    if _fired:
        print(f"🎯 事件驱动 triggers 激活 ({len(_fired)} 条):")
        for t in _fired:
            v = f" (值={t.get('triggered_value')})" if t.get('triggered_value') is not None else ""
            print(f"    [{t['name']}]{v}  {t.get('reason', '')}")
    elif r.get('auto_rebalance_event_exec'):
        print("🎯 事件驱动 triggers: 无激活 (阈值未破 或 cool-down 中)")
    else:
        print("🎯 事件驱动 triggers: 未启用 (set AUTO_REBALANCE_EVENT_EXEC=1 开启)")
    print("=" * 80)
    print("\nTarget weights:")
    for tk, w in sorted(r["targets"].items(), key=lambda x: -x[1]):
        print(f"  {tk:<6} {w:>5.1f}%")
    print(f"\nPlanned orders ({r['n_orders']}):")
    if not r["orders"]:
        print("  (无 — 所有偏差 < 3% 或 <$5K)")
    for o in r["orders"]:
        print(f"  {o['side']:<4} {o['qty']:>6} {o['ticker']:<6} "
              f"@ ${o['price']:.2f}  =${o['usd']:>8,.0f}  {o['reason']}")


if __name__ == "__main__":
    _cli_main()
