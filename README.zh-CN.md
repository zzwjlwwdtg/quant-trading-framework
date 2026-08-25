# fsi-skills Trading Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**语言 / Language**：[English](README.md) · **简体中文**

多策略量化信号 + Codex 优先的本地 AI CLI 综合解读 + moomoo 模拟仓自动下单系统。

覆盖 **杠杆 ETF + 权重股 + 债券对冲 + 宏观 proxy**（TQQQ / SOXL / DRAM / MULL / GLD / NBIS / SHY / IEI / LITE / CBRS / USO / XLV / NVDA / MSFT / AAPL）+ 卫星单股。以日 K 动量为主信号、15min K 为辅助，结合聪明钱/期权信息、Trump Truth Social CLI 解析、黄金宏观因子、债市监控 + 期权 GEX/IV/Skew 完整解读。**宏观预测层 (v0.4)**：未来 45 天事件场景预测（Cleveland Fed nowcast + CME FedWatch）+ 金十数据风格债/股影响 label + 具体标的清单 + 美债救援政策工具追踪（11 工具 × 30Y 收益率反应）。决策通过 paper_trader 在 moomoo SIMULATE 账户落地（永不实盘）。

## Dashboard 预览

WebUI（`webui.bat` → http://127.0.0.1:8080）— 零依赖 http.server + 单页 dashboard，
覆盖 NAV / 板块 regime / Trump 情绪 / 黄金+石油宏观 / 事件日历 / 每标的信号 + 期权墙 + AI 分析。

![Dashboard 全景](docs/dashboard-full.png)

**每张标的卡包含**：多空共振条 + 建议动作/置信度 → 迷你 **期权墙 SVG**（call/put 成交分布 + spot 虚线 + Max Pain）→
**攻防位表格**（strike / 保费 / OI 仓位 / 名义敞口）→ **挤压风险**（gamma_up / put_break / max_pain_gravity）→
**C/P 比 + 小白解读**（Put 远大于 Call 时区分 ATM 恐慌 vs OTM 保险）→ **🤖 AI 即时分析**
（3 行结构化：综合 / 攻防 / 警示，10 只标的全覆盖，缓存 by 数据 hash）→ **🔗 上下游供应链**
（AI CLI 生成 upstream/downstream/peers + confidence 标记 + 可选 FMP peers 交叉验证，懒加载缓存 30 天）。

**🕸 D3 蜘蛛网全图**（Bloomberg SPLC 风格）：每张卡供应链区右上角「🕸 1 层 / 2 层」按钮 —
- **1 层**：直接邻居（快，用本地 cache）
- **2 层**：BFS 展开 2 跳（NVDA depth=2 = 90 节点 / 233 边）—— 下游公司的下游、上游的上游都出来
- 边颜色：🔴 supply / 🟢 customer / 🟡 peer；宽度 ∝ weight；实线=high confidence，虚线=medium/low
- **边 hover 显示理由**（例：`TSM → NVDA · 供应关系 · 独家代工 H100/H200/B100/B200 先进制程 GPU`）
- FMP 验证过的 peer 有绿色圆环
- 可拖动节点重排布局，Esc / 点击遮罩关闭
- URL 直达：`?graph=NVDA&depth=2`

**1 层示例（NVDA）**：![NVDA 直接邻居](docs/supply-chain-nvda-graph.png)

**2 层示例（NVDA 深度扩展）**：![NVDA 2 跳供应链](docs/supply-chain-nvda-depth2.png)

**📊 4 年基本面（財報狗风格）**：每张卡新增折叠面板，4 个免费指标（yfinance 拉，30 天缓存）：
- **CROIC 现金回报率** — FCF ÷ Invested Capital，>10% 健康 / >20% 摇钱树
- **Piotroski F 分数** — 9 项财务打分，7-9 强 / 4-6 普通 / 0-3 警示
- **金融借款** — ST + LT debt，连年上升 = 杠杆放大
- **现金周转循环** — DIO + DSO - DPO，越短越好，>120 天警惕库存压力
- 每格：最新值 + 涨/跌箭头 + 4 年 SVG sparkline + hover 用途解释
- ETF 用代表单股（TQQQ/SOXL → NVDA / DRAM/MULL → MU）；GLD 跳过
- **AI 分析里也会引用**（发现 CROIC 骤跌 / 借款激增 / Piotroski<4 时会主动警示）

