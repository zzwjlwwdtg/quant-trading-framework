# cn_brief 机器人调用说明

A股每日研报调查 + 中美日重大经济数据的汇报与提醒。**只读**：不下单，不改动交易系统的任何状态。

- 代码位置：`F:\fsi-skills\agents\cn_brief\`
- 运行环境：Windows，`C:\Users\masa\AppData\Local\Programs\Python\Python312\python.exe`
- **工作目录必须是 `F:\fsi-skills\agents`**（命令写作 `python -m cn_brief ...`）
- 数据源：东方财富研报中心（个股 / 行业 / 宏观研报）、百度股市通财经日历（时间是北京时间，输出时换算成日本时间 JST）

---

## 1. 三种调用方式（选一种）

### A. 命令行（最简单，推荐给任何机器人库）

```bat
cd /d F:\fsi-skills\agents
set PYTHONUTF8=1
python -X utf8 -m cn_brief run --slot now            :: 即时汇报，Markdown 输出到标准输出
python -X utf8 -m cn_brief run --slot now --json     :: 同上，输出 JSON
python -X utf8 -m cn_brief calendar --days 1         :: 只看经济日历（中美日重大数据）
```

机器人读取**标准输出**并原样转发即可。标准输出是 UTF-8。

### B. 读文件（不想自己执行命令时）

每次生成都会覆盖写入：

| 文件 | 内容 |
|---|---|
| `agents\signals\cn_brief\latest_morning.md` / `.json` | 9:00 盘前汇报 |
| `agents\signals\cn_brief\latest_close.md` / `.json` | 16:30 收盘后汇报 |
| `agents\signals\cn_brief\latest_evening.md` / `.json` | 21:00 晚间数据提醒 |
| `agents\signals\cn_brief\latest_now.md` / `.json` | 最近一次即时调用 |
| `agents\signals\cn_brief\archive\` | 历史存档（最近 400 份） |
| `agents\signals\cn_brief\push_log.jsonl` | 每次定时推送 / 提醒的记录 |

### C. Python 直接调用（机器人和本系统在同一台电脑、同一个 Python 时）

```python
import sys
sys.path.insert(0, r"F:\fsi-skills\agents")
from cn_brief import build_brief

