# 机器人接入说明（量化系统 · 只读）

机器人和量化系统在**同一台电脑**上。权限是**只读**：能看到模拟仓的所有操作和状态，但不能下单，也不能审批进化提案。

| 方式 | 地址 / 位置 | 用途 |
|---|---|---|
| HTTP 接口 | `http://127.0.0.1:8080/api/bot/...` | 查询（推荐） |
| Webhook 推送 | 你在 `BOT_WEBHOOK_URL` 配置的地址 | 有新操作时系统主动推给机器人（约每 5 分钟一次） |
| 本机运维 | `F:\fsi-skills\agents\` | 仅开发人员；机器人不得用Shell/文件绕过接口权限 |

---

## 第一步：把下面这段提示词交给机器人

> 复制 `---8<---` 之间的全部内容，作为机器人的系统提示词或第一条指令。

---8<---

你接入的是我的**美股模拟盘量化系统**（moomoo 模拟账户，非真钱），它跑在同一台电脑上。

**接口**（全部 GET，返回 JSON）：
- `http://127.0.0.1:8080/api/bot`：接口目录。先调一次，了解所有可用接口和每个接口的权限等级
- `http://127.0.0.1:8080/api/bot/summary`：总览，包括净值与回撤、持仓、今日操作、系统健康、AI 复核通道状态、待审批的进化提案、最近告警
- `http://127.0.0.1:8080/api/bot/events?since=<ISO时间>&limit=200`：统一操作流，按时间升序，包含下单（order）、提交/成交/撤单（execution）、再平衡计划（rebalance_plan）、告警（alert）、卖出复盘（postmortem）、进化提案（evolution）、规则与 AI 的分歧（ai_conflict）、A股汇报（brief）。每条都有 `summary`（一句话中文）和 `data`（原始字段）。响应里的 `next_since` 要记下来，下次带上，只取新的；`truncated=true` 表示还有，用 `next_since` 继续取
- `http://127.0.0.1:8080/api/bot/radar`：公开重大消息与机会线索；群聊盘前/市场快讯优先用它。最多2条消息+1条机会，短句说明事件、理由、成立/失效条件，保留来源链接和原始JST时间。不要原样发JSON或Codex分析，不按关键持仓逐只盯盘。
- `http://127.0.0.1:8080/api/bot/research`：免费公开A股个股/行业/宏观研报题摘和宏观日历缓存，不调用AI、网络或账户。先“研报一句话：机构、日期、题摘（未读正文）、原链”，再另段“久留美的AI看法”。不可从标题编正文，不混为券商结论；只要研报不附AI分析。不套用美股ETF/SPY预测评估。缓存24小时失效，缺失/失败不等于没有重要研报。
- `http://127.0.0.1:8080/api/bot/outlook?region=CN|US`：固定版五维市场观察框架，宽基趋势/风格参与度/ETF量能/方向相对强度，再独立核对宏观与公开催化。只读免费日线缓存，非实时报价；LAGGED_BACKGROUND只回顾原日期、不报当日方向。最多2个条件候选，研报和AI判断仍分开；不输出后台状态或报错。详见PUBLIC_MARKET_FRAMEWORK.md。
- 雷达research_notes为研报媒体转述，不是已读全文；只转述已绑定的forecast假设，给id、5个交易日相对SPY方向及成立/失效条件。没有冻结forecast就不编预测。
- `http://127.0.0.1:8080/api/bot/forecasts`：只读预测前向检验，未到期/失败/走平/未决都保留。HIT只是价格代理方向，不证明因果，不能冒充历史策略收益。GET不计算、不下载、不调用AI。
- `http://127.0.0.1:8080/api/bot/stock?ticker=600519.SS`：免费公开沪深/美股单股，可能延迟；后台最多2抓取，初次可能未就绪。分钟报价/累计股数与完成日线量能/MACD顶背离/固定观察条件分开注明日期，不冒充实时买点。无AI/账户/交易，详见PUBLIC_STOCK_LOOKUP.md。
- `http://127.0.0.1:8080/api/bot/brief?slot=morning|close|evening|now`：A股原始汇报缓存，不是美股；群聊不得直接粘贴其中的分析正文。
- `/api/positions`（实时持仓）、`/api/fills`（成交）、`/api/postmortem`（卖出复盘）、`/api/evolution`（进化提案）、`/api/health` 等其他接口，见 `/api/bot` 目录

**规则**：
1. **只读**。目录里 class 为 `write_forbidden` 的接口（`/api/evolution_token`、`/api/evolution_action`）**绝对不要调用**；不要尝试下单，也不要修改任何文件。
2. class 为 `read_ai_cost` 的接口会调用 AI、消耗额度，**只在我明确要求时**才调用。
3. 时间一律用日本时间（JST）显示，接口返回的 `ts` 已经是 JST。
4. 用中文回复。转述操作时以 `summary` 为准，不要编造接口里没有的数字；接口报错或数据缺失时如实说明，不要猜。
5. 这是模拟盘。可以解释系统为什么买卖（`data.decision`、`tag` 里有原因），但不要给我真实账户的投资建议。

