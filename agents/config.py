import json
import os
from datetime import datetime as _dt
from pathlib import Path

# ── 本地敏感配置（不入 git）─────────────────────────────────────────────
# 优先级：环境变量 > secrets.local.json > secrets.example.json（占位） > 空
# secrets.local.json 应放在 agents/ 目录下，被 .gitignore 排除
_BASE = Path(__file__).parent
_SECRETS: dict = {}
# 先加载 example（占位/默认），再 local 覆盖（real values）
for _name in ("secrets.example.json", "secrets.local.json"):
    _p = _BASE / _name
    if _p.exists():
        try:
            _SECRETS = {**_SECRETS, **json.loads(_p.read_text(encoding="utf-8"))}
        except Exception:
            pass


def _cfg(key: str, default=""):
    """优先环境变量；缺则读 secrets.local.json；最后默认值。"""
    v = os.environ.get(key)
    if v:
        return v
    return _SECRETS.get(key, default)


# --- Trading account safety profile ---
# 本项目当前只支持 moomoo SIMULATE。积极交易模式必须同时满足这个硬编码账户
# 类型和显式环境变量，避免将来接入 REAL 账户时意外继承放宽后的模拟仓参数。
TRADING_ACCOUNT_MODE = "SIMULATE"


def is_sim_active_trading() -> bool:
    """Whether the simulation-only active trading profile is enabled."""
    return (
        TRADING_ACCOUNT_MODE == "SIMULATE"
        and os.environ.get("TRADER_SIM_ACTIVE", "0") == "1"
    )


# --- Tickers ---
# DRAM/存储板块（2026-06-17/18 加）：
#   US.DRAM = Roundhill Memory ETF（存储芯片板块 ETF，1x）
#   US.MULL = 2x MU 杠杆 ETF（Micron 单股杠杆暴露）
# LEVERAGED_PAIRS 配置防止 MU↔MULL 同时持仓叠杠杆（见 paper_trader.py）
TICKERS = ["US.TQQQ", "US.SOXL", "US.DRAM", "US.MULL"]

# --- Asset class (per-class calibration & TA weight lookup) ---
# 不同资产类别对相同 TA 信号的反应完全不同：
#   commodity（黄金/石油）— 结构性趋势强，均值回归信号（RSI/CCI/BB 超买）容易失效
#   equity_leveraged（3x ETF）— 高波动 mean-revert 强，超买/超卖信号有效
#   bond（IEI/SHY）— 低波动，趋势信号权重高，超买信号几乎无效
#   equity_single — 中等波动，all signals modest weight
# 用于 _calibrate_confidence.py 分组校准 + confluence.py 按 class 查权重
ASSET_CLASS_MAP = {
    "US.GLD":  "commodity",
    "US.USO":  "commodity",   # oil ETF, if added
    "US.SHY":  "bond",
    "US.IEI":  "bond",
    "US.TQQQ": "equity_leveraged",
    "US.SOXL": "equity_leveraged",
    "US.MULL": "equity_leveraged",  # 2x MU
    "US.DRAM": "equity_single",
    # 剩余单股默认 equity_single（NVDA/MSFT/AAPL/NBIS/LITE 等）
}
DEFAULT_ASSET_CLASS = "equity_single"

def get_asset_class(ticker: str) -> str:
    """从 ticker 推 asset_class；未映射的默认 equity_single。"""
    if not ticker:
        return DEFAULT_ASSET_CLASS
    tk = ticker if ticker.startswith("US.") else f"US.{ticker}"
    return ASSET_CLASS_MAP.get(tk, DEFAULT_ASSET_CLASS)

# 用户显式加入的跟踪标的。展示类型不决定交易资格：这里的每个美股/ETF
# 都会参与 scan，并可在 SIMULATE 账户按统一信号和风控规则买入。
# 动态 universe picks 只负责发现列表外的额外卫星票。
#   NVDA — 半导体链条领头，看它就知道 SOXL/DRAM 方向
#   MSFT — 云 AI 大客户 + FAANG 权重股（TQQQ 组成）
#   AAPL — TQQQ/QQQ 最大权重股，苹果链跟踪
TRACKED_TICKERS = [
    # AI/tech 权重股
    "US.NVDA",   # 半导体链条领头
    "US.MSFT",   # 云 AI + FAANG
    "US.AAPL",   # QQQ 最大权重
    # 2026-Q3 thesis 新增：AI 云 pure-play + 债券对冲（详见 memory/project_thesis_2026Q3.md）
    "US.NBIS",   # Nebius AI cloud pure-play — 云端消费增长表达
    "US.SHY",    # 1-3Y Treasury ETF — 2Y 债券多仓代理
    "US.IEI",    # 3-7Y Treasury ETF — 5Y 债券多仓代理
    # AI DC 光通信链
    "US.LITE",   # Lumentum — 400G/800G 光模块 / laser diode, NVIDIA/Cisco 光互联供应
    # AI 芯片新势力（NVDA 竞品）
    "US.CBRS",   # Cerebras Systems — WSE 晶圆级芯片, AI inference 替代方案, 2025 IPO
    # 大宗商品（macro / inflation proxy）
    "US.USO",    # WTI 原油 ETF — 通胀 proxy + geopolitics gauge，也用于 gold_macro 输入
]

