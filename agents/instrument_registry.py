"""instrument_registry.py — canonical ticker + metadata 单一源.

WP01 (audit 2026-09-19): 之前 ticker 元数据散落多处 (F01: sector_regime 的
TICKER_TO_SECTOR, F12: webui 的 LEVERAGED_OPTION_PRICE_MAP, 各种 hardcoded
杠杆倍数). 每加一个 ticker 要改多处, 且不同模块可能对同一 ticker 有不同判断.

本 module 是单一 registry:
- Instrument dataclass: canonical + display + asset class + leverage + proxies
- get(ticker): 归一化查找 (兼容 US.SOXL / SOXL / soxl)
- normalize(ticker): 只做 canonical 转换
- 现有 TICKER_TO_SECTOR / LEVERAGED_OPTION_PRICE_MAP 迁移: 从 registry 派生

## 设计原则

- **Frozen dataclass**: 元数据是 immutable, 修改需要新版本
- **Registry 是 default**: 未在 registry 里的 ticker 也能查询, 返 None
- **兼容 F01 归一化**: 裸 SOXL, US.SOXL, soxl 都指向同一 canonical "US.SOXL"

## 待迁移 (每个独立 commit)

- sector_regime.classify_ticker_sector: 从 registry.sector 派生
- webui.LEVERAGED_OPTION_PRICE_MAP: 从 registry.price_proxy / leverage 派生
- decision_agent.TICKER_TO_SECTOR: 同上
- config.LEVERAGE_FACTORS: 从 registry.leverage 派生
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Instrument:
    """Immutable instrument metadata. 修改需要新 registry 版本, 不能就地改."""
    canonical:      str                   # "US.SOXL" (源码里权威 key)
    display:        str                   # "SOXL" (UI / 用户输入)
    asset_class:    str                   # equity / leveraged_etf / bond / commodity / index_etf / sector_etf
    exchange:       str = "US"            # US / HK / JP; 决定 hours / calendar
    leverage:       float = 1.0           # 3.0 for TQQQ/SOXL
    price_proxy:    Optional[str] = None  # 目标价 anchor (SOXL 价格 = SOXX-anchored × 3)
    options_proxy:  Optional[str] = None  # 期权流代理 (SOXL 期权链薄, 用 SMH 更深)
    sector_bucket:  Optional[str] = None  # SMH / QQQ / GLD / IEI / XLV / USO (regime 判断用)
    is_short:       bool = False          # -3x / inverse
    notes:          str = ""              # 手写说明


# ─── Registry (tracked universe, alphabetical) ───────────────────────────
# 覆盖 signals/*_latest.json 里的所有 ticker + 常用未 tracked 但可能加入
# 的 (SPY / QQQ / SMH / IWM 作 sector 参考)
_REGISTRY: dict[str, Instrument] = {
    "US.AAPL":  Instrument("US.AAPL", "AAPL", "equity",
                             sector_bucket="QQQ",
                             notes="QQQ 权重最大股; 决策路径归 Nasdaq"),
    "US.AMAT":  Instrument("US.AMAT", "AMAT", "equity",
                             sector_bucket="SMH", notes="semi 设备; blacklist"),
    "US.CBRS":  Instrument("US.CBRS", "CBRS", "equity",
                             sector_bucket="SMH", notes="Cambricon 中国 AI 芯片; blacklist"),
    "US.DRAM":  Instrument("US.DRAM", "DRAM", "sector_etf",
                             sector_bucket="SMH", notes="内存 ETF; blacklist"),
    "US.ENPH":  Instrument("US.ENPH", "ENPH", "equity",
                             sector_bucket="QQQ", notes="Enphase 太阳能 inverter"),
    "US.GLD":   Instrument("US.GLD", "GLD", "commodity",
                             sector_bucket="GLD", notes="黄金 ETF 主标的; whitelist"),
    "US.GOOGL": Instrument("US.GOOGL", "GOOGL", "equity",
                             sector_bucket="QQQ", notes="whitelist cloud"),
    "US.IEI":   Instrument("US.IEI", "IEI", "bond",
                             sector_bucket="IEI",
                             notes="3-7 年国债 ETF; 2026-09-11 从 whitelist 移除 (CPI hot)"),
    "US.KLAC":  Instrument("US.KLAC", "KLAC", "equity",
                             sector_bucket="SMH", notes="semi 设备; blacklist"),
    "US.LITE":  Instrument("US.LITE", "LITE", "equity",
                             sector_bucket="SMH", notes="光通信; blacklist"),
    "US.MPWR":  Instrument("US.MPWR", "MPWR", "equity",
                             sector_bucket="SMH", notes="Monolithic Power; blacklist 2026-09-18"),
    "US.MSFT":  Instrument("US.MSFT", "MSFT", "equity",
                             sector_bucket="QQQ", notes="whitelist cloud pricing power"),
    "US.MU":    Instrument("US.MU", "MU", "equity",
                             sector_bucket="SMH", notes="Micron 内存; blacklist"),
    "US.MULL":  Instrument("US.MULL", "MULL", "leveraged_etf", leverage=2.0,
                             price_proxy="US.MU", options_proxy="US.MU",
                             sector_bucket="SMH", notes="2x MU; blacklist"),
    "US.NBIS":  Instrument("US.NBIS", "NBIS", "equity",
                             sector_bucket="SMH",
                             notes="Nebius AI cloud; 2026-09-11 从 whitelist 移除 (soft blacklist)"),
    "US.NVDA":  Instrument("US.NVDA", "NVDA", "equity",
                             sector_bucket="SMH", notes="AI 芯片龙头; blacklist"),
    "US.QRVO":  Instrument("US.QRVO", "QRVO", "equity",
                             sector_bucket="SMH", notes="Qorvo RF; blacklist 2026-09-18"),
    "US.SHY":   Instrument("US.SHY", "SHY", "bond",
                             sector_bucket="IEI",
                             notes="1-3 年国债 ETF; 2026-09-11 soft blacklist"),
    "US.SOXL":  Instrument("US.SOXL", "SOXL", "leveraged_etf", leverage=3.0,
                             price_proxy="US.SOXX", options_proxy="US.SMH",
                             sector_bucket="SMH",
                             notes="3x semi; blacklist; price=SOXX×3, options=SMH (更深)"),
    "US.SOXS":  Instrument("US.SOXS", "SOXS", "leveraged_etf", leverage=3.0,
                             is_short=True,
                             price_proxy="US.SOXX", options_proxy="US.SMH",
                             sector_bucket="SMH", notes="-3x semi inverse; blacklist"),
    "US.STM":   Instrument("US.STM", "STM", "equity",
                             sector_bucket="SMH", notes="STMicroelectronics; blacklist 2026-09-18"),
    "US.SWKS":  Instrument("US.SWKS", "SWKS", "equity",
                             sector_bucket="SMH", notes="Skyworks RF; blacklist 2026-09-18"),
    "US.TLT":   Instrument("US.TLT", "TLT", "bond",
                             sector_bucket="TLT", notes="20+ 年国债 ETF"),
    "US.TQQQ":  Instrument("US.TQQQ", "TQQQ", "leveraged_etf", leverage=3.0,
                             price_proxy="US.QQQ", options_proxy="US.QQQ",
                             sector_bucket="QQQ",
                             notes="3x Nasdaq; price/options 都用 QQQ (更深)"),
    "US.TSLA":  Instrument("US.TSLA", "TSLA", "equity",
                             sector_bucket="QQQ"),
    "US.USO":   Instrument("US.USO", "USO", "commodity",
                             sector_bucket="USO", notes="原油 ETF"),
    "US.VST":   Instrument("US.VST", "VST", "equity",
                             sector_bucket="XLV",
                             notes="Vistra 电力; AI datacenter 电耗 proxy"),
    "US.XLV":   Instrument("US.XLV", "XLV", "sector_etf",
                             sector_bucket="XLV", notes="医疗防御; whitelist"),
    # Reference-only (backtest / sector 参考, 非直接交易 universe)
    "US.SPY":   Instrument("US.SPY", "SPY", "index_etf",
                             sector_bucket="SPY", notes="S&P 500 参考"),
    "US.QQQ":   Instrument("US.QQQ", "QQQ", "index_etf",
                             sector_bucket="QQQ", notes="Nasdaq 100 参考"),
    "US.SMH":   Instrument("US.SMH", "SMH", "sector_etf",
                             sector_bucket="SMH", notes="半导体 ETF; SOXL options proxy"),
    "US.SOXX":  Instrument("US.SOXX", "SOXX", "sector_etf",
                             sector_bucket="SMH", notes="半导体 ETF; SOXL price anchor"),
    # Additional leveraged ETFs (存在于 config.LEVERAGE_FACTORS, 未主动 tracked
    # 但 leverage 判断要覆盖 — 防 SOXL 一天 -5% 误触发 crisis)
    "US.TECL":  Instrument("US.TECL", "TECL", "leveraged_etf", leverage=3.0,
                             sector_bucket="QQQ", notes="3x tech"),
    "US.UPRO":  Instrument("US.UPRO", "UPRO", "leveraged_etf", leverage=3.0,
                             sector_bucket="SPY", notes="3x S&P"),
    "US.SQQQ":  Instrument("US.SQQQ", "SQQQ", "leveraged_etf", leverage=3.0,
                             is_short=True, sector_bucket="QQQ",
                             notes="-3x Nasdaq inverse"),
    "US.SPXU":  Instrument("US.SPXU", "SPXU", "leveraged_etf", leverage=3.0,
                             is_short=True, sector_bucket="SPY",
                             notes="-3x S&P inverse"),
    "US.NVDU":  Instrument("US.NVDU", "NVDU", "leveraged_etf", leverage=2.0,
                             price_proxy="US.NVDA", sector_bucket="SMH",
                             notes="2x NVDA long"),
    "US.NVDX":  Instrument("US.NVDX", "NVDX", "leveraged_etf", leverage=2.0,
                             price_proxy="US.NVDA", sector_bucket="SMH",
                             notes="2x NVDA long alternate"),
    "US.NVDD":  Instrument("US.NVDD", "NVDD", "leveraged_etf", leverage=1.5,
                             is_short=True, price_proxy="US.NVDA",
                             sector_bucket="SMH", notes="-1.5x NVDA inverse"),
    "US.TSLL":  Instrument("US.TSLL", "TSLL", "leveraged_etf", leverage=1.5,
                             price_proxy="US.TSLA", sector_bucket="QQQ",
                             notes="1.5x TSLA long"),
    "US.TSLZ":  Instrument("US.TSLZ", "TSLZ", "leveraged_etf", leverage=2.0,
                             is_short=True, price_proxy="US.TSLA",
                             sector_bucket="QQQ", notes="-2x TSLA inverse"),
    "US.TSLQ":  Instrument("US.TSLQ", "TSLQ", "leveraged_etf", leverage=2.0,
                             is_short=True, price_proxy="US.TSLA",
                             sector_bucket="QQQ", notes="-2x TSLA inverse alt"),
    "US.AAPU":  Instrument("US.AAPU", "AAPU", "leveraged_etf", leverage=2.0,
                             price_proxy="US.AAPL", sector_bucket="QQQ",
                             notes="2x AAPL long"),
    "US.GGLL":  Instrument("US.GGLL", "GGLL", "leveraged_etf", leverage=2.0,
                             price_proxy="US.GOOGL", sector_bucket="QQQ",
                             notes="2x GOOGL long"),
}


# ─── Public API ──────────────────────────────────────────────────────────

def normalize(ticker: str) -> str:
    """Return canonical form. Accepts SOXL / US.SOXL / soxl → 'US.SOXL'.

    未在 registry 里也返回 US.-prefixed uppercase; caller 可以再 get() 判是否已知.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return ""
    # 已经有市场前缀就保留
    for pfx in ("US.", "HK.", "JP."):
        if t.startswith(pfx):
            return t
    # 否则默认 US.
    return f"US.{t}"