**🔥 大单高亮 + 财报结合**：
- 期权 wall 的 OI ≥ 5K 手 或 名义敞口 ≥ $30M → 攻防位表格显示 🔥，迷你 SVG 图的柱子加深填充 + 顶部 🟠 圆点
- 「异常成交」（当日 vol > 0.5 × OI）→ hover 显示 ⚡
- 每张卡 attack/defense 面板顶部新增 **📅 财报徽章**（关联财报股 + T-N 天 + 隐含波动 ± IM%）
  - MULL/DRAM ← MU 财报；SOXL/TQQQ ← NVDA 财报
  - T ≤ 3 天 🚨 红色 / T ≤ 14 天 ⚠ 黄色 / T ≤ 60 天 📅 灰色
  - 期权到期日跨财报时标注「含财报风险」

**顶部**：📅 最近 45 天事件日历（FOMC / CPI / NFP / NVDA 财报），带小白 hint 说明每种事件对市场的影响。

**展开全部详情视图**：`?expand=all`（用于 README 截图 / 一览分析）— [见 dashboard-full-expanded.png](docs/dashboard-full-expanded.png)

## 主要特性

- **宏观预测层**（v0.4 新）— thesis_forecast 45 天事件场景（Cleveland Fed + CME FedWatch + prior）+ 政策工具追踪（回购/YCC/TGA/SLR × 30Y 反应）+ Trump 归因
- **期权结构完整解读**（v0.4 扩展）— OI-based walls + 联合 band + GEX+IV+Skew stock verdict + 保费/OI/名义敞口 + 杠杆 ETF 结构价换算
- **AI 新闻结构化生态** — 所有 RSS / Truth Social 先调统一 AI CLI（默认 Codex）拆为固定 JSON schema 再供规则消费；Google News 定题搜索作为主信源
- **Regime 单一源** — pre-open 算定 → 全系统读单一源，禁止多处独立检测
- **TECHNICAL_ONLY 默认 ON** — 决策只看技术面，消息面（Trump / breaking_news / 事件日历）仅 banner，不进 decision_agent 评分
- **回测门控** — 任何信号/决策改动必须跑 `_backtest_modules_accuracy.py` 等回测脚本，hit rate 不退化才合并；训练集 N≤5 立 hard rule 是过拟合
- **可解释信号栈** — 固定动量/技术规则 + 共振（confluence 按资产类别校准 hit rate）+ 期权环境；自动进化分数不再进入实时决策
- **三巫日识别** — 自动识别每季 3/6/9/12 月第三周周五，GEX 代理 + 关联财报提示
- **AI 目标价** — AI CLI 输出结构化 JSON（entry_ref / stop_ref），paper_trader 自动挂限价 + SELL STOP

### 机构级测量层（小资金账户口径）

- **组合风险与归因** — NAV/现金/名义及杠杆调整暴露、历史或备用协方差 VaR/ES、板块压力测试、相关性/风险贡献、SPY beta/alpha/跟踪误差和盈亏归因。
- **杠杆 ETF 路径风险** — TQQQ/QQQ、SOXL/SOXX、MULL/MU 按日复位模拟，分开展示终点杠杆、波动率折损、费用与实现跟踪残差。
- **成交与审计** — 对齐券商的 submitted/filled/partial/cancelled 状态，计算实际成交率和 implementation shortfall，JSONL 执行账本带防篡改 hash chain。
- **Point-in-time 数据控制** — 追加式 observed/effective 时间、新鲜度/价格/指标完整性检查；核心行情无效时拦截新风险，但不阻断减仓退出。
- **Purged walk-forward** — 固定规则用时序样本外 fold 验证，训练/测试之间带 purge 和 embargo；未通过的规则不晋级为当前形态信号。
- **进攻型小资金策略** — 理论 bid/ask 点差、模型化市场冲击和策略容量都不减仓、不拦单，但仍复盘券商实际成交。`SIM_ACTIVE` 中 VaR/压力/集中度只在 dashboard 提示。

## 标的列表（[`agents/config.py`](agents/config.py)）

**核心杠杆 ETF (TICKERS)**：

| Ticker | 类型 | 杠杆 | 备注 |
|---|---|---|---|
| TQQQ | NDX 杠杆 ETF | 3x | 科技多头主仓 |
| SOXL | SOX 半导体 杠杆 ETF | 3x | 半导体多头 |
| DRAM | Roundhill Memory ETF | 1x | 存储芯片板块（Micron + SK Hynix + Samsung 暴露）|
| MULL | Micron 杠杆 ETF | 2x | DRAM/NAND 龙头单股 |

**扩展 tracked 标的 (TRACKED_TICKERS + GLD)**：

