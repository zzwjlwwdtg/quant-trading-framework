"""Unified signal-generation entry point.

Borrowed pattern from FreqTrade IStrategy — one class every caller invokes.
Eliminates the recurring "手动 emit 才有 CBRS 信号" bug where new tickers
added to TRACKED_TICKERS wouldn't get _latest.json until orchestrator's
next scheduled window.

Callers (current):
  - _snapshot_today.py  → SignalStrategy().emit_all('tracked')
  - manual/ad-hoc       → python -m strategy_runner --emit CBRS
  - backtest engines    → SignalStrategy().get_signal(tk)  (pure, no I/O)

Callers (future, incremental migration):
  - orchestrator._etf_cycle / _gold_cycle → SignalStrategy(full=True).emit_all(...)
    (postponed: orchestrator has confluence + claude_gate + m15_context + trade_execute
     that we don't want to touch in this pass)
"""
from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Universe scopes ──────────────────────────────────────────────────
SCOPE_CORE = "core"        # config.TICKERS (4 leveraged ETFs)
SCOPE_TRACKED = "tracked"  # + TRACKED_TICKERS (adds NVDA/MSFT/AAPL/NBIS/SHY/IEI/LITE/CBRS/USO/XLV/...)
SCOPE_FULL = "full"        # + GOLD_TICKER (US.GLD)
VALID_SCOPES = {SCOPE_CORE, SCOPE_TRACKED, SCOPE_FULL}


