"""EVO-20261104-1：止损距离研究用纯函数，不接入交易路径。

方案须在查看样本及结果前完成登记并冻结；本文件本身不构成预注册证明。
输入须由独立的数据准备过程提供，并保留原始数据、提取方法及内容哈希。
"""
from datetime import date
from math import isfinite
from statistics import median

TRAIN_START = date(2024, 1, 1)
TRAIN_END = date(2026, 5, 31)
OOS_START = date(2026, 6, 1)
OOS_END = date(2026, 10, 31)
CANDIDATES = (0.01, 0.015, 0.02)
MIN_SAMPLES = 30


def _sample(row, start, end):
    """校验一笔已完整平仓的历史买入；距离和收益均使用小数比例。"""
    if not isinstance(row, dict):
        raise ValueError("样本必须是字典")
    try:
        sample_id = row["sample_id"]
        entry_date = date.fromisoformat(row["entry_date"])
        exit_date = date.fromisoformat(row["exit_date"])
        entry = float(row["entry_price"])
        stop = float(row["stop_line"])
        pnl = float(row["pnl_pct"])
        exit_reason = row["exit_reason"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("样本字段缺失或格式错误") from exc
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ValueError("样本必须有非空唯一标识")
    if not start <= entry_date <= end or exit_date < entry_date:
        raise ValueError("样本日期不符合研究区间")
    if not all(isfinite(x) for x in (entry, stop, pnl)) or entry <= 0 or stop <= 0:
        raise ValueError("价格及收益必须是有限数，价格必须为正")
    if exit_reason not in ("stop", "signal_sell"):
        raise ValueError("退出原因必须是实际止损或信号卖出")
    return {
        "sample_id": sample_id,
        "distance": (entry - stop) / entry,
        "pnl_pct": pnl,
    }


def _prepare(rows, start, end):
    samples = [_sample(row, start, end) for row in rows]
    ids = [row["sample_id"] for row in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("样本标识重复")
    return samples


def _stats(samples, threshold):
    values = [row["pnl_pct"] for row in samples if row["distance"] < threshold]
    n = len(values)
    return {
        "n": n,
        "avg_pnl": sum(values) / n if n else None,
        "median_pnl": median(values) if n else None,
        "win_rate": sum(value > 0 for value in values) / n if n else None,
    }


def _passes(stats):
    return (
        stats["n"] >= MIN_SAMPLES
        and stats["avg_pnl"] < 0
        and stats["win_rate"] < 0.45
    )


def select_training(rows):
    """只用训练集选参：合格候选中平均收益最低；并列取较小阈值。

    结果必须连同训练数据哈希、代码版本和预注册记录一起独立冻结，
    然后才可查看 OOS。没有合格候选时停止，不进入 OOS 选参。
    """
    samples = _prepare(rows, TRAIN_START, TRAIN_END)
    results = {threshold: _stats(samples, threshold) for threshold in CANDIDATES}
    eligible = [threshold for threshold in CANDIDATES if _passes(results[threshold])]
    selected = min(eligible, key=lambda threshold: (results[threshold]["avg_pnl"], threshold)) if eligible else None
    return {"selected_threshold": selected, "training": results}


def evaluate_oos(rows, frozen_threshold):
    """只检验已冻结阈值；OOS 不比较候选、不重新选参。

    返回研究判定，不授权启用交易规则。调用方须另行核对冻结记录、
    数据来源与样本完整性，并将完整判定保存到 backtest_verdicts。
    """
    if frozen_threshold not in CANDIDATES:
        raise ValueError("必须提供训练阶段冻结的候选阈值")
    samples = _prepare(rows, OOS_START, OOS_END)
    stats = _stats(samples, frozen_threshold)
    return {
        "selected_threshold": frozen_threshold,
        "oos": stats,
        "oos_pass": _passes(stats),
        "trading_enabled": False,
    }
