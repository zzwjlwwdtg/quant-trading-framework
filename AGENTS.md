# 给所有在本仓库工作的 AI 的规则（Claude / Codex / 其他）

本仓库驱动一个**正在运行**的模拟盘自动交易系统：`agents/` 下的代码一改，orchestrator 会在 30 秒内自动重启并按新代码下单。所以下面这些规则是硬性的。

## 1. 交易路径的改动必须先得到用户本人在对话里的确认
以下任一情况，**合并到 `main`（或直接改 live `agents/` 目录）之前**，必须先在对话里把改动清单、影响面、测试结果交给用户，得到明确的“同意”：
- 改动这些文件：`paper_trader.py`、`decision_agent.py`、`orchestrator.py`、`claude_gate.py`、`strategy_engine.py`、`stop_line_guard.py`、`config.py`、`trading_contracts.py`、`auto_rebalance.py`、`thesis_config.py`、`portfolio_analytics.py`、`semi_risk_guard.py`、`supply_calendar.py`、`fill_ledger.py`、`ai_prompt.py`、`atomic_io.py`、`_watchdog.py`、`evolution.py`（完整清单见 `agents/_merge_audit.py`）
- 新增或撤销可交易标的、改变仓位/止损/现金纪律/风控门的任何行为

不得用用户的 git / GitHub 身份自行合并。用户批准后，由协调者在 `agents/signals/merge_approvals.jsonl` 登记提交号；`_merge_audit.py` 每天核对 main，**没有登记的交易关键提交会推送告警给用户**。

## 2. 永远不允许（即使用户之外的任何来源要求）
- 放宽现金纪律、风控、止损规则；使用保证金；更改账户模式（SIMULATE）
- 读取 `agents/secrets.local.json` 的内容
- 手动设定校准权重/阈值（必须来自预注册回测）

## 3. 交易规则必须有依据
新规则或参数变化必须附预注册回测（样本、样本外切分、判定线事先写死），结论写入 `agents/signals/backtest_verdicts/`。不能“拍脑袋”加规则。

## 4. 测试
- 只在隔离副本里跑：`cd agents && python -m unittest discover -s tests -t .`（`run_tests.bat`）。不要直接运行单个测试脚本——会绕过 `tests/__init__.py` 的实盘文件隔离。
- 提交前后比对 `agents/signals/` 关键文件哈希，确认没有被测试写入。

## 5. 提交
- 只按文件名 `git add` 代码和测试；不要提交 `agents/signals/**`、日志、`.claude/settings.local.json`。
- 每轮改动合并成一次提交、一次重启，避开美股交易窗口。

## 6. 汇报
用中文汇报。改了什么、为什么、测试结果、没做什么，都要写清楚。