| Ticker | 类型 | 用途 |
|---|---|---|
| GLD | 黄金 ETF | 避险 / 宏观对冲 |
| NVDA | 半导体链条领头 | 看它就知道 SOXL/DRAM 方向 |
| MSFT | 云 AI + FAANG | TQQQ/QQQ 权重股 |
| AAPL | QQQ 最大权重 | 苹果链跟踪 |
| **NBIS** | Nebius AI cloud pure-play | 2026-Q3 thesis：云端消费增长表达 |
| **SHY** | 1-3Y Treasury ETF | 2026-Q3 thesis：2Y 债券多仓代理 |
| **IEI** | 3-7Y Treasury ETF | 2026-Q3 thesis：5Y 债券多仓代理 |
| LITE | Lumentum 光通信 | AI DC 400G/800G 光模块 |
| CBRS | Cerebras Systems | WSE 晶圆级 AI inference，NVDA 竞品 |
| USO | WTI 原油 ETF | 通胀 proxy + geopolitics gauge |
| XLV | 医疗 ETF | 防御性板块（JNJ/UNH/LLY/PFE 权重）|

**JP 板块**（11 只 short name：長弘 / TDK / ARE / MUFG 等，见 [`jp_watch_contracts.py`](agents/jp_watch_contracts.py)）

## 快速开始

```cmd
:: 1. 安装依赖（首次）
cd f:\fsi-skills\agents
setup.bat

:: 2. 配置 secrets（首次）
copy secrets.example.json secrets.local.json
:: 编辑 secrets.local.json 填入 FRED_API_KEY 和 MOOMOO_ACC_ID
:: ⚠ secrets.local.json 已在 .gitignore，永远不要 commit — 详见 SECURITY.md

:: 3. 启用 pre-commit 安全钩子（首次）
git config core.hooksPath .githooks
:: 拦截 sk-... / ghp_... / AKIA... 等常见 secret 意外 commit

:: 4. 启动 moomoo OpenD（一直开着）

:: 5. 选择运行模式
run.bat        :: 长跑（orchestrator，每 5min 检查 + 5 个 ET 窗口）
snap.bat       :: 一键当日快照（regime + Trump + 信号 + 期权 + AI 解读）
tools.bat      :: 工具菜单（trader status / regime / picks / flatten 等）
backtest.bat   :: 回测菜单（regime / news / trump / modules / V-bounce）
trump.bat      :: Trump signal 单独查
weekly.bat     :: 周末跑一次模块准确率回测，刷 signals/module_accuracy.md
```

AI CLI 默认策略是 `AI_CLI_PRIMARY=codex`、`AI_CLI_FALLBACK=none`。因此 Codex
失败时不会自动调用 Claude。若临时需要旧行为，可在启动前显式设置
`AI_CLI_PRIMARY=claude` 与 `AI_CLI_FALLBACK=codex`；`snap_public.bat` 为保护额度，
始终强制 Codex 且无 Claude fallback。

## 配置文件

| 文件 | 用途 | 是否入 git |
|---|---|---|
| [`agents/config.py`](agents/config.py) | 公共配置：TICKERS / 杠杆系数 / 时间窗口 / 阈值 | ✅ |
| [`agents/secrets.example.json`](agents/secrets.example.json) | 敏感配置模板（占位） | ✅ |
| `agents/secrets.local.json` | **真实** FRED_API_KEY / MOOMOO_ACC_ID 等 | ❌（.gitignore） |
| `.claude/settings.local.json` | Claude Code 权限白名单 | ✅ |
| [`SECURITY.md`](SECURITY.md) | 敏感信息管理规则 / pre-commit hook / 漏洞报告方式 | ✅ |
| [`.githooks/pre-commit`](.githooks/pre-commit) | 拦截误 commit 的 API key（`git config core.hooksPath .githooks` 启用） | ✅ |

## 入口脚本

- [`run.bat`](agents/run.bat) — orchestrator 长跑，5 个 ET 窗口（08:30 / 09:20 / 10:00 / 12:00 / 15:45）自动触发
- [`snap.bat`](agents/snap.bat) — 一次性快照，含 regime / Trump / 三巫日 / 信号 / 决策 / 事件日历 / AI 综合解读
- [`tools.bat`](agents/tools.bat) → [`tools_menu.py`](agents/tools_menu.py) — 交互菜单
- [`backtest.bat`](agents/backtest.bat) → [`backtest_menu.py`](agents/backtest_menu.py) — 回测菜单
- [`trump.bat`](agents/trump.bat) — 仅看 Trump signal banner
- [`weekly.bat`](agents/weekly.bat) — 一键刷 module_accuracy.md（周末跑）

