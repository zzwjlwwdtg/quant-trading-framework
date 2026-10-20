# 久留美公共单股查询 v1

2026-10-20，Product: Kurumi Stock Bot。基线6ac5b464d，不改变模拟仓决策/交易。GET /api/bot/stock?ticker=600519.SS，目录class=read。沪深常见六位/.SH/.SS/.SZ、美股/US./BRK.B；未知交易所、混合代码、路径或URL拒绝。

新增public_stock.py，复用yfinance1.2.0与backtest_engine.add_indicators，无新依赖/自研行情协议。2任务、32代码缓存，正常60秒/失败30秒冷却，初次PENDING、满BUSY；读取会有界启动公共网络请求，并非纯磁盘缓存。完成任务跨代码回收，防止两代码后队列永久被占。无AI、OpenD、账户、交易、审批，无任何信号/仓位/推送游标写入；复用纯指标可能导入config，但任何凭据不进入接口。

6mo原始日线由库复权，1d常规分钟K。quote为最近已结束分钟Close，as_of为分钟结束时刻，可能延迟；quote.volume是当日取得分钟K累计股数，可能缺分钟，不是全天总量/成交额/手数。百分比相对前交易日原始Close；已知分红/拆股日弃判。

方法KURUMI_STOCK_DAILY_V1：CN15:15/US16:15当地时间前排除当日K，纽约自动DST，不以工作日逻辑冒充交易所日历。至少60完整日线，异常/未来/重复/无时区/7日以上旧数据弃判。最近60根有Repaired?时量能/背离/候选弃判。

日线量能=最后完整日Volume/此前20完整日均量，不含当前；≥1.5放量、<0.8缩量，不是盘中量比或资金流入。MACD复用EMA12/26/9，RSI如展示为SMA14。顶背离为60根High左右各2根严格唯一峰、峰距≥5、后峰距当前≤10、高价抬升≥0.5%、DIF降低超过前值绝对值5%（至少1e−8），右2根完成后确认。观察同时要求Close>MA20>MA50、DIF>DEA、突破此前20日High、量能≥1.5、无背离/重建。WATCH_ONLY不是订单、盈利概率或已验证收益；未检出不等于安全。

单股13+gateway8+market_outlook9项通过，是公式/权限/失败/缓存合成测试，不是盈利回测。AAPL/600519真实免费抓价成功，但时间各异，不冒称实时；没有QQ/量化AI/交易调用。部署后HTTP与机器人验收须分别记录。后台新增公共查询不扩大群白名单，原三人小群测试、大群静默/广播暂停继续。

参考：https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html

## 生产验收

功能b0eb8ebb9无快进合并main dc41f2fad，仅核验原PID后重载WebUI245552→246968，不运行含其他自动任务的watchdog，不动OpenD/交易。实际HTTP目录read，FsiQuantReadClient严格契约验证600519/AAPL的真实快照及渲染，错误交易所HTTP400。D盘久留美五个实际命令处理器+生产小群配置随后全部离线执行成功，不发QQ/不调模型/不启动私人runtime。报价显示日本时间、保留原市场交易日。

原AI Planner198168/Scheduler19544未动；久留美仅自身247656→241296重连。四份配置/角色/推送SHA不变，广播端口仍停。F盘原signals/logs等脏改动与工作树原ai_calls.jsonl保留，不纳入提交。未push；真实QQ客户端与后台按钮未代验收。
