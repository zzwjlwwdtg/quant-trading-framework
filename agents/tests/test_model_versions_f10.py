"""F10 (MODEL_AUDIT 2026-09-19): 校准 / HMM 版本可追溯.

要求:
1. 校准文件、HMM 状态各有确定性指纹 (内容变 → id 变; 同内容 → 同 id).
2. HMM 训练产物冻结: 训练窗口、特征、超参、模型参数 (startprob/transmat/
   means/covars) 都写进 hmm_state.json, 并追加 hmm_versions.jsonl 历史;
   用冻结参数能在同一输入上重放出相同的推断.
3. 每个决策结果带 model_versions (用了哪个校准 / 哪个 HMM 版本), trade_log
   记录它. backtest context 下不读 live 文件.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import model_versions as mv

CAL = {"ts": "2026-08-11T18:16:39", "tickers": ["TQQQ", "SOXL"], "bull_weights": {"a": 1.0},
       "lookback_days": 250, "forward_days": 5}


class Fingerprints(unittest.TestCase):
    def test_calibration_id_deterministic_and_content_sensitive(self):
        a = mv.calibration_version(CAL)
        b = mv.calibration_version(json.loads(json.dumps(CAL)))
        c = mv.calibration_version({**CAL, "bull_weights": {"a": 1.1}})
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])
        self.assertTrue(a["id"].startswith("calib-"))
        self.assertEqual(a["trained_at"], CAL["ts"])
        self.assertEqual(a["n_tickers"], 2)

    def test_calibration_mappingproxy_same_id(self):
        frozen = MappingProxyType({**CAL, "tickers": ("TQQQ", "SOXL"),
                                   "bull_weights": MappingProxyType({"a": 1.0})})
        self.assertEqual(mv.calibration_version(frozen)["id"], mv.calibration_version(CAL)["id"])

    def test_missing_calibration(self):
        self.assertEqual(mv.calibration_version(None), {"id": None, "status": "missing"})

    def test_hmm_version_prefers_frozen_version_id(self):
        info = {"ts": "x", "version_id": "hmm-abc", "current_label": "bull", "current_prob": 0.8,
                "train_start": "2024-01-02", "train_end": "2026-10-01"}
        v = mv.hmm_version(info)
        self.assertEqual(v["id"], "hmm-abc")
        self.assertEqual((v["train_start"], v["train_end"], v["label"]),
                         ("2024-01-02", "2026-10-01", "bull"))

    def test_legacy_hmm_state_gets_content_id(self):
        info = {"ts": "2026-10-01T22:22:53", "current_label": "bull", "current_prob": 0.9,
                "state_stats": {"0": {"spy_ret": 0.1}}}
        v = mv.hmm_version(info)
        self.assertTrue(v["id"].startswith("hmm-legacy-"))
        self.assertFalse(v["frozen"])


try:
    import hmmlearn  # noqa: F401
    import numpy as np
    import pandas as pd
    HAVE_HMM = True
except Exception:
    HAVE_HMM = False


@unittest.skipUnless(HAVE_HMM, "hmmlearn not installed")
class FrozenHmmArtifact(unittest.TestCase):
    def _df(self):
        rng = np.random.default_rng(0)
        idx = pd.bdate_range("2024-01-02", periods=300)
        X = np.vstack([rng.normal(0.001, 0.01, 300), rng.normal(0.15, 0.03, 300),
                       rng.normal(18, 3, 300), rng.normal(0, 0.05, 300)]).T
        return pd.DataFrame(X, index=idx, columns=["spy_ret", "spy_vol", "vix", "vix_chg"])

    def test_train_freezes_params_and_replays(self):
        import hmm_regime
        df = self._df()
        with patch.object(hmm_regime, "_fetch_features", return_value=df):
            info = hmm_regime.train_and_detect()
        self.assertNotIn("error", info)
        for k in ("version_id", "train_start", "train_end", "features", "model_params", "model", "ts_utc"):
            self.assertIn(k, info)
        self.assertEqual(info["train_start"], "2024-01-02")
        self.assertEqual(info["features"], list(df.columns))
        for k in ("startprob", "transmat", "means", "covars"):
            self.assertIn(k, info["model"])
        # 重放: 冻结参数 + 同一输入 → 同一当前状态与概率
        rep = hmm_regime.replay(info, df.values)
        self.assertEqual(rep["current_state"], info["current_state"])
        self.assertAlmostEqual(rep["current_prob"], info["current_prob"], places=3)
        # JSON 往返后 version_id 仍可重算
        rt = json.loads(json.dumps(info))
        self.assertEqual(hmm_regime.compute_version_id(rt), info["version_id"])

    def test_detect_today_appends_version_history(self):
        import hmm_regime
        with tempfile.TemporaryDirectory() as td:
            st, hist = Path(td) / "hmm_state.json", Path(td) / "hmm_versions.jsonl"
            with patch.object(hmm_regime, "_fetch_features", return_value=self._df()), \
                 patch.object(hmm_regime, "HMM_STATE_PATH", st), \
                 patch.object(hmm_regime, "HMM_VERSIONS_PATH", hist):
                info = hmm_regime.detect_today()
                hmm_regime.detect_today()
            rows = [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["version_id"], info["version_id"])
            self.assertIn("model", rows[0])
            self.assertEqual(json.loads(st.read_text(encoding="utf-8"))["version_id"], info["version_id"])


class DecisionCarriesVersions(unittest.TestCase):
    MKT = {"ticker": "US.SOXL", "price": 30.0, "pct_chg": 0.5, "rsi_14": 50, "vol_ratio": 1.0,
           "trend": "up", "ma_stack": "bull"}

    def test_live_decision_has_model_versions(self):
        import decision_agent as da
        hmm = {"version_id": "hmm-test1", "ts": "t", "current_label": "bull", "current_prob": 0.9}
        with patch.object(da, "_load_calibration", return_value=CAL), \
             patch.object(da, "_llm_call", return_value=None), \
             patch("hmm_regime.load", return_value=hmm):
            r = da.get_decision(self.MKT, {}, {}, board_regime="neutral")
        mvs = r["model_versions"]
        self.assertEqual(mvs["calibration"]["id"], mv.calibration_version(CAL)["id"])
        self.assertEqual(mvs["hmm"]["id"], "hmm-test1")
        self.assertEqual(mvs["source"], "live")

    def test_backtest_context_does_not_read_live_hmm(self):
        import decision_agent as da
        from decision_context import DecisionContext
        from datetime import datetime, timezone
        ctx = DecisionContext(as_of=datetime(2026, 1, 5, tzinfo=timezone.utc),
                              calibration_snapshot={}, hmm_state="", is_backtest=True,
                              board_regime="neutral")
        with patch.object(da, "_llm_call", return_value=None), \
             patch("hmm_regime.load", side_effect=AssertionError("live hmm read")):
            r = da.get_decision(self.MKT, {}, {}, context=ctx)
        mvs = r["model_versions"]
        self.assertEqual(mvs["source"], "context")
        self.assertEqual(mvs["hmm"], {"id": None, "status": "context_unavailable", "label": None})
        self.assertEqual(mvs["calibration"]["status"], "missing")

    def test_trade_log_records_model_versions(self):
        import paper_trader as pt
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "signals" / "trade_log.jsonl"
            with patch.object(pt, "TRADE_LOG_PATH", log_path):
                pt._log_trade("US.X", "BUY", 1, 10.0, "1", "t",
                              decision={"action": "BUY", "model_versions": {"hmm": {"id": "hmm-z"}}})
            row = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(row["decision"]["model_versions"], {"hmm": {"id": "hmm-z"}})


if __name__ == "__main__":
    raise SystemExit(
        "Run the suite from agents/: python -m unittest discover -s tests -t . "
        "Direct runs bypass tests/__init__.py isolation and can touch live files."
    )