## 系统架构（数据流）

```
                        ┌─── moomoo OpenD（实时 quote + paper 下单）
                        │
   FRED ────┐           │      ┌─── Trump signal (CNN truth_archive)
            ├── data_feeds + market_watch ──┤
   yfinance ┘           │      └─── 期权链 (yfinance options)
                        │
                        ↓
            [Block 1-11 信号采集层]
                        ↓
   regime_today.py（pre-open 单一源）
                        ↓
   ┌──────────────────────────────────────┐
   │ decision_agent._etf_rules /          │
   │ _gold_rules → action + conf + stop_ref│
   │ (含 V 反弹 / Trump override / 事件分级)│
   └──────────────────────────────────────┘
                        ↓
   claude_gate.py（下单前二审；run.bat 默认失败关闭）
                        ↓
   ┌──────────────────────────────────────┐
   │ paper_trader.execute() 7 层下单链：    │
   │  ① 窗口门槛 → ② 纪律性管理 → ③ 去重    │
   │  ④ action 分流 → ⑤ vol-target 仓位     │
   │  ⑥ 限价组装 → ⑦ moomoo SDK 提交       │
   └──────────────────────────────────────┘
                        ↓
                  moomoo SIMULATE 账户

   ＊ AI CLI 报告解读层（不在下单链，与上面的二审 gate 分开）：
     run_cycle 完成后默认调 Codex CLI 综合 11 个 block
     输出 700-1000 字人话报告 + 结构化 JSON 目标价
     paper_trader 下一个 cycle 会读 JSON 调整限价/止损
```

## 信号模块（Block 编号供 AI 依据标注）

| # | 模块 | 文件 |
|---|---|---|
| ① 基础报告 | [`report_generator.py`](agents/report_generator.py) |
| ② 固定技术形态回测 | [`strategy_engine.py`](agents/strategy_engine.py) `generate_pattern_leaderboard` |
| ③ 信号实况（共振） | [`confluence.py`](agents/confluence.py) + [`market_watch.py`](agents/market_watch.py) |
| ④ 事件日历 | [`events_watch.py`](agents/events_watch.py) |
| ⑤ Trump signal | [`trump_signal.py`](agents/trump_signal.py) |
| ⑥ 期权墙 | [`option_walls.py`](agents/option_walls.py) |
| ⑦ MACD + ADX | [`market_watch.py`](agents/market_watch.py) |
| ⑧ SOX PCA | [`pca_sox.py`](agents/pca_sox.py) |
| ⑨ 黄金宏观 | [`gold_macro.py`](agents/gold_macro.py) |
| ⑩ 期权风险（三巫日 / GEX / 关联财报）| [`option_walls.py`](agents/option_walls.py) `get_options_risk_signal` |
| ⑪ 日本社交推荐 | [`jp_social_reco/`](agents/jp_social_reco/) |

## 决策模块

| 文件 | 作用 |
|---|---|
| [`agents/decision_agent.py`](agents/decision_agent.py) | 规则引擎：`_etf_rules` / `_gold_rules` 出 action + conf + stop_ref |
| [`agents/regime_today.py`](agents/regime_today.py) | regime 单一源（pre-open 写 regime_state.json）|
| [`agents/paper_trader.py`](agents/paper_trader.py) | moomoo SIMULATE 下单 + 仓位管理 + 止盈止损 |
| [`agents/ai_prompt.py`](agents/ai_prompt.py) | Codex 优先的统一 AI CLI 路由 + prompt 模板 + 结构化 JSON 解析 |
| [`agents/claude_gate.py`](agents/claude_gate.py) | AI 二审 gate（保留旧文件名兼容；pre-trade approval）|
| [`agents/portfolio_analytics.py`](agents/portfolio_analytics.py) | 组合暴露、VaR/ES、压力、相关性、基准与归因 |
| [`agents/leveraged_etf_risk.py`](agents/leveraged_etf_risk.py) | 日复位杠杆、波动率折损与路径情景 |
| [`agents/execution_analytics.py`](agents/execution_analytics.py) | 小资金执行口径、实际成交与审计链 |
| [`agents/data_quality.py`](agents/data_quality.py) | Point-in-time 存储、新鲜度/覆盖检查与下单数据门控 |

## 回测