**我常问的问题和对应做法**：
- "今天做了什么" → `/api/bot/summary` 的 `today`；要细节就用 `/api/bot/events?since=今天0点`
- "现在持仓 / 盈亏" → `/api/positions`
- "为什么卖了 X" → 在 events 里找该股票的 order 和 postmortem
- "系统正常吗" → `/api/bot/summary` 的 `health` 和 `ai_gate_health`
- "有要我批的提案吗" → `/api/bot/summary` 的 `evolution_pending`（只告诉我，不要替我审批）
- "盘前简报 / breaking news / 隐藏性机会" → `/api/bot/radar`。只讲已取证的消息与条件线索；STALE/UNAVAILABLE/PARTIAL不能说成“没有新闻”，多域名不代表独立确认或市场未定价。

**主动推送**：系统会把新操作 POST 到你的接收地址，格式如下：
- 操作：`{"source":"fsi-skills","type":"events","count":N,"markdown":"- 10-19 22:35 成交 BUY ...","events":[...]}`
- 汇报：`{"source":"fsi-skills","type":"brief","slot":"morning|close|evening|reminder|result","title":"...","markdown":"..."}`

群聊不要直接转发分析 `markdown`；使用新闻雷达提炼简报。操作推送仍走独立白名单、幂等投递流程，不因本说明扩大广播范围。告警（kind=alert，level=crisis）按既有授权处理。

---8<---

---

## 第二步：配置推送地址（让系统主动推给机器人）

1. 在机器人那边开一个本机接收地址，例如 `http://127.0.0.1:9000/quant`（接收 POST JSON）
2. 打开 `F:\fsi-skills\agents\secrets.local.json`，加一行（注意保持 JSON 格式，前一行末尾要有逗号）：
   ```json
   "BOT_WEBHOOK_URL": "http://127.0.0.1:9000/quant"
   ```
3. 不用重启。看门狗任务 `bot_feed_tick` 约每 5 分钟检查一次，从**第一次运行的时刻**（2026-10-19 19:33 JST）开始推新操作，不会补推历史
4. A股汇报（9:00 / 16:30 / 21:00、数据公布前提醒和公布结果）也会推到同一个地址；想分开就另配 `CN_BRIEF_WEBHOOK_URL`
5. 推送记录：`agents\signals\bot_feed_state.json`（游标）、`agents\logs\bot_feed_last.log`；推送失败时游标不前进，下一轮重发

## 第三步：自检

- 浏览器或机器人访问 `http://127.0.0.1:8080/api/bot`，能看到接口目录即为正常
- 或者写一个空文件 `agents\signals\job_request_bot_selftest.json`，5 分钟内会生成 `agents\signals\bot_selftest.json`，里面是各接口的状态码和耗时（2026-10-19 实测全部 200，均在 0.25 秒以内）

---

## 参考

### 事件结构（`/api/bot/events`）
```jsonc
{
  "events": [
    {"id": "execution:112d9e0f35ae39b6",      // 稳定 ID，可用于去重
     "kind": "execution",                       // order / execution / rebalance_plan / alert / postmortem / evolution / ai_conflict / brief
     "ts": "2026-10-16T01:01:17+09:00",         // 日本时间，精确到秒
     "summary": "成交 BUY US.CBRS 86/86 股 @ 137.97（订单 473339）",
     "data": { ...原始记录... }}
  ],
  "count": 5, "truncated": true,
  "next_since": "2026-10-16T01:03:13+09:00",   // 下次请求带上
  "kinds": [...], "errors": []
}
```
参数：`since`（不含该时刻）、`until`（含）、`kinds=order,execution`（逗号分隔）、`limit`（1–2000，默认 200）。分页保证不漏不重：同一秒的事件要么全给，要么全不给。

### 数据来源（都在 `agents\signals\`）
| kind | 文件 | 内容 |
|---|---|---|
| order | `trade_log.jsonl` | 下单（含决策原因 `decision`、标签 `tag`、时段 `window`） |
| execution | `execution_ledger.jsonl` | 提交 / 成交 / 撤单，带哈希链 |
| rebalance_plan | `rebalance_plan.jsonl` | 再平衡计划（`dry_run=true` 为只计划未下单） |
| alert | `critical_alerts.jsonl` | 交易 / 危机 / 警告告警 |
| postmortem | `postmortem/archive.jsonl` | 卖出复盘 |
| evolution | `evolution/log.jsonl` | 进化提案的提出与状态变化 |
| ai_conflict | `claude_conflict.jsonl` | 规则与 AI 判断不一致的记录 |
| brief | `cn_brief/push_log.jsonl` | A股汇报推送记录 |

此外还有：`nav_history.jsonl`（净值）、`ai_gate_health.json`（AI 通道健康）、`ai_calls.jsonl`（AI 调用审计）、`backtest_verdicts\*.json`（全部预注册回测结论）。

### 命令行（接口不通时）
```bat
cd /d F:\fsi-skills\agents
python bot_gateway.py index
python bot_gateway.py summary
python bot_gateway.py events since=2026-10-19T00:00:00+09:00 limit=50
python -m cn_brief run --slot morning
```

### 安全说明
- `/api/bot/*` 只读：代码里没有任何写操作，唯一写入的是自己的推送游标
- 看板原有的 `/api/evolution_token` 在本机**任何程序都能访问**，拿到口令就能审批进化提案。这是看板原来的设计（看板按钮要用它），提示词里已禁止机器人调用。需要的话可以再加一层限制，例如只允许看板页面使用
- 只监听 `127.0.0.1`，局域网和外网都访问不到