b = build_brief("now", use_ai=True)   # slot: morning / close / evening / now
text = b["markdown"]                  # 直接发给聊天
errors = b["errors"]                  # 非空表示部分数据没拿到（仍然有能拿到的部分）
```

---

## 2. 命令参考

### `run` —— 生成一份汇报

```
python -m cn_brief run --slot {morning|close|evening|now} [--json] [--no-ai] [--push]
```

| 参数 | 说明 |
|---|---|
| `--slot now` | 即时版：上一个交易日到今天的研报 + 过去 24 小时已公布的数据 + 未来 24 小时将公布的数据 |
| `--slot morning` | **盘前定调（精简版，约 10 秒，不用 AI）**：一句话定调 + 今日关键时刻 + 外围隔夜（附按回测推算的开盘幅度）+ 风险名单 + 特殊日。见第 4.1 节 |
| `--slot close` | 只看**今天**新出的研报 + 过去/未来 24 小时数据 |
| `--slot evening` | 不含研报；只列今晚到明早 14 小时内将公布的中美日重大数据 |
| `--json` | 输出完整 JSON（结构见第 4 节） |
| `--no-ai` | 不生成 AI 要点摘要：更快（约 10 秒），不消耗 Claude / Codex 额度 |
| `--push` | 同时推送到已配置的渠道（见第 5 节）。**机器人自己转发时不要加**，否则会重复 |

耗时：不带 AI 约 10–20 秒；带 AI 约 30–90 秒（AI 超时上限 240 秒）。建议机器人先回复"生成中…"。

### `calendar` —— 经济日历

```
python -m cn_brief calendar [--days N] [--all] [--json]
```
- 默认只列**中国 / 美国 / 日本**的重大数据，从今天（北京时间）起 N 天，时间为 JST
- `--all` 列出全部国家、全部数据

### `tick` —— 定时调度（已由看门狗自动调用，机器人一般不用管）

```
python -m cn_brief tick [--dry-run]
```

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数错误 |
| 2 | 部分数据获取失败；输出里仍有能拿到的部分，原因写在 JSON 的 `errors` |

---

## 3. 建议的机器人命令映射

| 聊天命令 | 执行 |
|---|---|
| `/研报` 或 `/brief` | `python -m cn_brief run --slot now` |
| `/研报快` | `python -m cn_brief run --slot now --no-ai` |
| `/日历` | `python -m cn_brief calendar --days 1` |
| `/本周日历` | `python -m cn_brief calendar --days 7` |
| `/今晚` | `python -m cn_brief run --slot evening --no-ai` |
| `/盘前` `/收盘` | 直接读 `latest_morning.md` / `latest_close.md`（不重新生成，秒回） |
| `/定调` | `python -m cn_brief run --slot morning`（重新生成盘前定调，约 10 秒） |

消息长度：Discord 单条上限 2000 字、Telegram 4096 字。完整汇报常在 2000–4000 字，**请按换行切分后分段发送**（`cn_brief.push.chunk(text, limit)` 可以直接用）。

---

## 4. JSON 结构（`run --json` / `latest_*.json`）

```jsonc
{
  "slot": "close",
  "title": "A股收盘后研报 & 数据回顾",
  "generated_at": "2026-10-19T16:53:32+09:00",
  "markdown": "## ...完整汇报...",
  "errors": [],                                   // 非空 = 部分来源失败
  "ai": {"summary": "- 要点1\n- 要点2", "status": "ok (Claude)"},
                                                  // status 也可能是 "AI 不可用 (Claude 与 Codex 均失败): ..."
  "reports": {
    "window": ["2026-10-19", "2026-10-19"],
    "stock": {
      "n": 7, "n_stocks": 6,
      "upgrades":       [ {"code","name","org","rating","last_rating","change","title","industry",
                           "eps_this_year","pe_this_year","target_price","date","link"} ],
      "downgrades":     [ ...同上... ],
      "first_coverage": [ ...同上，只含买入/增持... ],
      "hot":            [ {"code","name","n_reports","orgs":[...],"ratings":[[评级,次数]],"titles":[...],"industry","link"} ],
      "industries":     [["电池", 5], ...],
      "rating_mix":     [["买入", 4], ...]
    },
    "industry": {"n": 37, "industries": [{"industry","n","ratings","titles"}]},
    "macro": ["机构: 标题", ...]
  },
  "events": {
    "released": [ {"region","title","star","time_jst","formerVal","indicateVal","pubVal", ...} ],
    "upcoming": [ ...同上... ]                   // evening 时只有 upcoming
  }
}
```

- `change`：`调高` / `调低` / `首次` / `维持` / `无评级`（比较上次评级和本次评级得出）
- `indicateVal` = 预期值（有预期才有这个字段），`formerVal` = 前值，`pubVal` = 公布值（未公布时为 `"未公布"`）
- `star` = 百度的重要性星级（1–5）
- `link` = 东方财富研报详情页

### 4.1 盘前定调 `--slot morning` 的 JSON（另有字段）

```jsonc
{
  "headline": "10:00 有中国重大数据；外围指向低开 0.1–0.4%",   // 一句话定调，适合做消息标题
  "key_events": [ ...今天到明早 6:00 将公布的重大数据，结构同 events... ],
  "overnight": {
    "last_a_close": "2026-10-16",                               // 隔夜 = 这个 A 股收盘以来
    "items": [ {"ticker": "^GSPC", "name": "标普500", "symbol": "^GSPC", "level": 6000.1,
                "change": -0.0109, "kind": "pct", "as_of": "2026-10-16"} ]   // ^TNX 为 kind=diff（收益率差值）
  },
  "implied_open": {                                             // 只用回测通过的指标；没通过时为 null
    "low": -0.0039, "high": -0.0009, "hit_range": [0.65, 0.66],
    "parts": [ {"driver": "标普500", "implied_gap": -0.0026, "r": 0.386, "hit": 0.649, "n": 1256} ]
  },
  "risk": {
    "watchlist": ["600487"],                                    // 空 = 显示全市场前 5
    "downgrades": [ {"code","name","org","from","to","date"} ], // 近 7 天评级调低
    "crowded":    [ {"code","name","n_orgs","industry"} ],      // 近 7 天 ≥3 家机构扎堆覆盖
    "down_note": "样本外20日平均 -2.27%，跑赢比例 32%", "crowded_note": "..."
  },
  "special_days": ["今天是股指期货/期权交割日（...）"]
}
```

- **自选股**：在 `agents\cn_brief\watchlist.txt` 每行写一个 6 位代码（`#` 后为注释），风险名单就只看这些股票
- 盘前里所有"历史上…"的数字都来自预注册回测的结论文件（`signals\backtest_verdicts\overnight_ashare.json`、`ashare_econ_surprise.json`、`ashare_report_signals.json`）；没通过检验的不附统计
- 回测结论（2016–2026）：标普 / 纳指 / 中概（KWEB）的隔夜涨跌对 A 股**开盘跳空**有预测力（样本外相关 0.38–0.46，方向命中 65–66%），对开盘后的**日内走势没有**；美债收益率、美元指数、人民币汇率没有预测力

