"""Deterministic A-share research only. No orders, models, downloads or notifications."""
from .core import DecisionInput, DecisionResult, RuleConfig, build_decision, render_report

__all__ = ["DecisionInput", "DecisionResult", "RuleConfig", "build_decision", "render_report"]
