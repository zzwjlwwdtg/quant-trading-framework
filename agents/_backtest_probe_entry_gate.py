"""EVO-20261118-1：双 AI Gate 联合状态的研究辅助函数。

这里只分类历史证据，不调用交易代码，也不改变下单行为。
每个 Gate 必须提供当时的 action、confidence 和适用的 confidence_min；
缺失或非法数据标为 UNKNOWN，不推断为明确赞成或反对。
"""
import math


明确买入 = frozenset(("BUY", "STRONG_BUY"))
明确反对 = frozenset(("HOLD", "SELL", "WATCH_BUY"))


def gate_state(action, confidence, confidence_min):
    """返回 BUY、STRONG_BUY、HOLD、SELL、WATCH_BUY、LOW_CONF 或 UNKNOWN。"""
    if not isinstance(action, str):
        return "UNKNOWN"
    action = action.strip().upper()
    if action not in 明确买入 | 明确反对:
        return "UNKNOWN"
    if isinstance(confidence, bool) or isinstance(confidence_min, bool):
        return "UNKNOWN"
    try:
        confidence = float(confidence)
        confidence_min = float(confidence_min)
    except (TypeError, ValueError, OverflowError):
        return "UNKNOWN"
    if (not math.isfinite(confidence) or not math.isfinite(confidence_min)
            or confidence < 0 or confidence_min <= 0):
        return "UNKNOWN"
    if confidence < confidence_min:
        return "LOW_CONF"
    return action


def joint_state(claude, codex):
    """返回联合分桶及候选门的影子判定；证据不足时判定为 None。

    输入分别为含 action、confidence、confidence_min 的字典。此函数不将
    APPROVE、服务故障或规则引擎动作擅自解释成另一个 Gate 的买入意见。
    """
    def classify(gate):
        if not isinstance(gate, dict):
            return "UNKNOWN"
        return gate_state(
            gate.get("action"), gate.get("confidence"), gate.get("confidence_min")
        )

    left, right = classify(claude), classify(codex)
    bucket = f"{left}/{right}"
    if "UNKNOWN" in (left, right):
        return {"bucket": bucket, "would_block": None}
    opposing = 明确反对 | {"LOW_CONF"}
    would_block = (
        (left in opposing and right not in 明确买入)
        or (right in opposing and left not in 明确买入)
    )
    return {"bucket": bucket, "would_block": would_block}