"重大数据"的筛选规则：中国、美国、日本三地；CPI / 非农 / GDP / PCE / 利率决议 / LPR / 社融 / 央行 等头部数据一律保留；其余关键指标要求 ≥2 星，或任意 ≥3 星；油气库存、国债竞拍、钻井数、能源进出口量等噪声剔除。

---

## 5. 定时汇报与推送（已上线）

由看门狗每 5 分钟执行一次 `tick`（白名单任务 `cn_brief_tick`）：

| 时间（JST） | 内容 | 日期 |
|---|---|---|
| 09:00 | 盘前定调：一句话 + 今日关键时刻 + 外围隔夜（推算开盘幅度）+ 风险名单 + 特殊日 | 工作日 |
| 16:30 | 收盘后：今日研报 + 今天已公布数据（公布值 vs 预期）+ 今晚预告 | 工作日 |
| 21:00 | 晚间：今晚到明早的中美日重大数据 | 工作日 |
| 公布前 30 分钟 | "⏰ 30 分钟后公布"：同一时刻的多条数据合并成一条 | 每天 |
| 公布后（2 小时内拿到公布值时） | "📊 重大数据已公布"：公布值 vs 预期，并标出高于 / 低于 / 符合预期 | 每天 |

- 每个时段一天只发一次；电脑睡眠或延迟时，到点后 40 分钟内会补发，超过就跳过
- 每条数据只提醒一次、只补发一次结果（记录在 `signals\cn_brief\state.json`）

### 推送渠道配置

写在 `agents\secrets.local.json`（或环境变量）。不配就只写文件、不推送：

| 键 | 用途 |
|---|---|
| `CN_BRIEF_WEBHOOK_URL` | **通用接口（给第三方机器人库用）**：每次汇报 / 提醒 / 结果都会 `POST` JSON：`{"slot": "morning\|close\|evening\|reminder\|result", "title": "...", "markdown": "...", "data": {...}}`。机器人在本机起一个 HTTP 接收端（如 `http://127.0.0.1:9000/cn_brief`）收到后转发即可 |
| `CN_BRIEF_DISCORD_WEBHOOK_URL` | 本模块专用的 Discord 频道（不配则用 `DISCORD_WEBHOOK_URL`） |
| `TELEGRAM_BOT_TOKEN` + `CN_BRIEF_TELEGRAM_CHAT_ID` | Telegram（chat id 不配则用 `TELEGRAM_CHAT_ID`） |
| `CN_BRIEF_NO_AI=1` | 环境变量：全局关闭 AI 摘要 |
| `CN_BRIEF_AI_COMPLEXITY` | AI 档位，默认 `simple`（Sonnet，标题整理属于简单抽取）；设 `medium` 用 Opus |

推送结果见 `signals\cn_brief\push_log.jsonl`（`sent` / `skipped` 两个字段）。

---

## 6. 注意事项

- AI 要点只依据研报**标题和评级**整理，不读正文。Claude 没额度时自动切 Codex；两边都不可用时如实写"AI 不可用"，其余内容照常输出
- 东方财富的研报白天陆续发布，上午调用时当天的篇数会偏少
- 以上全部内容仅供参考，不构成投资建议