# 单一交易资格源：以后只需把标的加入 TICKERS 或 TRACKED_TICKERS，
# 调度、仓位和轮换模块都会自动识别，不再维护独立白名单。
TRADE_ELIGIBLE_TICKERS = frozenset((*TICKERS, *TRACKED_TICKERS, "US.GLD"))

# --- Leverage Factors ---
# 杠杆 ETF 单日波动是标的的 N 倍 → 各种"暴跌/暴涨"绝对阈值要按倍数缩放
# 否则 SOXL 一天 -5% (正常 1σ 波动) 就误触发 crisis
LEVERAGE_FACTORS = {
    "US.TQQQ": 3.0, "US.SOXL": 3.0, "US.TECL": 3.0, "US.UPRO": 3.0,
    "US.MULL": 2.0, "US.NVDU": 2.0, "US.NVDX": 2.0,
    "US.TSLL": 1.5, "US.AAPU": 2.0, "US.GGLL": 2.0,
    # 反向杠杆（绝对值看波动幅度一样）
    "US.SQQQ": 3.0, "US.SOXS": 3.0, "US.SPXU": 3.0,
    "US.NVDD": 1.5, "US.TSLZ": 2.0, "US.TSLQ": 2.0,
    # 默认 1x（正常股票/ETF）
}

# --- moomoo OpenD ---
OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111
MOOMOO_ACC_ID = int(_cfg("MOOMOO_ACC_ID", "0"))   # SIMULATE 账户 ID
MOOMOO_ACC_ID_JP = int(_cfg("MOOMOO_ACC_ID_JP", "0"))   # 日股 SIMULATE 账户

# --- OpenAI ---
OPENAI_API_KEY = _cfg("OPENAI_API_KEY", "")
DECISION_MODEL = "gpt-4o-mini"

# --- FRED (Federal Reserve Economic Data) ---
# 免费申请: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY = _cfg("FRED_API_KEY", "")

# --- Scheduler (日K交易者模式) ---
# 每天 5 个 ET 时间窗口；trader 只在 TRADE_WINDOWS 里的几个下单
# pre-open 仅做"选股 + evolver"不下单；pre-market 仅紧急减仓 (严门槛)
DAILY_WINDOWS_ET = [
    (8*60+30,  8*60+45,  "pre-market"), # 08:30-08:45 ET  盘前 (严门槛 + 大 buffer + RTH 外撮合)
    (9*60+20,  9*60+35,  "pre-open"),   # 09:20-09:35 ET  开盘前扫描 (选 picks 不交易)
    (10*60,    10*60+15, "post-open"),  # 10:00-10:15 ET  开盘后跟踪
    (12*60,    12*60+15, "midday"),     # 12:00-12:15 ET  午盘跟踪
    (15*60+45, 16*60,    "pre-close"),  # 15:45-16:00 ET  收盘前确认
]

# --- Technical thresholds ---
RSI_OVERBOUGHT = 78       # REDUCE_RISK trigger
RSI_EXTREME_OB = 82       # Strong REDUCE_RISK
RSI_OVERSOLD = 38

VOL_DIVERGE_WARN = 0.78   # new high but vol < this ratio = divergence
VOL_DIVERGE_STRONG = 0.70 # strong divergence threshold

# --- Paths ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_DIR = os.path.join(BASE_DIR, "signals")
# 按日切割的 log（UTF-8，由 Python 的 FileHandler 直接写，避免 PowerShell 双写导致编码错乱）
_LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

def get_today_log_path():
    """返回当前 UTC/本地日期对应的 log 文件路径，跨午夜自动切换。"""
    return os.path.join(_LOG_DIR, f"run_{_dt.now().strftime('%Y%m%d')}.log")

# 保留 LOG_PATH 作为启动时刻快照，仅用于向后兼容旧代码；
# 真正写入应使用 get_today_log_path() 或 notifier 里的 _DailyLogHandler
LOG_PATH = get_today_log_path()