| 脚本 | 验证什么 |
|---|---|
| [`_backtest_modules_accuracy.py`](agents/_backtest_modules_accuracy.py) | 各模块 × 1d/5d/10d/20d hit rate（基础回归测试） |
| [`_backtest_regime_fix.py`](agents/_backtest_regime_fix.py) | regime 单一源改动前后决策一致性 |
| [`_backtest_news_pipeline.py`](agents/_backtest_news_pipeline.py) | CLI 解析 vs keyword 法（事件落地判断准确率） |
| [`_backtest_trump_signal.py`](agents/_backtest_trump_signal.py) | Trump 信号方向命中率（vs trump-code baseline） |
| [`_backtest_v_bounce.py`](agents/_backtest_v_bounce.py) | V 反转追买（杠杆 ETF 5d/10d/20d） |
| [`_backtest_gold_macro.py`](agents/_backtest_gold_macro.py) | 黄金宏观信号注入回测 |
| [`backtest_engine.py`](agents/backtest_engine.py) | Lite / Mid 两档全系统回测 |
| [`research_validation.py`](agents/research_validation.py) | 固定规则的 purge/embargo 时序样本外验证 |

回归测试（在 `agents/` 目录运行）：`python -m unittest discover -s tests -v`

输出报告：[`agents/signals/module_accuracy.md`](agents/signals/) （注：实际文件被 gitignore 排除）

## 备份

- 大改动前：`python _make_backup.py` → `backups/agents_backup_<时间戳>_with_AB.zip`
- 重要里程碑：`git tag v0.x.x && git push --tags`

## 已知限制

- 不提供实盘交易（仅 moomoo SIMULATE 账户）
- DRAM ETF 历史数据短于老 ETF，长周期指标的有效样本可能较少
- AI CLI 调用通常延迟 30-60s；半小时公共快照被强制锁定为 Codex，不会静默消耗 Claude 额度
- 不需要模型 API key — 使用本机 Codex CLI 已保存登录；Claude 仅作为显式选择或显式 fallback

## Changelog

每个 tag 对应的主要变更。详细 diff 见 [GitHub Releases](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases)。

### v0.4.0 — 2026-08-25

**大版本**：宏观预测层 + 期权结构完整解读 + AI 新闻结构化生态成熟

**新功能**：

- **宏观预测层**（新）
  - [`thesis_forecast.py`](agents/thesis_forecast.py) 未来 45 天事件 × 3 场景（dovish/base/hawkish）× 概率 → 概率加权期望 delta_pp
  - 概率数据源: **Cleveland Fed InflationNowcast**（CPI/PCE）+ **CME FedWatch**（FOMC）+ 历史 prior（NFP/PPI/Retail）
  - 每事件配 **金十数据风格 label**: 债/股 强利多/利多/偏多/无方向/偏空/利空/强利空 + `_ASSET_MAP` 12 组事件×方向 → 4×4 具体标的清单（利多 USD/能源/银行/黄金 vs 利空 科技股/长债/REITs/growth）
  - [`policy_toolkit_tracker.py`](agents/policy_toolkit_tracker.py) **美债救援政策工具追踪**：Google News RSS + Fed 官方 → CLI 结构化 → 匹配 11 个预定义工具（回购/YCC/TGA/SLR/FIMA/汇市干预/QE/QRA/发债久期/Fed 鸽鹰口头）→ ^TYX 算 30Y T+0/T+1d 反应 bp
  - [`trump_thesis_attribution.py`](agents/trump_thesis_attribution.py) Trump 帖子对 cut_prob 影响归因（48h 窗口）
  - [`bond_ai_interpret.py`](agents/bond_ai_interpret.py) AI CLI 大白话解读 bond_monitor 数字
  - [`thesis_history.py`](agents/thesis_history.py) 180d cut_prob 演化时序
  - [`cleveland_nowcast.py`](agents/cleveland_nowcast.py) Cleveland Fed nowcast 数据抓取
