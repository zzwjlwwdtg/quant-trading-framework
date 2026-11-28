# 2026-10-26：周期量化任务无窗口启动修复

执行者：Codex。用户明确授权只修复 F 盘量化项目的启动方式并验证，不改交易、研报、广播逻辑，不重启 OpenD 或机器人。

## 原因与范围

- `webui-watchdog` 计划任务本身已使用 `pythonw.exe`，每 5 分钟运行。
- 19:48 采样抓到 CMD PID 364408，目标 `_cn_brief_tick.bat`；看门狗同一轮 `job_started` 日志的 PID 一致。
- `_webui_watchdog.py` 的批处理任务及 WebUI 恢复入口将 `DETACHED_PROCESS`、`CREATE_NEW_PROCESS_GROUP`、`CREATE_NO_WINDOW` 混用。[微软文档](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)明确说明 `CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 同用时会被忽略。这是本次确认并修复的一条高频弹窗路径，不等于排除了电脑上其他程序的弹窗。
- 原秘书看门狗已经是 pythonw→PowerShell `CREATE_NO_WINDOW`，没有改它。未添加 VBS、未降低任务频率、未停用任何功能。

## 修改

- `agents/_webui_watchdog.py`：后台启动参数统一为 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（Windows）；删除冲突的 `DETACHED_PROCESS`，批处理标准输入指向 DEVNULL。原有两个直接 Python worker 复用同一常量，等值替换。
- 所有既有命令、参数、工作目录、环境覆盖、白名单、触发频率、去重、日志、健康检查和恢复条件不变。
- `agents/tests/test_watchdog_windowless.py`：遍历全部批处理白名单的参数回归；WebUI 恢复环境保持；直接 Python worker 不使用 Shell；真实 Windows pythonw→临时 CMD→Python 验收。临时子进程在启动器退出后仍完成，自己的控制台没有可见窗口；含空格及中文目录。

## 验证与部署

- 工作区私有原件备份：`C:/Users/masa/Documents/ChatGPT/personal plan/tmp/console-flash-20261026/original/_webui_watchdog.py`，原始 SHA256 `7AEB2F52D1E519936B33A4971C8E5BF625AAE1B1AA46C0675495A10F2EF8DF9D`。部署前比较哈希，防止覆盖并发修改。
- 首次隔离测试被沙箱临时目录权限阻止，获准在正常本机环境重跑后 4/4 通过，没有弱化测试断言。
- 生产目录隔离回归：`python -B -m unittest tests.test_watchdog_job_requests tests.test_webui_restart_request tests.test_watchdog_windowless tests.test_zz_live_files_sentinel -v`，22/22 通过，生产执行资料内容/修改时间哨兵通过。测试仅运行临时模拟批处理，不手动触发真实研报、交易或推送。
- 19:52:55 部署；19:53:01 自然计划轮次已用新源码，日志显示 bot_feed_tick、cn_brief_tick、market_radar_tick 启动成功、healthy，计划任务返回 0。没有额外手动执行完整看门狗。
- 部署前后监听 PID 一致：WebUI 8080=202528；Scheduler 8765=19544；OpenD 11111=41200；久留美 18792=390176；原秘书 18991=198168。WebUI 与两个机器人健康接口均 HTTP 200。
- 新的测试和本文留在工作区供其他 AI 审阅；本轮未提交或推送，未改其他已有工作区变动。未保存凭据、账户、持仓、聊天或运行日志到提交文件。

## 后续边界

这次证明修正后的启动参数和实际子进程无可见窗口，且自然调度未受影响；没有宣称已观察所有计划轮次或排除其他应用。若仍出现前台窗口，应继续按发生时间和父进程追踪，而不是批量杀 CMD、关闭广播或停用安全软件。回退时先核对目标文件未被其他 AI 修改，再按明确补丁撤销；不要覆盖其他人的后续工作。