def get(ticker: str) -> Optional[Instrument]:
    """Look up Instrument by any form. 未知 ticker 返 None."""
    if not ticker:
        return None
    canonical = normalize(ticker)
    return _REGISTRY.get(canonical)


def list_all() -> list[Instrument]:
    """所有 registered instruments, 字母序."""
    return sorted(_REGISTRY.values(), key=lambda i: i.canonical)


def list_by_class(asset_class: str) -> list[Instrument]:
    """按 asset_class 过滤 (equity / leveraged_etf / bond / commodity / etc)."""
    return [i for i in _REGISTRY.values() if i.asset_class == asset_class]


def list_by_sector(sector_bucket: str) -> list[Instrument]:
    """按 sector_bucket 过滤 (SMH / QQQ / GLD / IEI / etc)."""
    return [i for i in _REGISTRY.values() if i.sector_bucket == sector_bucket]


def ticker_to_sector_map() -> dict[str, str]:
    """Derived: {canonical → sector_bucket}. 供旧 TICKER_TO_SECTOR 迁移用."""
    return {i.canonical: i.sector_bucket for i in _REGISTRY.values()
             if i.sector_bucket}


def leveraged_option_price_map() -> dict[str, dict]:
    """Derived: {display → {source, leverage, options_proxy}}. 供旧
    LEVERAGED_OPTION_PRICE_MAP 迁移用.

    只包含 leverage != 1.0 的; 且必须有 price_proxy.
    """
    out = {}
    for i in _REGISTRY.values():
        if i.leverage == 1.0 or not i.price_proxy:
            continue
        proxy_display = _REGISTRY.get(i.price_proxy)
        source = proxy_display.display if proxy_display else i.price_proxy.replace("US.", "")
        out[i.display] = {
            "source":   source,
            "leverage": i.leverage,
        }
    return out


def is_leveraged(ticker: str) -> bool:
    """True if leverage != 1.0."""
    inst = get(ticker)
    return bool(inst and inst.leverage != 1.0)


def leverage_of(ticker: str) -> float:
    """Return leverage (1.0 if unknown / not leveraged)."""
    inst = get(ticker)
    return inst.leverage if inst else 1.0


def leverage_factors_map() -> dict[str, float]:
    """Derived: {canonical → leverage} for all instruments with leverage != 1.0.
    供 config.LEVERAGE_FACTORS 迁移用."""
    return {i.canonical: i.leverage for i in _REGISTRY.values() if i.leverage != 1.0}
