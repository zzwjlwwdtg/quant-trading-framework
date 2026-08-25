# fsi-skills Trading Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**Language**: **English** · [简体中文](README.zh-CN.md)

Multi-strategy quantitative signals + Codex-first local AI CLI analysis + moomoo paper-trading automation.

Targets leveraged and single-name US equities plus rate-sensitive hedges (**TQQQ / SOXL / DRAM / MULL / GLD / NBIS / SHY / IEI / LITE / CBRS / USO / XLV / NVDA / MSFT / AAPL** + satellite: TSLA / GOOGL / KLAC / AMAT / MU). Daily-candle momentum is the main signal with a 15-min intraday assist, smart-money/options context, Trump Truth Social parsed via CLI, gold macro factors, bond monitoring, and options gamma/GEX/IV/Skew analysis. **Macro layer (v0.4)**: next-45-day event scenario forecasts (Cleveland Fed nowcast + CME FedWatch) with Jinshi-style bond/equity impact labels and concrete asset lists, plus a US bond-rescue policy-toolkit tracker (11 tools × 30Y yield reaction). Actionable decisions flow through `paper_trader` into a moomoo **SIMULATE** account (never live).

## Dashboard Preview

**🌐 Live public snapshot (read-only): https://zzwjlwwdtg.github.io/quant-trading-framework/**

Auto-refreshed every 30 min from local webui via `agents/snapshot_generator.py`.
Owner-only panels (positions / NAV / trade log) are stripped; all other cards
(signals, capital flow, options walls, JP guidance, Ichimoku, ToSTNeT verification)
are visible. Not investment advice — educational demo of the framework.