- **期权结构完整解读**（重点扩展）
  - 每张卡的 **期权墙 SVG**（call/put 成交分布 + spot 虚线 + Max Pain + Pin/gamma_flip）
  - **OI-based walls (asymmetric)**: call above spot / put below spot，避免 ITM 保护性 put OI 污染 defense 位
  - **联合 wall bands**: 相邻多 strike 合并（例 AAPL $300+$295 = $285M 联合防御）
  - **攻防位 4 列表格**: 行权价 / 单手保费（bid/ask mid） / OI 仓位 / 名义敞口
  - [`gex_calc.py`](agents/gex_calc.py) **GEX + IV Regime + Skew** 三合一 stock verdict：3 类风险（breakdown/event/chase_high）+ 3 类机会（buy_now/add_more/reduce）+ 关键价位（阻力/pin/支撑）
  - 挤压风险: gamma_up / put_break / gamma_band_up / put_band_break / max_pain_gravity
  - **杠杆 ETF 结构价换算**（QQQ→TQQQ, SOXX→SOXL）: 3 个月历史折损校准 + spot-anchored + 到期路径估算
  - 大单标记 🔥（OI ≥ 5K 或名义 ≥ $30M）+ 异常成交 ⚡（vol > 0.5×OI）
  - [`option_flow.py`](agents/option_flow.py) 新增期权流扫描（quote-side 推断，与 wall 结构分离，score cap 69 防止单独触发自动减仓）
  - [`moomoo_data.py`](agents/moomoo_data.py) openD 期权链主源（JP 支持 + 速度）+ yfinance fallback
- **AI 新闻结构化生态**
  - [`news_analyzer.py`](agents/news_analyzer.py) 统一 RSS → CLI → 固定 JSON schema pipeline（8 个事件枚举 + 归一化）
  - Google News RSS 定题搜索作为主信源（Reuters/Bloomberg/CNBC/WSJ 聚合）
  - CLI 分批（20/批 × 120s）避免超时
- **Dashboard 层**（重排 + 新面板）
  - 顶部 macro block：bond_monitor + AI 解读 + thesis 历史 + **未来 45 天事件场景预测** + **美债救援政策工具追踪** + Trump 归因
  - 每张信号卡：多空共振条 → 期权墙 SVG → 攻防位表格 → 挤压风险 → **⚡ 期权判读**（GEX+IV+Skew 综合）→ C/P 比 → 墙 OI 失衡解读 → 财报徽章 → 供应链 D3 蜘蛛网（depth=1/2）→ 4 年基本面（財報狗风格：CROIC/Piotroski/借款/现金周转）
  - 静态快照模式（`STATIC_SNAPSHOT_MODE`）→ GitHub Pages 部署
- **标的扩容**（5 → 15+）
  - 核心杠杆 ETF: TQQQ/SOXL/DRAM/MULL/GLD
  - 权重股: NVDA/MSFT/AAPL
  - 2026-Q3 thesis: NBIS（AI 云）+ SHY/IEI（债券对冲）
  - AI 链: LITE（光通信）+ CBRS（Cerebras）
  - 宏观 proxy: USO（油）+ XLV（医疗）
  - JP 板块（长弘/TDK/ARE/MUFG 等 11 只 short name）

**关键 bug 修复**：

- **Gold 假 SELL** — `_gold_rules` 用 hardcoded `_tech_bear()`, RSI 82 → bear=2 → SELL 触发；bypass 了 `_calibrate_confidence` 已将 RSI/CCI overbought 在 gold 上剔除的 hit rate 29.5% 事实。Fix: 传 confluence 到 `_gold_rules`, 用 `bull_weighted/bear_weighted`
- **Confluence 未 attach 到信号** — `strategy_runner.emit_signal` → `get_decision` 计算但不带出；只有 orchestrator 手工 attach。Fix: `get_decision` 统一 attach `result["confluence"]`
- **僵尸信号文件** — `US.GLD_latest.json` 等 2026-05 老文件仍在 dashboard 显示旧 SELL；`rm` 清理
- **AI 宏观暂时不可用** — `f'{None:+.2f}'` 崩溃；加 `if x is not None else "数据不足"` 防御
- **snapshot endpoints 缺失** — `/api/thesis_forecast`, `/api/thesis_history`, `/api/trump_attribution`, `/api/policy_toolkit` 未加到 `GLOBAL_ENDPOINTS` → docs snapshot 缺 4 个 JSON
- **URL 参数不匹配** — dashboard `fetchJson('/api/thesis_forecast?days=45')` 但 snapshot 存为无参数文件；改为无参 URL
- **stale market_impact** — thesis_forecast cache 生成早于 `_market_impact_from_expected`, 前端 fallback "中性"；purge cache 后正确显示 CPI -7.88pp = 债/股 强利空
- **TECHNICAL_ONLY 默认 ON** — Trump/breaking_news/事件日历退化为 banner, 不进 decision_agent 评分（避免消息面噪音）

**新记忆**：

