# 2026-10-21：A 股统计优先研究 P0

Agent: Codex。Product: Kurumi Stock Bot。基线main2d83234a0，分支agent-2026-10-21-quant-p0；既有美股模拟盘、交易orchestrator、审批、凭据、游标不改。

用户提供《A股量化决策AI 开发规格 v1.0》，认为之前测试矛盾。先写根目录IMPLEMENTATION_PLAN.md，再用已有Pydantic2.12.5、sqlite3/unittest实现cn_decisions强类型规则、时点/独立校准证据门、冻结查询比较、中文/JSON CLI及只读GET。QQ仓同步读取固定机器模板，不让模型改方向。

海外旧逻辑从过旧A股锚点跨两日累计，可能反转美债变化；改为同源最近两个已结束场次，标注日期和代理，不以CNY充CNH。旧开盘系数不兼容新基准，也非本规格1/3/5模型：停止输出；旧缓存不可回送旧概率。经济日历没有覆盖到事件不等于安静日。现有美国ETF/SPY的前向假设也不是A股模型。

没有已审核的真实时点A股训练集/主数据、退市覆盖和独立校准模型，本轮不造概率或回测收益。默认结果证据不足/数据不足；fixture强制mock，纯单元阈值测试不是业绩。P0仅close-to-close研究，净交易收益null；原始OOS样本数与有效独立样本/匹配数量不混用。P1真实标签、Logistic/候选LightGBM、走步/校准/费用基准仍待完成。

## 回归与已知问题

受影响模块51项通过，覆盖强类型/阈值/费用/不可成交/时间泄漏/校准跨界/冻结/修订/缓存旧结果/只读GET/晨报/市场接口。CLI实际输出与QQ Zod跨语言校验通过。修日文CP932下中文输出UnicodeEncodeError，标准流固定UTF8并加测试。

全量在两个干净源导出比较：基线1046项，修改1062项，均只有下述8个ERROR，改动没有新增失败；后续小补丁相关51项重新通过。不声称全量测试通过，不借本任务去改交易和发布模块：

- tests.test_growth_eps.ManualFileFallback.test_uses_placed_file_when_download_fails
- tests.test_growth_eps.SystemRouteFallback.test_falls_back_to_system_route_on_windows
- tests.test_opend_watchdog.MainFlowTests.test_launch_failure_returns_4
- tests.test_opend_watchdog.MainFlowTests.test_launched_but_port_never_bound_returns_5
- tests.test_opend_watchdog.MainFlowTests.test_process_alive_but_port_dead_waits_no_relaunch
- tests.test_opend_watchdog.MainFlowTests.test_process_dead_triggers_launch_and_waits_for_port
- tests.test_publish_gh_pages.PublishGhPages.test_restore_from_gh_pages_when_new_payload_is_empty
- tests.test_webui_single_instance.ExclusiveServer.test_main_exits_hard_when_port_taken

测试须用tests包或discover带-t .，使既有tests/__init__.py隔离运行文件；不能在F盘生产根直接跑会改交易文件的脚本。生产已有运行态modified/untracked全部保留，C盘工作区原有ai_calls和测试生成signals不入提交。无发送QQ、下单或改日历。

后续先核实源许可/时点/交易日历/证券状态，再实施P1。冻结研究库在忽略的agents/signals/cn_decisions，不与账户或原个人秘书库合并；GET无记录返回NO_RECORD，不暗中抓数/训练。源码本地合并与服务加载分别核验，未获本次推送指示不push。

## 最终环境对照

八个错误全部为CP932打印中文的UnicodeEncodeError，并非测试断言失败。对两个隔离源码导出统一设置`PYTHONIOENCODING=utf-8`和Python`-X utf8`，最终干净基线1046项、改版1063项全部OK；测试没有放松，交易/发布源码未改。前述错误清单保留作为环境排错证据，不再是待修产品缺陷。相关51项亦通过。