WebUI (`webui.bat` → http://127.0.0.1:8080) — zero-dependency `http.server` + single-page dashboard covering NAV, sector regimes, Trump sentiment, gold/oil macro, event calendar, and per-ticker cards with signals + option walls + AI analysis.

![Dashboard overview](docs/dashboard-full.png)

### Per-ticker card contents

1. **Bull/bear confluence bar** + recommended action + confidence
2. **Mini options wall SVG** — call/put volume distribution by strike + spot dashed line + Max Pain + Pin (gamma_flip)
3. **Leveraged-ETF price mapping** (TQQQ/SOXL) — proxy structure prices (QQQ/SOXX) converted to spot-anchored actionable prices with historical decay + expiry-path estimates
4. **Attack/defense 4-column table** — strike / single-contract premium (bid/ask mid) / **OI** (open interest, real positioning) / **notional exposure** (OI × 100 × strike); big-money tag 🔥 when OI ≥ 5K or notional ≥ $30M
5. **Squeeze risk badge** — `gamma_up` / `put_break` / `gamma_band_up` / `put_band_break` / `max_pain_gravity`, detail text includes concrete OI + premium numbers
6. **⚡ Options verdict** (GEX + IV regime + Skew combined stock verdict) — 3 risks (breakdown / event / chase_high) + 3 opportunities (buy_now / add_more / reduce) + key prices (resistance / pin / support), all mapped to leveraged-ETF prices where applicable
7. **C/P ratio + wall OI imbalance** — beginner-friendly interpretation distinguishing ATM panic vs OTM insurance, and Put >> Call OI vs Call >> Put OI scenarios
8. **📅 Earnings badge** — related-earnings stock + T-N days + implied move ± IM%; expiry crossing earnings tagged "spans earnings"
9. **🤖 Codex-first live analysis** — 3-line structured for all 15+ tickers, cached by data hash
10. **🔗 Supply chain** — AI CLI-generated upstream/downstream/peers with confidence marks; optional FMP peers cross-verification; lazy-loaded, 30-day cache
11. **📊 4-year fundamentals** (or **8 quarters** via toggle) — CROIC / Piotroski F / financial debt / cash conversion cycle from yfinance; 30/15-day cache

**Top macro block** (above the ticker cards): bond monitor + AI interpretation + 180-day `cut_prob` trend + **next-45-day event scenario forecast** (each event with bond/equity impact label + concrete asset lists in Jinshi Data style) + **US bond-rescue policy toolkit tracker** (11 tools × status × 30Y T+0/T+1d reaction) + Trump attribution.

### 🕸 Supply-chain spider web (Bloomberg SPLC style)

Each card has `🕸 1-hop / 🕸 2-hop` buttons in the supply-chain section:
- **1-hop**: direct neighbors (fast, uses local cache)
- **2-hop**: BFS expansion (NVDA depth=2 = 90 nodes / 233 edges — downstream's downstream + upstream's upstream)
- **Edge colors**: 🔴 supply / 🟢 customer / 🟡 peer; width ∝ weight; solid = high confidence, dashed = medium/low
- **Edge hover shows reason** (example: `TSM → NVDA · supply · exclusive foundry for H100/H200/B100/B200 leading-edge nodes (4N/3nm)`)
- FMP-verified peers get a green ring
- Drag nodes to rearrange, Esc / click backdrop to close
- Direct URL: `?graph=NVDA&depth=2`

**1-hop example (NVDA)**: ![NVDA direct neighbors](docs/supply-chain-nvda-graph.png)

**2-hop example (NVDA deep)**: ![NVDA 2-hop supply chain](docs/supply-chain-nvda-depth2.png)

### 📊 4-year fundamentals (StatementDog style, free)

Each card exposes 4 free metrics from yfinance (30-day cache, or 15-day for quarterly):

- **CROIC (Cash Return on Invested Capital)** — FCF ÷ Invested Capital; >10% healthy, >20% cash machine
- **Piotroski F-Score** — 9-point financial improvement scorecard; 7-9 strong, 4-6 normal, 0-3 warning
- **Financial debt** — ST + LT debt in $M; rising trend = leverage buildup
- **Cash conversion cycle** — DIO + DSO - DPO in days; <30 excellent, >120 inventory pressure
- Each cell: latest value + up/down arrow + 4-period sparkline + hover explanation
- ETFs mapped to a representative single stock (TQQQ/SOXL → NVDA, DRAM/MULL → MU); GLD skipped (commodity)
- **Also fed into AI analysis** — the configured CLI flags CROIC crashes / debt surges / Piotroski < 4

Toggle button `📆 4 years · switch to quarterly` / `📅 8 quarters · switch to yearly` per ticker, choice remembered in localStorage. Quarterly mode uses **year-over-year same-quarter comparison** for Piotroski (avoids seasonality).

### 🔥 Large-order highlight + earnings integration

- Option wall OI ≥ 5K contracts or notional ≥ $30M → attack/defense table shows 🔥, mini SVG bar gets brighter fill + top 🟠 dot
- "Unusual volume" (today's vol > 0.5 × OI) → hover shows ⚡
- Each card gets an **📅 earnings badge** on top (related earnings stock + T-N days + implied move ± IM%)
  - MULL/DRAM ← MU earnings; SOXL/TQQQ ← NVDA earnings
  - T ≤ 3 days 🚨 red / T ≤ 14 days ⚠ yellow / T ≤ 60 days 📅 gray
  - Option expiries crossing earnings tagged "spans earnings" (premium expensive but hedge valid)

### Top row

📅 **Upcoming events calendar** (next 45 days) — FOMC / CPI / NFP / NVDA earnings with beginner hints on market impact.

**Expand-all view**: `?expand=all` (for screenshots / holistic review) — [see dashboard-full-expanded.png](docs/dashboard-full-expanded.png)

## Key features

- **Macro forecasting layer** (v0.4 new) — `thesis_forecast` 45-day event scenarios (Cleveland Fed + CME FedWatch + prior) + policy-toolkit tracker (buybacks/YCC/TGA/SLR × 30Y bp reaction) + Trump attribution
- **Full options-structure interpretation** (v0.4 expansion) — OI-based walls + combined bands + GEX+IV+Skew `stock_verdict` + premium/OI/notional table + leveraged-ETF structural-price mapping
- **AI news structuring ecosystem** — all RSS / Truth Social first go through the unified AI CLI (Codex by default) into a fixed JSON schema before rules consume them; Google News topic queries serve as the primary macro-news source
- **Regime as single source** — computed once at pre-open → all modules read the same source; no independent detection allowed
- **TECHNICAL_ONLY default ON** — decisions look at technicals only; message-side (Trump / breaking_news / event calendar) is banners only and does not enter `decision_agent` scoring
- **Backtest gate** — any signal or decision change must pass `_backtest_modules_accuracy.py` etc. without hit-rate regression before merging; training-set N ≤ 5 as a hard rule is over-fitting
- **Explainable signal stack** — fixed momentum/technical rules plus confluence (calibrated per asset class) and options context; automatically evolved scores are excluded from live decisions
- **Witching-day detection** — identifies quarterly 3/6/9/12 third-Friday, plus GEX proxy + related-earnings alerts
- **AI price targets** — the AI CLI outputs structured JSON (entry_ref / stop_ref), `paper_trader` auto-places limit + SELL STOP orders

### Institutional measurement layer (small-account profile)

- **Portfolio risk and attribution** — NAV/cash/gross and leverage-adjusted exposure, historical or fallback VaR/ES, group stress tests, correlation/risk contribution, SPY beta/alpha/tracking error, and P&L attribution.
- **Leveraged-ETF path risk** — TQQQ/QQQ, SOXL/SOXX and MULL/MU daily-reset simulations separate endpoint leverage from volatility decay, fees and realized tracking residuals.
- **Execution audit** — submitted/filled/partial/cancelled orders are reconciled to the broker feed, actual implementation shortfall is measured, and the JSONL ledger has a tamper-evident hash chain.
- **Point-in-time data controls** — append-only observation/effective timestamps, freshness/price/indicator validation, and fail-closed new-risk orders when core market data is invalid. Risk-reducing exits remain available.
- **Purged walk-forward validation** — fixed strategy rules are evaluated on chronological out-of-sample folds with purge and embargo gaps; failed rules are not promoted as current pattern signals.
- **Aggressive small-account policy** — theoretical bid/ask spread, modeled market impact and strategy capacity do not reduce size or block an order. Actual fills are still measured. In `SIM_ACTIVE`, VaR/stress/concentration results are dashboard warnings rather than order gates.

## Ticker universe ([`agents/config.py`](agents/config.py))

**Core leveraged ETFs (`TICKERS`)**:

| Ticker | Type | Leverage | Notes |
|---|---|---|---|
| TQQQ | Nasdaq-100 leveraged ETF | 3x | Primary tech long |
| SOXL | Semiconductor leveraged ETF | 3x | Semi long |
| DRAM | Roundhill Memory ETF | 1x | Memory sector (Micron + SK Hynix + Samsung exposure) |
| MULL | Micron leveraged ETF | 2x | DRAM/NAND single-name leverage |

**Extended `TRACKED_TICKERS` + GLD**:

| Ticker | Type | Purpose |
|---|---|---|
| GLD | Gold ETF | Hedge / macro offset |
| NVDA | Semi-chain bellwether | Read this to know SOXL/DRAM direction |
| MSFT | Cloud AI + FAANG | TQQQ/QQQ weight |
| AAPL | QQQ largest weight | Apple supply-chain tracking |
| **NBIS** | Nebius AI cloud pure-play | 2026-Q3 thesis: cloud-consumption growth expression |
| **SHY** | 1-3Y Treasury ETF | 2026-Q3 thesis: 2Y bond-long proxy |
| **IEI** | 3-7Y Treasury ETF | 2026-Q3 thesis: 5Y bond-long proxy |
| LITE | Lumentum optical | AI DC 400G/800G modules |
| CBRS | Cerebras Systems | Wafer-scale AI inference, NVDA competitor |
| USO | WTI crude ETF | Inflation proxy + geopolitics gauge |
| XLV | Healthcare ETF | Defensive sector (JNJ/UNH/LLY/PFE weights) |

Also: satellite single-names (TSLA/GOOGL/KLAC/AMAT etc.) selected daily by the universe picker; **JP sector** — 11 short-name tickers (NBR/TDK/ARE/MUFG etc., see [`jp_watch_contracts.py`](agents/jp_watch_contracts.py)).

## Quick start

```cmd
:: 1. Install deps (first-time)
cd f:\fsi-skills\agents
setup.bat

:: 2. Configure secrets (first-time)
copy secrets.example.json secrets.local.json
:: Edit secrets.local.json to fill FRED_API_KEY and MOOMOO_ACC_ID
:: ⚠ secrets.local.json is in .gitignore — NEVER commit; see SECURITY.md

:: 3. Enable pre-commit safety hook (first-time)
git config core.hooksPath .githooks
:: Blocks accidental commits of sk-... / ghp_... / AKIA... secret patterns

:: 4. Start moomoo OpenD (keep running)

:: 5. Pick a run mode
run.bat        :: Long-running orchestrator (5min scheduler checks + 5 ET windows)
snap.bat       :: One-shot daily snapshot (regime + Trump + signals + options + AI)
tools.bat      :: Tools menu (trader status / regime / picks / flatten etc.)
backtest.bat   :: Backtest menu (regime / news / trump / modules / V-bounce)
trump.bat      :: Trump signal alone
weekly.bat     :: Weekend refresh of module_accuracy.md
webui.bat      :: WebUI dashboard at http://127.0.0.1:8080
```

The default policy is `AI_CLI_PRIMARY=codex` and `AI_CLI_FALLBACK=none`, so a
Codex failure never invokes Claude implicitly. To restore the old behavior for
an exceptional run, set `AI_CLI_PRIMARY=claude` and `AI_CLI_FALLBACK=codex`
before launch. `snap_public.bat` always pins Codex with no Claude fallback.

## Config files

| File | Purpose | In git |
|---|---|---|
| [`agents/config.py`](agents/config.py) | Public config: TICKERS / leverage factors / windows / thresholds | ✅ |
| [`agents/secrets.example.json`](agents/secrets.example.json) | Secrets template (placeholders) | ✅ |
| `agents/secrets.local.json` | **Real** FRED_API_KEY / MOOMOO_ACC_ID etc. | ❌ (.gitignore) |
| `.claude/settings.local.json` | Claude Code permission whitelist | ✅ |
| [`SECURITY.md`](SECURITY.md) | Secrets management / pre-commit hook / vulnerability reporting | ✅ |
| [`.githooks/pre-commit`](.githooks/pre-commit) | Blocks accidental API-key commits (enable with `git config core.hooksPath .githooks`) | ✅ |

## Entry-point scripts

- [`run.bat`](agents/run.bat) — orchestrator loop, 5 ET windows (08:30 / 09:20 / 10:00 / 12:00 / 15:45) auto-trigger
- [`snap.bat`](agents/snap.bat) — one-shot snapshot: regime / Trump / witching / signals / decisions / event calendar / AI narrative
- [`tools.bat`](agents/tools.bat) → [`tools_menu.py`](agents/tools_menu.py) — interactive menu
- [`backtest.bat`](agents/backtest.bat) → [`backtest_menu.py`](agents/backtest_menu.py) — backtest menu
- [`trump.bat`](agents/trump.bat) — Trump signal banner only
- [`weekly.bat`](agents/weekly.bat) — one-click refresh of module_accuracy.md (weekend)

## System architecture (data flow)

```
                        ┌─── moomoo OpenD (real-time quote + paper orders)
                        │
   FRED ────┐           │      ┌─── Trump signal (CNN truth_archive)
            ├── data_feeds + market_watch ──┤
   yfinance ┘           │      └─── Options chain (yfinance options)
                        │
                        ↓
            [Block 1-11 signal-collection layer]
                        ↓
   regime_today.py (single source, computed pre-open)
                        ↓
   ┌──────────────────────────────────────┐
   │ decision_agent._etf_rules /          │
   │ _gold_rules → action + conf + stop_ref│
   │ (V-bounce / Trump override / event-tier)│
   └──────────────────────────────────────┘
                        ↓
   claude_gate.py (pre-trade approval; run.bat defaults fail-closed)
                        ↓
   ┌──────────────────────────────────────┐
   │ paper_trader.execute() 7-stage chain:│
   │  ① window gating → ② discipline mgmt │
   │  ③ dedup → ④ action routing          │
   │  ⑤ vol-target sizing → ⑥ limit build │
   │  ⑦ moomoo SDK submission             │
   └──────────────────────────────────────┘
                        ↓
                  moomoo SIMULATE account

   ＊ AI CLI narrative/report layer (out-of-band, separate from the gate):
     After run_cycle completes, invokes Codex CLI by default to summarize
     all 11 blocks into a 700-1000 word plain-language report
     + structured JSON price targets. Next cycle, paper_trader
     reads the JSON to adjust limit / stop-loss levels.
```

## Signal modules (Block numbers referenced by AI prompts)

| # | Module | File |
|---|---|---|
| ① Base report | [`report_generator.py`](agents/report_generator.py) |
| ② Fixed technical-pattern backtest | [`strategy_engine.py`](agents/strategy_engine.py) `generate_pattern_leaderboard` |
| ③ Signal live (confluence, per-asset-class calibrated) | [`confluence.py`](agents/confluence.py) + [`market_watch.py`](agents/market_watch.py) + [`_calibrate_confidence.py`](agents/_calibrate_confidence.py) |
| ④ Event calendar | [`events_watch.py`](agents/events_watch.py) |
| ⑤ Trump signal (technical-only mode: banner-only) | [`trump_signal.py`](agents/trump_signal.py) |
| ⑥ Options wall (OI-based + combined bands + GEX+IV+Skew verdict) | [`option_walls.py`](agents/option_walls.py) + [`gex_calc.py`](agents/gex_calc.py) |
| ⑦ MACD + ADX | [`market_watch.py`](agents/market_watch.py) |
| ⑧ SOX PCA | [`pca_sox.py`](agents/pca_sox.py) |
| ⑨ Gold macro | [`gold_macro.py`](agents/gold_macro.py) |
| ⑩ Options risk (witching / GEX / related earnings) | [`option_walls.py`](agents/option_walls.py) `get_options_risk_signal` |
| ⑪ JP social signal | [`jp_social_reco/`](agents/jp_social_reco/) |
| ⑫ Bond monitor + AI interpretation | [`bond_monitor.py`](agents/bond_monitor.py) + [`bond_ai_interpret.py`](agents/bond_ai_interpret.py) |
| ⑬ Macro thesis forecast (event × 3 scenarios × probability) | [`thesis_forecast.py`](agents/thesis_forecast.py) + [`cleveland_nowcast.py`](agents/cleveland_nowcast.py) + [`fed_watch.py`](agents/fed_watch.py) |
| ⑭ US bond-rescue policy toolkit tracker | [`policy_toolkit_tracker.py`](agents/policy_toolkit_tracker.py) |
| ⑮ Trump thesis attribution (post → cut_prob delta, 48h) | [`trump_thesis_attribution.py`](agents/trump_thesis_attribution.py) |
| ⑯ Options flow (quote-side, score-capped 69) | [`option_flow.py`](agents/option_flow.py) |
| ⑰ Structured news pipeline (RSS → CLI → JSON) | [`news_analyzer.py`](agents/news_analyzer.py) |

## Decision modules

| File | Role |
|---|---|
| [`agents/decision_agent.py`](agents/decision_agent.py) | Rule engine: `_etf_rules` / `_gold_rules` → action + conf + stop_ref (both respect confluence calibration) |
| [`agents/regime_today.py`](agents/regime_today.py) | Regime single source (writes `regime_state.json` at pre-open) |
| [`agents/paper_trader.py`](agents/paper_trader.py) | moomoo SIMULATE ordering + position sizing + TP/SL |
| [`agents/ai_prompt.py`](agents/ai_prompt.py) | Central Codex-first AI CLI router + prompt templates + structured JSON parsing |
| [`agents/claude_gate.py`](agents/claude_gate.py) | AI second-opinion gate (legacy filename; pre-trade approval) |
| [`agents/portfolio_analytics.py`](agents/portfolio_analytics.py) | Portfolio exposure, VaR/ES, stress, correlation, benchmark and attribution analytics |
| [`agents/leveraged_etf_risk.py`](agents/leveraged_etf_risk.py) | Daily-reset leverage, volatility-decay and path-scenario analysis |
| [`agents/execution_analytics.py`](agents/execution_analytics.py) | Small-account execution plan plus actual-fill and audit-chain reconciliation |
| [`agents/data_quality.py`](agents/data_quality.py) | Point-in-time store, freshness/coverage checks and order data gate |
| [`agents/webui.py`](agents/webui.py) | Dashboard HTTP server + 20+ API endpoints (health/nav/positions/signals/bonds/thesis_forecast/policy_toolkit/…) |
| [`agents/snapshot_generator.py`](agents/snapshot_generator.py) | Static snapshot to `docs/data/*.json` + `docs/index.html` for GitHub Pages |

## Backtests

| Script | Validates |
|---|---|
| [`_backtest_modules_accuracy.py`](agents/_backtest_modules_accuracy.py) | Per-module × 1d/5d/10d/20d hit-rate (baseline regression) |
| [`_backtest_regime_fix.py`](agents/_backtest_regime_fix.py) | Decision stability across regime-source refactors |
| [`_backtest_news_pipeline.py`](agents/_backtest_news_pipeline.py) | CLI-parsed vs keyword-match (event-landing accuracy) |
| [`_backtest_trump_signal.py`](agents/_backtest_trump_signal.py) | Trump signal direction hit-rate (vs trump-code baseline) |
| [`_backtest_v_bounce.py`](agents/_backtest_v_bounce.py) | V-reversal chase-buy (leveraged ETFs 5d/10d/20d) |
| [`_backtest_gold_macro.py`](agents/_backtest_gold_macro.py) | Gold macro signal injection backtest |
| [`backtest_engine.py`](agents/backtest_engine.py) | Lite / Mid full-system backtest tiers |
| [`research_validation.py`](agents/research_validation.py) | Purged/embargoed chronological walk-forward validation for fixed rules |

Regression tests (run from `agents/`): `python -m unittest discover -s tests -v`

Output report: `agents/signals/module_accuracy.md` (actual file is gitignored).

## Backup

- Before major changes: `python _make_backup.py` → `backups/agents_backup_<timestamp>_with_AB.zip`
- Milestones: `git tag v0.x.x && git push --tags`

## Known limitations

- **Paper trading only** — moomoo SIMULATE account, never live
- DRAM ETF has a shorter price history than the older ETFs, so long-window indicators may have fewer valid samples
- AI CLI calls normally take 30-60s; the 30-minute public snapshot is pinned to Codex and cannot silently spend Claude quota
- **No model API key required** — uses the saved local Codex CLI login; Claude is an explicit opt-in provider/fallback

## Changelog

Each tag's main changes. Full diffs at [GitHub Releases](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases).

### v0.4.0 — 2026-08-25

**Major release**: macro forecasting layer + full options-structure interpretation + mature AI-news pipeline

**New features**:

- **Macro forecasting layer** (new)
  - [`thesis_forecast.py`](agents/thesis_forecast.py) — next 45 days of events × 3 scenarios (dovish/base/hawkish) × probability → probability-weighted expected `delta_pp` for FOMC cut probability
  - Probability sources: **Cleveland Fed InflationNowcast** (CPI/PCE) + **CME FedWatch** (FOMC) + historical prior (NFP/PPI/Retail)
  - Each event gets **Jinshi-style market impact labels**: bond/equity 强利多/利多/偏多/无方向/偏空/利空/强利空 plus `_ASSET_MAP` — 12 event×direction combos → 4×4 concrete tickers (e.g. CPI hawkish 利多: USD/energy/banks/gold, 利空: tech/long bonds/REITs/growth)
  - [`policy_toolkit_tracker.py`](agents/policy_toolkit_tracker.py) — **US bond-rescue policy toolkit tracker**: Google News RSS + Fed press → CLI structuring → matches 11 predefined tools (Treasury buybacks/YCC/TGA deployment/SLR exemption/FIMA repo/FX intervention/QE restart/QRA wording/duration shortening/Fed dovish/hawkish signals) → `^TYX` computes 30Y yield reaction in bp for T+0 (event day) and T+1d (next day)
  - [`trump_thesis_attribution.py`](agents/trump_thesis_attribution.py) — attribution of Trump posts to `cut_prob` deltas (48h window)
  - [`bond_ai_interpret.py`](agents/bond_ai_interpret.py) — plain-language AI CLI interpretation of `bond_monitor` numbers
  - [`thesis_history.py`](agents/thesis_history.py) — 180-day `cut_prob` evolution time series
  - [`cleveland_nowcast.py`](agents/cleveland_nowcast.py) — Cleveland Fed nowcast data fetcher
- **Full options-structure interpretation** (heavy expansion)
  - Per-card **options wall SVG** (call/put volume distribution + spot dashed line + Max Pain + Pin/gamma_flip)
  - **OI-based walls (asymmetric)**: call above spot / put below spot to avoid ITM protective-put OI polluting the defense level
  - **Combined wall bands**: adjacent strong-OI strikes are merged (e.g. AAPL $300+$295 = $285M combined defense)
  - **Attack/defense 4-column table**: strike / single-contract premium (bid/ask mid) / OI / notional exposure
  - [`gex_calc.py`](agents/gex_calc.py) — **GEX + IV Regime + Skew** combined `stock_verdict`: 3 risks (breakdown/event/chase_high) + 3 opportunities (buy_now/add_more/reduce) + key prices (resistance/pin/support)
  - Squeeze risks: `gamma_up` / `put_break` / `gamma_band_up` / `put_band_break` / `max_pain_gravity`
  - **Leveraged-ETF structural-price mapping** (QQQ→TQQQ, SOXX→SOXL): 3-month historical decay calibration + spot-anchored + expiry-path estimate — so the UI never shows a $700 QQQ strike as a TQQQ limit price
  - Big-money tags 🔥 (OI ≥ 5K or notional ≥ $30M) + unusual-volume tag ⚡ (vol > 0.5 × OI)
  - [`option_flow.py`](agents/option_flow.py) — new options-flow scanner (quote-side inference, separated from wall structure, score cap 69 so it cannot alone trigger auto-reduction)
  - [`moomoo_data.py`](agents/moomoo_data.py) — moomoo openD as primary options-chain source (JP support + speed) with yfinance fallback
- **AI news structuring ecosystem**
  - [`news_analyzer.py`](agents/news_analyzer.py) — unified RSS → CLI → fixed JSON schema pipeline (8 event enum + normalization)
  - Google News RSS with topic queries as primary source (aggregates Reuters/Bloomberg/CNBC/WSJ)
  - CLI batching (20 items × 120s per batch) to avoid timeouts
- **Dashboard layer** (rearranged + new panels)
  - Top macro block: bond_monitor + AI interpretation + thesis history + **next-45-day event scenario forecast** + **US bond-rescue policy-toolkit tracker** + Trump attribution
  - Per signal card: bull/bear confluence bar → options wall SVG → attack/defense table → squeeze risk → **⚡ options verdict** (GEX+IV+Skew combined) → C/P ratio → wall OI imbalance interpretation → earnings badge → supply chain D3 spider web (depth=1/2) → 4-year fundamentals (StatementDog style: CROIC/Piotroski/debt/cash cycle)
  - Static snapshot mode (`STATIC_SNAPSHOT_MODE`) → GitHub Pages deployment
- **Ticker universe expansion** (5 → 15+)
  - Core leveraged ETFs: TQQQ/SOXL/DRAM/MULL/GLD
  - Bellwethers: NVDA/MSFT/AAPL
  - 2026-Q3 thesis: NBIS (AI cloud) + SHY/IEI (bond long hedge)
  - AI chain: LITE (optical) + CBRS (Cerebras)
  - Macro proxies: USO (oil) + XLV (healthcare)
  - JP sector (11 short-name tickers: NBR/TDK/ARE/MUFG etc.)

**Key bug fixes**:

- **Gold false SELL** — `_gold_rules` used hardcoded `_tech_bear()`, RSI 82 → bear=2 → SELL fired; bypassed `_calibrate_confidence` which had already removed RSI/CCI overbought on gold (empirical hit-rate 29.5%, i.e. inverse indicator). Fix: pass confluence into `_gold_rules`, use `bull_weighted/bear_weighted`
- **Confluence not attached to signals** — `strategy_runner.emit_signal` → `get_decision` computed confluence but didn't return it; only orchestrator's `_etf_cycle` did the manual attach. Fix: `get_decision` uniformly attaches `result["confluence"] = confluence`
- **Zombie signal files** — `US.GLD_latest.json` etc. from 2026-05 kept the old SELL visible in dashboard; `rm` cleanup
- **"AI macro unavailable"** — `f'{None:+.2f}'` crashed when yfinance returned None; added `if x is not None else "insufficient data"` guards
- **Missing snapshot endpoints** — `/api/thesis_forecast`, `/api/thesis_history`, `/api/trump_attribution`, `/api/policy_toolkit` weren't in `GLOBAL_ENDPOINTS` → docs snapshot missed 4 JSON files
- **URL param mismatch** — dashboard called `fetchJson('/api/thesis_forecast?days=45')` but snapshot saved parameter-less filename; unified to param-less URLs
- **Stale market_impact** — thesis_forecast cache was generated before `_market_impact_from_expected` existed, so front-end fell back to "中性"; purge cache → CPI -7.88pp now correctly shows bond/equity 强利空 with concrete asset lists
- **TECHNICAL_ONLY default ON** — Trump/breaking_news/event calendar degrade to banners only, no longer enter `decision_agent` scoring (avoids message-side noise)

**New memory entries**:

- `feedback_technical_only_mode.md` — decisions look at technicals only
- `feedback_oos_required.md` — auto-reduction / reversal signals must pass OOS validation
- `feedback_model_selection.md` — simple tasks use Haiku/Sonnet; Opus reserved for deep reasoning
- `project_thesis_2026Q3.md` — 2026-Q3 second-derivative inflection thesis (bond+cloud long, avoid semi)

### [v0.3.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.3.0) — 2026-07-28

**Major release**: 15 system-capability upgrades + push notifications + WebUI + open-source ready

**New features (15-item checklist landed)**:

- **P1 observability**
  - [`docs/EDGE.md`](docs/EDGE.md) explicitly lists real edge / fake edge / non-edge
  - [`_benchmark_report.py`](agents/_benchmark_report.py) NAV vs SPY/QQQ weekly (Sharpe/Max DD/α)
  - [`_trade_postmortem.py`](agents/_trade_postmortem.py) BUY/SELL pairing + P&L attribution
- **P2/P3 risk + sizing**
  - Correlation-group position caps (`tech_3x` 50% / `tech_2x` 30% / `single_high_beta` 30% etc., 6 groups)
  - Probe positions (low conf = 30% size) + pyramid adds (≤3 layers)
  - Half-Kelly size tweak (min 10 trades, cap [0.5, 1.5])
- **P4 decision/timing**
  - Sitting minimum-hold (SELL rejected if position < 3 days)
  - Loss-streak pause (3 consecutive losses → 24h no new positions)
  - HMM meta-regime one-way tightening (volatile/crisis/bear → bull_thresh +1, never loosen)
  - **Inverse ETF OOS rejection**: SQQQ/SOXS triggers had 5d up-rate only 26.5% (-33pp inverse indicator) → not deployed
- **P5 infrastructure**
  - `TRADER_LIVE_FRACTION` env var for gradual rollout (0.0-1.0)
  - [`_data_source_health.py`](agents/_data_source_health.py) data-source health check
- **Push notifications** [`notifications.py`](agents/notifications.py)
  - Discord webhook + Telegram bot (either or both)
  - 5-min dedup to prevent spam
  - Hooks: trade filled / crisis regime / watchdog restart / loss-streak pause
- **WebUI Dashboard** (zero-dep Python `http.server`)
  - [`webui.py`](agents/webui.py) 8 API endpoints (health/nav/positions/trades/log/hmm/signals/banners/ai_analysis)
  - [`dashboard.html`](agents/dashboard.html) single-page SPA + Chart.js + marked.js
  - NAV time-series + per-ticker signal cards (bull/bear confluence bar + regime badge) + HMM + Trump/Gold Macro cards + AI analysis markdown render
  - 30s auto-refresh, bound to 127.0.0.1 only
  - Launch via [`webui.bat`](agents/webui.bat)
- **Watchdog + auto-restart** [`_watchdog.py`](agents/_watchdog.py)
  - Windows Task Scheduler checks orchestrator PID every 30 min
  - Auto-clears stale lock + silently restarts via `pythonw` (no window)
- **Open source ready**
  - MIT [`LICENSE`](LICENSE)
  - [`SECURITY.md`](SECURITY.md) secrets management
  - [`.githooks/pre-commit`](.githooks/pre-commit) blocks sk-/ghp_/AKIA etc. patterns
  - `.gitignore` hardened for `.env.*` / credentials / pem / id_rsa
- **Backtest scientization**
  - OOS validation as hard gate (14 OOS samples ≥ 30, 5d edge ≥ 8pp before deployment)
  - `_backtest_overheated_oos.py` / `_backtest_divergence.py` / `_backtest_trend_capture.py` / `_backtest_inverse_etf.py` full OOS validation scripts

**Key bug fixes**:

- **conf_min not scaled to /5 range** — under TECHNICAL_ONLY all signals silently skipped (fix `conf_min * scale/10`)
- **A+B / C plan rollback** — OOS proved CAUTION layer + overheated multi-day accumulation + top divergence are all inverse indicators (-14 to -17pp)
- **.bat Chinese REM misparsed under Japanese CP932** — enforce ASCII-only
- **LOG_PATH not switching at midnight** — added `_DailyLogHandler` for auto file switch

### [v0.2.1](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.2.1) — 2026-06-23

Earnings-week implied-move guard + midnight log switch

- **Added** [`option_walls.get_earnings_implied_move(stock)`](agents/option_walls.py): reads ATM straddle of earnings week to compute implied ±% (with ATM±1 smoothing) + C/P vol ratio + IV
- **Added** [`decision_agent._apply_earnings_guard()`](agents/decision_agent.py): tiered dampening by `leveraged_im = im × ETF leverage`
  - `> 20%` → force HOLD; `12-20%` → conf-3 (<6 → HOLD); `6-12%` → conf-2; T-1/T-0 always HOLD
- orchestrator injects `events.earnings_implied_move` per cycle (MU/NVDA related stocks within 30 days)
- Backtest ([`_backtest_earnings_implied_move.py`](agents/_backtest_earnings_implied_move.py)): MU 5-year 20 earnings, MULL empirical beta=2.08x (validated); MULL |move|>20% probability 25%; 2024-12-18 MULL single-day -32.74%
- Live: MU 6-25 earnings, MULL/DRAM `WATCH_BUY` auto-downgraded to HOLD

### [v0.2.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.2.0) — 2026-06-22

JP Social Reco integration + Block ⑫

- Integrated [`jp_social_reco`](agents/jp_social_reco/) subsystem into main framework (snap.bat / orchestrator / AI prompt)
- Added `get_jp_social_with_backtest()`: signals + creator historical hit-rate + per-ticker per-horizon backtest verification
- AI prompt gained **block ⑫** JP influencer picks, ≥★★★ tickers must appear in morning watchlist
- Star algorithm (mentions × creator count): ★★★★★ / ★★★★ / ★★★ / ★★ / ★

### [v0.1.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.1.0) — 2026-06-21

Initial release

- Regime single source (pre-open compute + -5% crisis override)
- 5 tickers: TQQQ / SOXL / DRAM / MULL / GLD
- Evolved-rule confluence + 250-day module accuracy backtest (quant 20d 70-78%)
- Trump signal CLI parsing (80% hit-rate vs trump-code baseline)
- Gold macro (real_rate + DXY + WALCL + FOMC + oil + 10Y)
- Options monitoring (Call/Put Wall + Max Pain + GEX + witching + related earnings)
- V-bounce chase-buy (1x WATCH_BUY / leveraged LONG_HOLD split)
- Event tiering (CPI/FOMC/NFP critical, PCE/PPI/Retail high, earnings moderate)
- Codex-first AI CLI narrative + structured JSON price targets (Plan A)
- decision_agent auto-computes stop_ref (Plan B) → paper_trader submits real SELL STOP
- 7-stage vol-target order chain
- Secrets isolated to agents/secrets.local.json (gitignore)

## License

MIT — see [LICENSE](LICENSE). Free to use, modify, and distribute; **no warranty**, use at your own risk. Paper-trading only.

## Related projects

- [`F:\trump-code`](F:\trump-code) — Trump Truth Social tweets → US equity signal backtest; this system's `trump_signal` module draws on its key learnings

## Maintenance

Main memory files live at `C:\Users\masa\.claude\projects\f--fsi-skills\memory\`:
- `feedback_trading_style.md` — user preferences (chase strong trends / 3-tier indicators / no markdown in CMD / etc.)
- `feedback_regime_first.md` — regime single-source principle
- `feedback_backtest_gate.md` — any change requires backtest validation
- `feedback_news_pipeline.md` — news must be CLI-parsed into structured JSON before use