- [feedback_technical_only_mode.md](memory) 决策只看技术面
- [feedback_oos_required.md](memory) 自动减仓/反转信号必须 OOS 验证
- [feedback_model_selection.md](memory) 简单任务用 Haiku/Sonnet，Opus 只留深度推理
- [project_thesis_2026Q3.md](memory) 2026-Q3 二阶导拐点 thesis（bond+cloud long, avoid semi）

### [v0.3.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.3.0) — 2026-07-28

**大版本**：15 项系统能力升级 + 主动推送 + WebUI + 开源就绪

**新功能（15 项 checklist 落地）**：

- **P1 观察层**
  - [`docs/EDGE.md`](docs/EDGE.md) 明确系统 edge 来源、伪 edge、非 edge
  - [`_benchmark_report.py`](agents/_benchmark_report.py) NAV vs SPY/QQQ 周报（Sharpe/Max DD/α）
  - [`_trade_postmortem.py`](agents/_trade_postmortem.py) BUY/SELL 配对 + P&L 归因
- **P2/P3 风控 + 仓位**
  - 相关性组仓位上限（`tech_3x` 50% / `tech_2x` 30% / `single_high_beta` 30% 等 6 组）
  - Probe 试探仓位（低 conf = 30% 仓位）+ 金字塔加仓（≤3 层）
  - Half-Kelly 仓位微调（min 10 trades，cap [0.5, 1.5]）
- **P4 决策/时序**
  - Sitting 强制确认期（持仓 < 3 天信号 SELL 不放行）
  - 连续亏损暂停（3 笔连亏 → 24h 停开新仓）
  - HMM meta-regime 单向收紧（volatile/crisis/bear → bull_thresh +1，绝不放宽）
  - **反向 ETF OOS 拒绝**：SQQQ/SOXS 触发后 5d 上涨率仅 26.5%（-33pp 反指标）→ 不上线
- **P5 基础设施**
  - `TRADER_LIVE_FRACTION` env var 灰度切换（0.0-1.0）
  - [`_data_source_health.py`](agents/_data_source_health.py) 数据源体检
- **主动推送** [`notifications.py`](agents/notifications.py)
  - Discord webhook + Telegram bot 双通道（有哪个用哪个）
  - 5 分钟 dedup 防刷屏
  - Hook 点：trade 成交 / crisis regime / watchdog 死机重启 / 连续亏损暂停
- **WebUI Dashboard**（零依赖 Python 自带 `http.server`）
  - [`webui.py`](agents/webui.py) 8 个 API 端点（health/nav/positions/trades/log/hmm/signals/banners/ai_analysis）
  - [`dashboard.html`](agents/dashboard.html) 单页 SPA + Chart.js + marked.js
  - NAV 时序图 + 每标的信号卡（bull/bear 共振条 + regime 徽章）+ HMM 状态 + Trump/Gold Macro 卡片 + AI 分析 markdown 渲染
  - 30s 自动刷新，仅绑 127.0.0.1
  - [`webui.bat`](agents/webui.bat) 启动
- **Watchdog + 自动重启** [`_watchdog.py`](agents/_watchdog.py)
  - Windows Task Scheduler 每 30 分钟检查 orchestrator PID
  - 死了自动清 stale lock + 用 pythonw 静默重启（无窗口）
- **开源就绪**
  - MIT [`LICENSE`](LICENSE)
  - [`SECURITY.md`](SECURITY.md) 敏感信息管理
  - [`.githooks/pre-commit`](.githooks/pre-commit) 拦截 sk-/ghp_/AKIA 等 secret 模式
  - `.gitignore` 加固 `.env.*` / credentials / pem / id_rsa
- **回测科学化**
  - OOS 验证成为硬门槛（14 OOS 标的样本 ≥ 30，5d edge ≥ 8pp 才上线）
  - `_backtest_overheated_oos.py` / `_backtest_divergence.py` / `_backtest_trend_capture.py` / `_backtest_inverse_etf.py` 全套 OOS 验证脚本

**关键 bug 修复**：

- **conf_min 未按 /5 量程缩放** — TECHNICAL_ONLY 下所有信号被静默跳过（fix `conf_min * scale/10`）
- **A+B / C 方案回滚** — OOS 证实 CAUTION 层 + overheated 多日累积 + 顶背离 均为反指标（-14~17pp）
- **.bat 中文 REM 在日语 CP932 下解析错乱** — 全部 ASCII-only
- **LOG_PATH 跨午夜不切** — 加 `_DailyLogHandler` 自动切文件

**新记忆**：