class SignalStrategy:
    """
    Single entry point for ticker signal generation.

    Methods:
      get_signal(tk):    pure computation, no side effects (safe for backtest)
      emit_signal(tk):   get_signal + notifier.emit → writes signals/{tk}_latest.json + log
      emit_all(scope):   iterate universe(scope), emit each, resilient to individual failures

    Handles:
      - GLD-vs-equity routing (get_futures_signal + get_gold_decision for gold)
      - events cache reuse across tickers in one emit_all pass
      - continues on individual ticker errors instead of aborting the batch
    """

    def __init__(self, gold_ticker: str = "US.GLD"):
        self.gold_ticker = gold_ticker
        # Cache events across a single emit_all call so each ticker doesn't
        # re-fetch. Callers can .reset_events_cache() between runs.
        self._cached_events: Optional[dict] = None
        self._cached_gold_events: Optional[dict] = None

    # ── Universe helpers ────────────────────────────────────────
    def get_universe(self, scope: str = SCOPE_FULL) -> list[str]:
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        from config import TICKERS, TRACKED_TICKERS
        if scope == SCOPE_CORE:
            return list(TICKERS)
        result = list(TICKERS) + list(TRACKED_TICKERS)
        if scope == SCOPE_FULL and self.gold_ticker not in result:
            result.append(self.gold_ticker)
        return result

    def is_gold(self, ticker: str) -> bool:
        return ticker == self.gold_ticker or ticker == self.gold_ticker.split(".")[-1]

    # ── Events (cached across one emit_all pass) ────────────────
    def _events_for(self, ticker: str) -> dict:
        if self.is_gold(ticker):
            if self._cached_gold_events is None:
                try:
                    from events_watch import get_gold_events_signal
                    self._cached_gold_events = get_gold_events_signal() or {}
                except Exception as e:
                    logger.warning(f"get_gold_events_signal failed: {e}")
                    self._cached_gold_events = {}
            return self._cached_gold_events
        if self._cached_events is None:
            try:
                from events_watch import get_events_signal
                self._cached_events = get_events_signal() or {}
            except Exception as e:
                logger.warning(f"get_events_signal failed: {e}")
                self._cached_events = {}
        return self._cached_events

    def reset_events_cache(self) -> None:
        self._cached_events = None
        self._cached_gold_events = None

    # ── Pure computation (safe for backtest) ────────────────────
    def get_signal(self, ticker: str, events: Optional[dict] = None) -> dict:
        """Compute market + decision for one ticker. No side effects.

        Returns:
          {"ticker", "market", "events", "decision"}  on success
          {"ticker", "error"}                          on failure
        """
        try:
            if self.is_gold(ticker):
                from futures_watch import get_futures_signal
                from decision_agent import get_gold_decision
                mkt = get_futures_signal(ticker)
                if isinstance(mkt, dict) and mkt.get("error"):
                    return {"ticker": ticker, "error": f"market: {str(mkt['error'])[:100]}"}
                ev = events if events is not None else self._events_for(ticker)
                dec = get_gold_decision(mkt, ev, {})
            else:
                from market_watch import get_market_signal
                from decision_agent import get_decision
                mkt = get_market_signal(ticker)
                if isinstance(mkt, dict) and mkt.get("error"):
                    return {"ticker": ticker, "error": f"market: {str(mkt['error'])[:100]}"}
                ev = events if events is not None else self._events_for(ticker)
                dec = get_decision(mkt, ev)
            return {"ticker": ticker, "market": mkt, "events": ev, "decision": dec}
        except Exception as e:
            return {"ticker": ticker, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # ── With side effects (writes _latest.json + logs) ──────────
    def emit_signal(self, ticker: str, events: Optional[dict] = None) -> dict:
        """get_signal + notifier.emit(). Continues gracefully if emit fails."""
        result = self.get_signal(ticker, events)
        if "error" not in result:
            try:
                from notifier import emit
                emit(result["market"], result["events"], result["decision"])
            except Exception as e:
                result["_emit_error"] = f"emit fail: {str(e)[:100]}"
        return result

    def emit_all(self, scope: str = SCOPE_FULL,
                 progress_callback: Optional[Callable[[int, int, str, str], None]] = None
                 ) -> dict[str, dict]:
        """Iterate universe(scope), emit each. Resilient to individual failures.

        Returns: {ticker: result_dict}

        progress_callback(idx, total, ticker, summary_str) invoked after each emit;
        summary_str is a short human-readable status like "BUY conf=4 @$401".
        """
        universe = self.get_universe(scope)
        results: dict[str, dict] = {}
        total = len(universe)
        for i, tk in enumerate(universe, 1):
            result = self.emit_signal(tk)
            results[tk] = result
            if progress_callback:
                try:
                    if "error" in result:
                        summary = f"ERR: {result['error']}"
                    else:
                        d = result["decision"] or {}
                        m = result["market"] or {}
                        summary = f"{d.get('action', '?')} conf={d.get('confidence', '?')} @${m.get('price', '?')}"
                        if result.get("_emit_error"):
                            summary += f" ({result['_emit_error']})"
                    progress_callback(i, total, tk, summary)
                except Exception:
                    pass
        return results


# ── CLI entry point ─────────────────────────────────────────
def _print_progress(idx: int, total: int, tk: str, summary: str) -> None:
    prefix = f"[{idx}/{total}]"
    print(f"  {prefix} {tk}: {summary}")


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="SignalStrategy CLI — unified signal emit (borrows FreqTrade IStrategy pattern)"
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--emit", metavar="TICKER",
                     help="Emit signal for one ticker (e.g. CBRS or US.CBRS)")
    grp.add_argument("--emit-all", action="store_true",
                     help="Emit signals for entire universe (scope-controlled)")
    grp.add_argument("--list", action="store_true",
                     help="List tickers in universe for given scope")
    ap.add_argument("--scope", choices=sorted(VALID_SCOPES), default=SCOPE_FULL,
                    help="Universe scope (default: full = TICKERS + TRACKED + GLD)")
    ap.add_argument("--dry", action="store_true",
                    help="Compute + print, don't call emit() (no _latest.json write)")
    args = ap.parse_args()

    strategy = SignalStrategy()

    if args.list:
        tks = strategy.get_universe(args.scope)
        print(f"scope={args.scope} ({len(tks)} tickers):")
        for tk in tks:
            print(f"  {tk}")
        return 0

    if args.emit_all:
        print(f"emit_all scope={args.scope} ({len(strategy.get_universe(args.scope))} tickers)")
        results = strategy.emit_all(scope=args.scope, progress_callback=_print_progress)
        ok = sum(1 for r in results.values() if "error" not in r)
        print(f"\ndone: {ok}/{len(results)} tickers emitted successfully")
        return 0 if ok == len(results) else 1

    if args.emit:
        raw = args.emit.strip().upper()
        tk = raw if raw.startswith("US.") else f"US.{raw}"
        result = strategy.get_signal(tk) if args.dry else strategy.emit_signal(tk)
        if "error" in result:
            print(f"{tk}: ERR {result['error']}")
            return 1
        d = result["decision"]
        m = result["market"]
        print(f"{tk}: {d.get('action')} conf={d.get('confidence')} @${m.get('price')}")
        if args.dry:
            print("  (dry mode — no _latest.json written)")
        elif result.get("_emit_error"):
            print(f"  emit warning: {result['_emit_error']}")
            return 1
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
