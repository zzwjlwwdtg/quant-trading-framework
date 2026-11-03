"""F10 (MODEL_AUDIT 2026-09-19): 校准 / HMM 版本指纹.

只做"可追溯": 给决策时用到的校准文件与 HMM 状态一个确定性 id, 并记录到
决策结果 (decision["model_versions"]) 与 trade_log. 不改变任何决策逻辑,
不触发重训 (审计: 未重新校准不自动证明参数失效).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional


# 先冻结案例；不在这里重新实现故障分类、数据健康检查或交易规则。
_ACCEPTANCE_CASES = (
    ("dual_model_unavailable", "双模型不可用"),
    ("symbol_not_found", "符号不存在"),
    ("etf_fundamentals_missing", "ETF基本面缺失"),
    ("pre_fix_price_mismatch", "修复前价格不一致事件"),
    ("mixed_old_worker", "旧子进程混跑"),
)
_EVIDENCE_LABELS = {
    "historical": "历史事件",
    "current_reproduction": "当前版本复现",
    "normal_degradation": "正常降级",
    "unknown": "证据不足（未知）",
}


def _complete_identity(value: Any) -> bool:
    """只检查证据字段完整性；不能据此证明对应进程实际执行了该版本。"""
    required = ("run_id", "code_version", "started_at",
                "config_digest", "entrypoint")
    return isinstance(value, dict) and all(
        isinstance(value.get(key), str) and value[key].strip()
        for key in required
    )


def runtime_acceptance_summary(document: Any) -> dict:
    """只读解释外部核对材料，不把 merged 或磁盘源码当作运行证明。

    输入约定：
    current_runtime 为已核对的主进程身份；cases 为案例编号到证据的映射。
    每条案例包含 runtime、fix_version、expected_result、observed_result、
    fault_classification、data_health_result、source 和 relation。
    relation 只能引用核对材料已有的 historical/current_reproduction/
    normal_degradation；本函数不根据异常文本或价格自行推断故障类别。
    """
    document = document if isinstance(document, dict) else {}
    current = document.get("current_runtime")
    cases = document.get("cases")
    cases = cases if isinstance(cases, dict) else {}
    rows = []
    for case_id, name in _ACCEPTANCE_CASES:
        evidence = cases.get(case_id)
        evidence = evidence if isinstance(evidence, dict) else {}
        runtime = evidence.get("runtime")
        relation = evidence.get("relation")
        required = ("fix_version", "expected_result", "observed_result",
                    "fault_classification", "data_health_result", "source")
        complete = (
            _complete_identity(current)
            and _complete_identity(runtime)
            and all(isinstance(evidence.get(key), str)
                    and evidence[key].strip() for key in required)
        )
        status = "unknown"
        reason = "运行身份、修复映射或现有分类依据缺失"
        if complete:
            # 同一 run_id 的身份字段冲突时也不能认定为当前版本。
            same_runtime = all(
                runtime[key] == current[key]
                for key in ("run_id", "code_version", "started_at",
                            "config_digest", "entrypoint")
            )
            if relation == "historical":
                # 不按版本字符串排序，也不把“不同版本”自动等同于历史。
                try:
                    event_time = datetime.fromisoformat(runtime["started_at"])
                    current_time = datetime.fromisoformat(current["started_at"])
                    historical = (
                        event_time.utcoffset() is not None
                        and current_time.utcoffset() is not None
                        and event_time < current_time
                        and runtime["run_id"] != current["run_id"]
                    )
                except (TypeError, ValueError):
                    historical = False
                if historical:
                    status = relation
                    reason = "核对材料引用的运行早于当前运行；不代表修复已验收"
            elif same_runtime and relation == "current_reproduction":
                if evidence["observed_result"] != evidence["expected_result"]:
                    status = relation
                    reason = "当前运行的引用结果与冻结预期不一致"
            elif same_runtime and relation == "normal_degradation":
                if evidence["observed_result"] == evidence["expected_result"]:
                    status = relation
                    reason = "当前运行的引用结果符合已有降级预期"
            if status == "unknown":
                reason = "运行关联、时间关系或引用结果不能支持所给分类"
        rows.append({
            "case_id": case_id, "name": name,
            "status": status, "label": _EVIDENCE_LABELS[status],
            "reason": reason,
            "fix_version": evidence.get("fix_version"),
            "runtime": runtime if isinstance(runtime, dict) else None,
            "source": evidence.get("source"),
        })
    return {
        "current_runtime": current if _complete_identity(current) else None,
        "cases": rows,
        # 本阶段只展示材料，不能替代两个完整交易日的逐批核对和基线比较。
        "accepted": False,
        "acceptance_status": "未知：尚未核实两个完整交易日及订单意图基线一致性",
    }


def runtime_acceptance_lines(path: Path) -> list[str]:
    """读取指定核对材料；不写 signals，不读取密钥，不启动任何任务。"""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        warning = None
    except (OSError, ValueError, UnicodeError):
        document = {}
        warning = "告警：运行验收材料缺失或不可读，运行身份与验收结果均为未知"
    summary = runtime_acceptance_summary(document)
    lines = ["  只读核对：以下为引用材料，未独立验证运行进程；merged 不代表已验收"]
    if warning:
        lines.append(f"  {warning}")
    identity = summary["current_runtime"]
    if identity:
        lines.append("  材料中的主进程身份: " + json.dumps(
            identity, ensure_ascii=False, sort_keys=True))
    else:
        lines.append("  告警：主进程版本、启动时间、配置摘要或入口缺失")
    for row in summary["cases"]:
        lines.append(f"  {row['name']}: {row['label']}；{row['reason']}")
        if row["runtime"]:
            lines.append("    材料中的执行身份: " + json.dumps(
                row["runtime"], ensure_ascii=False, sort_keys=True))
        if row["fix_version"] or row["source"]:
            lines.append(
                f"    修复版本={row['fix_version']}；依据={row['source']}")
    lines.append("  告警：子进程覆盖、所有决策批次关联及观察期完整性尚未独立核实")
    lines.append(f"  验收状态: {summary['acceptance_status']}")
    return lines


from datetime import datetime


def _plain(o: Any) -> Any:
    """MappingProxyType / tuple / numpy 标量 → 纯 JSON 结构 (context 快照是只读包装)."""
    if isinstance(o, (dict, MappingProxyType)):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            return o.item()
        except Exception:
            pass
    return o


def fingerprint(obj: Any, n: int = 12) -> str:
    raw = json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def calibration_version(data: Optional[Any]) -> dict:
    if not data:
        return {"id": None, "status": "missing"}
    d = _plain(data)
    return {"id": f"calib-{fingerprint(d)}", "trained_at": d.get("ts"),
            "n_tickers": len(d.get("tickers") or []),
            "lookback_days": d.get("lookback_days"), "forward_days": d.get("forward_days")}


def hmm_version(info: Optional[Any]) -> dict:
    if not info:
        return {"id": None, "status": "missing"}
    d = _plain(info)
    vid = d.get("version_id")
    return {"id": vid or f"hmm-legacy-{fingerprint(d)}",
            "frozen": bool(vid),
            "trained_at": d.get("ts_utc") or d.get("ts"),
            "train_start": d.get("train_start"), "train_end": d.get("train_end"),
            "label": d.get("current_label"), "prob": d.get("current_prob")}