- [feedback_technical_only_mode.md](memory) 消息面仅 banner，不进决策评分
- [feedback_oos_required.md](memory) 训练集 N≤5 立 hard rule 是过拟合
- [feedback_bat_ascii_only.md](memory) .bat 中文 REM 会让日语 cmd 误解析

### [v0.2.1](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.2.1) — 2026-06-23

财报期权隐含 move 屏蔽 + 跨午夜日志切换

- **新增** [`option_walls.get_earnings_implied_move(stock)`](agents/option_walls.py)：读财报当周 ATM straddle 算 implied ±%（含 ATM±1 smoothed）+ C/P vol ratio + IV
- **新增** [`decision_agent._apply_earnings_guard()`](agents/decision_agent.py)：按 `leveraged_im = im × ETF leverage` 分档屏蔽
  - `> 20%` → 强制 HOLD；`12-20%` → conf-3（<6 降 HOLD）；`6-12%` → conf-2；T-1/T-0 一律 HOLD
- orchestrator 每 cycle 注入 `events.earnings_implied_move`（MU/NVDA 等关联股 30 天内）
- 回测 ([`_backtest_earnings_implied_move.py`](agents/_backtest_earnings_implied_move.py))：MU 5 年 20 次财报，MULL 实测 beta=2.08x（验证设定）；MULL |move|>20% 概率 25%；2024-12-18 MULL 单日 -32.74%
- 实战：MU 6-25 财报，MULL/DRAM `WATCH_BUY` 自动降 HOLD
- **bug 修复**：[`config.LOG_PATH`](agents/config.py) 跨午夜不切——新增 `get_today_log_path()` + [`notifier._DailyLogHandler`](agents/notifier.py)，emit 时按当天日期切换文件

### [v0.2.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.2.0) — 2026-06-22

JP Social Reco 接入 + Block ⑫

- 接入 [`jp_social_reco`](agents/jp_social_reco/) 子系统到主框架（snap.bat / orchestrator / AI prompt）
- 新增 `get_jp_social_with_backtest()`：信号 + 创作者历史胜率 + ticker 按 horizon 回测命中
- 新增 `format_jp_social_banner_enhanced()`：含星标 / thesis / 风险 / 时间维度 / 创作者胜率 / 回测命中
- AI prompt 加 **block ⑫** JP 博主推荐，要求 ≥★★★ 标的必须列入早盘可参考清单
- 星标算法（按 mentions × creators 数）：★★★★★ / ★★★★ / ★★★ / ★★ / ★
- bug 修复：price_check 字段附加方式 + creator_accuracy 取 creators list

### [v0.1.0](https://github.com/zzwjlwwdtg/fsi-skills-agents/releases/tag/v0.1.0) — 2026-06-21

初始版本

- Regime 单一源（pre-open 算定 + -5% crisis override）
- 5 标的：TQQQ / SOXL / DRAM / MULL / GLD
- 进化规则共振 + module accuracy 250d 回测（quant 20d 70-78%）
- Trump signal CLI 解析（80% hit rate vs trump-code baseline）
- 黄金宏观（real_rate + DXY + WALCL + FOMC + oil + 10Y）
- 期权盯盘（Call/Put Wall + Max Pain + GEX + 三巫日 + 关联财报）
- V 反弹追买（1x WATCH_BUY / 杠杆 LONG_HOLD 分流）
- 事件分级（CPI/FOMC/NFP critical, PCE/PPI/Retail high, 财报 moderate）
- Codex 优先的 AI CLI 综合解读 + 结构化 JSON 目标价（A 方案）
- decision_agent 自动算 stop_ref（B 方案）→ paper_trader 挂真实 SELL STOP
- ticker prefix bug 修复（_scaled_pct 兼容 "SOXL" 和 "US.SOXL"）
- trump_score 方向不对称（bullish 不加 risk）
- 7 层 vol-target 下单链
- Secrets 隔离到 agents/secrets.local.json（gitignore）

## License

私有项目，未授权不得分发。

## 相关项目

- [`F:\trump-code`](F:\trump-code) — Trump Truth Social 推文 → 美股信号回测，本系统的 trump_signal 模块借鉴其关键经验

## 维护

主要 memory 在 `C:\Users\masa\.claude\projects\f--fsi-skills\memory\` 下：
- `feedback_trading_style.md` — 用户偏好（追高强趋势 / 指标三档 / CMD 不用 markdown 等）
- `feedback_regime_first.md` — Regime 单一源原则
- `feedback_backtest_gate.md` — 任何改动必须回测验证
- `feedback_news_pipeline.md` — 新闻进入系统前必须 CLI 拆为结构化 JSON
