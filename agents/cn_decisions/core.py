"""P0 contract and decision gate. Probabilities must come from a future validated model."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

SH = ZoneInfo("Asia/Shanghai")
ShanghaiTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(SH))]
Direction = Literal["强看多", "偏多", "中性", "偏空", "强看空", "证据不足"]
Action = Literal["做多候选", "观望", "数据不足"]


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RuleConfig(Frozen):
    version: str = Field(default="cn-research-p0-v1", pattern=r"^[A-Za-z0-9._-]{1,80}$")
    strong_bullish: FiniteFloat = 0.65
    bullish: FiniteFloat = 0.56
    bearish: FiniteFloat = 0.44
    strong_bearish: FiniteFloat = 0.35
    min_oos_samples: int = Field(default=200, ge=1)
    min_expected_net_return: FiniteFloat = Field(default=0.002, ge=0)
    max_staleness_minutes: int = Field(default=1440, ge=1)
    allow_shorting_a_shares: Literal[False] = False

    @model_validator(mode="after")
    def ordered(self):
        if not 0 <= self.strong_bearish < self.bearish < self.bullish < self.strong_bullish <= 1:
            raise ValueError("INVALID_RULE_THRESHOLDS")
        return self


class SourceRef(Frozen):
    id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=80)
    url: str = Field(pattern=r"^https://[^\s]+$", max_length=1000)
    published_at: ShanghaiTime
    available_at: ShanghaiTime

    @model_validator(mode="after")
    def available(self):
        if self.available_at < self.published_at:
            raise ValueError("SOURCE_NOT_YET_PUBLISHED")
        return self


class ValidationReport(Frozen):
    report_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    model_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    horizon_trading_days: Literal[1, 3, 5]
    train_label_end: ShanghaiTime
    calibration_start: ShanghaiTime
    calibration_end: ShanghaiTime
    test_start: ShanghaiTime
    test_end: ShanghaiTime
    available_at: ShanghaiTime
    oos_samples: int = Field(ge=0)
    oos_brier: FiniteFloat = Field(ge=0, le=1)
    baseline_brier: FiniteFloat = Field(ge=0, le=1)
    calibration_method: Literal["PLATT", "ISOTONIC"]
    purge_embargo_verified: bool
    is_synthetic: bool
    effective_sample_size_note: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def separated(self):
        if not (self.train_label_end < self.calibration_start <= self.calibration_end
                < self.test_start <= self.test_end <= self.available_at):
            raise ValueError("VALIDATION_OR_CALIBRATION_TIME_LEAK")
        return self


class Estimate(Frozen):
    raw_p_up: FiniteFloat = Field(ge=0, le=1)
    calibrated_p_up: FiniteFloat = Field(ge=0, le=1)
    expected_gross_return: FiniteFloat | None = None
    predicted_return_p05: FiniteFloat | None = None


class TargetWindow(Frozen):
    basis: Literal["RESEARCH_CLOSE_TO_CLOSE"] = "RESEARCH_CLOSE_TO_CLOSE"
    calendar_snapshot_id: str = Field(min_length=1, max_length=100)
    start: date
    end: date


class Factor(Frozen):
    id: str = Field(min_length=1, max_length=60)
    value: FiniteFloat


class DecisionInput(Frozen):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    market: Literal["CN_A"] = "CN_A"
    prediction_time: ShanghaiTime
    cutoff_time: ShanghaiTime
    data_available_at: ShanghaiTime | None = None
    horizon_trading_days: Literal[1, 3, 5]
    model_id: str = Field(default="UNTRAINED", pattern=r"^[A-Za-z0-9._-]{1,80}$")
    model_version: str = Field(default="0", pattern=r"^[A-Za-z0-9._-]{1,80}$")
    snapshot_values: dict[str, FiniteFloat] = Field(default_factory=dict, max_length=100)
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=30)
    target_window: TargetWindow | None = None
    estimate: Estimate | None = None
    validation: ValidationReport | None = None
    data_complete: bool = False
    risk_ok: bool = False
    executable: bool = False
    costs: FiniteFloat | None = Field(default=None, ge=0)
    is_mock_data: bool = False
    top_factors: tuple[Factor, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def point_in_time(self):
        if self.cutoff_time > self.prediction_time:
            raise ValueError("PREDICTION_PRECEDES_CUTOFF")
        if self.data_available_at is not None and self.data_available_at > self.cutoff_time:
            raise ValueError("DATA_TIME_LEAK")
        if any(max(ref.published_at, ref.available_at) > self.cutoff_time for ref in self.source_refs):
            raise ValueError("SOURCE_TIME_LEAK")
        if self.validation and self.validation.available_at > self.cutoff_time:
            raise ValueError("MODEL_REPORT_TIME_LEAK")
        if self.target_window and (self.target_window.start != self.cutoff_time.astimezone(SH).date()
                                   or self.target_window.end <= self.target_window.start):
            raise ValueError("INVALID_RESEARCH_TARGET_WINDOW")
        return self


def quality_flags(request: DecisionInput, config: RuleConfig) -> tuple[str, ...]:
    flags = []
    if request.is_mock_data:
        flags.append("MOCK_NOT_REAL_EVIDENCE")
    if not request.data_complete or not request.source_refs or not request.snapshot_values:
        flags.append("DATA_INCOMPLETE")
    if request.data_available_at is None:
        flags.append("AVAILABLE_AT_MISSING")
    elif request.prediction_time - request.data_available_at > timedelta(minutes=config.max_staleness_minutes):
        flags.append("DATA_STALE")
    if request.target_window is None:
        flags.append("CALENDAR_TARGET_UNVERIFIED")
    if request.estimate is None:
        flags.append("NO_CALIBRATED_MODEL")
    v = request.validation
    if v is None:
        flags.append("VALIDATION_REPORT_MISSING")
    else:
        if (v.model_id, v.model_version, v.horizon_trading_days) != (
                request.model_id, request.model_version, request.horizon_trading_days):
            flags.append("VALIDATION_IDENTITY_MISMATCH")
        if v.is_synthetic:
            flags.append("SYNTHETIC_VALIDATION")
        if not v.purge_embargo_verified:
            flags.append("OVERLAPPING_LABELS_UNVERIFIED")
        if v.oos_samples < config.min_oos_samples:
            flags.append("OOS_SAMPLES_INSUFFICIENT")
        if v.oos_brier >= v.baseline_brier:
            flags.append("NO_POSITIVE_OOS_EDGE")
    return tuple(flags)


def classify_direction(p_up: float | None, evidence_ok: bool, config: RuleConfig) -> Direction:
    if not evidence_ok or p_up is None:
        return "证据不足"
    if p_up >= config.strong_bullish:
        return "强看多"
    if p_up >= config.bullish:
        return "偏多"
    if p_up <= config.strong_bearish:
        return "强看空"
    if p_up <= config.bearish:
        return "偏空"
    return "中性"


def action(direction: Direction, net: float | None, risk_ok: bool, executable: bool,
           config: RuleConfig) -> Action:
    if direction == "证据不足":
        return "数据不足"
    if not risk_ok or not executable or net is None:
        return "观望"
    return "做多候选" if direction in ("偏多", "强看多") and net > config.min_expected_net_return else "观望"


class DecisionResult(DecisionInput):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^cn-p0-[a-f0-9]{32}$")
    data_snapshot_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    decision_rule_version: str
    rule_config: RuleConfig
    quality_flags: tuple[str, ...]
    evidence_ok: bool
    direction: Direction
    trade_action: Action
    raw_p_up: FiniteFloat | None
    calibrated_p_up: FiniteFloat | None
    p_up_ci: None = None
    expected_gross_return: FiniteFloat | None
    expected_net_return: FiniteFloat | None
    predicted_return_p05: FiniteFloat | None
    matched_historical_samples: int | None
    oos_samples: int | None
    effective_sample_size_note: str
    oos_brier: FiniteFloat | None
    oos_baseline_brier: FiniteFloat | None
    oos_edge_validated: bool
    invalidation_conditions: tuple[str, ...]

    @model_validator(mode="after")
    def derived_consistency(self):
        req = DecisionInput.model_validate({k: getattr(self, k) for k in DecisionInput.model_fields})
        expected = _derived(req, self.rule_config)
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError("MACHINE_RESULT_CONTRADICTION")
        return self


def _derived(req: DecisionInput, config: RuleConfig) -> dict:
    flags = quality_flags(req, config)
    # No numeric forecast escapes an invalid evidence gate, even if a caller supplied 0.7.
    est = req.estimate if not flags else None
    raw, calibrated = (est.raw_p_up, est.calibrated_p_up) if est else (None, None)
    gross = est.expected_gross_return if est else None
    # P0 has only close-to-close research labels, NOT a next-open execution model.
    # Even a caller's executable=True cannot turn that directional return into trade P&L.
    net = None
    direction = classify_direction(calibrated, not flags, config)
    snap = digest({k: req.model_dump(mode="json")[k] for k in (
        "symbol", "cutoff_time", "data_available_at", "snapshot_values", "source_refs", "data_complete", "is_mock_data")})
    identity = {"symbol": req.symbol, "snapshot": snap, "model": [req.model_id, req.model_version],
                "horizon": req.horizon_trading_days, "rules": config.model_dump(),
                "operational": [req.costs, req.risk_ok, req.executable], "quality": flags,
                "validation_ref": req.validation.report_id if req.validation else None,
                "target": req.target_window.model_dump(mode="json") if req.target_window else None}
    v = req.validation
    return dict(run_id="cn-p0-" + digest(identity)[7:39], data_snapshot_id=snap,
                decision_rule_version=config.version, quality_flags=flags, evidence_ok=not flags,
                direction=direction, trade_action=action(direction, net, req.risk_ok, req.executable, config),
                raw_p_up=raw, calibrated_p_up=calibrated, expected_gross_return=gross,
                expected_net_return=net, predicted_return_p05=est.predicted_return_p05 if est else None,
                matched_historical_samples=None, oos_samples=v.oos_samples if v else None,
                effective_sample_size_note=v.effective_sample_size_note if v else "未计算，不能按独立样本解释",
                oos_brier=v.oos_brier if v else None, oos_baseline_brier=v.baseline_brier if v else None,
                oos_edge_validated=not flags,
                invalidation_conditions=("数据、模型、目标窗口或规则变化须新建记录，不覆盖本次结果",
                                         "研究收盘到收盘口径不能当作下一可交易时点的净收益"))


def build_decision(request: DecisionInput, config: RuleConfig | None = None) -> DecisionResult:
    config = config or RuleConfig()
    return DecisionResult(**request.model_dump(), rule_config=config, **_derived(request, config))


def render_report(result: DecisionResult) -> str:
    # Revalidate serialized input: model_copy(update=...) must not bypass the result gate.
    result = DecisionResult.model_validate_json(result.model_dump_json())
    p = "未验证" if result.calibrated_p_up is None else f"{result.calibrated_p_up * 100:.2f}%"
    net = "未计算" if result.expected_net_return is None else f"{result.expected_net_return * 100:+.3f}%"
    target = result.target_window
    window = f"{target.start}→{target.end}，研究收盘到收盘" if target else "起止交易日尚未核验"
    sample = "未验证" if result.oos_samples is None else str(result.oos_samples)
    return "\n".join([
        f"结论｜{result.symbol}｜未来{result.horizon_trading_days}个交易日｜{result.direction}｜操作：{result.trade_action}",
        f"证据｜校准上涨概率：{p}；扣费期望：{net}；样本外条数：{sample}（不等于有效独立样本）。",
        "没过样本外和校准这关，我可不拿政策标题给你捏个胜率。" if not result.evidence_ok else
        "方向和能不能做是两码事。数是冻结的，嘴硬也不能改账。",
        "风险｜未训练/未验证的模型不能给方向；费用、停牌/涨跌停及可成交性须独立核验。",
        f"窗口｜{window}",
        f"审计｜{result.run_id}；预测 {result.prediction_time.astimezone(SH).isoformat()}；截止 {result.cutoff_time.astimezone(SH).isoformat()}",
        f"快照｜{result.data_snapshot_id}；模型 {result.model_id}@{result.model_version}；规则 {result.decision_rule_version}",
        "演示fixture，不是真实行情或回测成绩。" if result.is_mock_data else "研究记录，不是自动下单。",
    ])
