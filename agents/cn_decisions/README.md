# A 股确定性研究决策 P0

这是统计模型前的安全与一致性层，不是已通过回测的选股策略。当前没有获准的真实A股1/3/5交易日模型，实际输出为“证据不足／数据不足”，概率与期望收益为null；不能把历史均线/涨跌/政策利好当作未来胜率。

依赖：Python3.12、`pydantic==2.12.5`。不需要新API Key，不下载模型/行情；准备环境时按requirements安装，本轮机器已有该版本。数据与记录不提交Git。

在 `agents` 目录执行：

```powershell
python -m cn_decisions analyze --symbol 600000.SH --horizon 5
python -m cn_decisions analyze --symbol 600000.SH --horizon 1 --json
python -m cn_decisions show cn-p0-<run_id后缀>
python -m cn_decisions compare <旧run_id> <新run_id>
python -m unittest tests.test_cn_decisions tests.test_webui_cn_decisions -v
```

默认独立库：`agents/signals/cn_decisions/runs.sqlite3`，CLI的`--db`可用于临时验证目录，需置于子命令前。显式analyze才保存；HTTP `GET /api/bot/decision?symbol=600000.SH&horizon=5`只读已冻结记录，无记录返回NO_RECORD，不创建数据库、抓价或训练。

`--fixture 文件.json`强制标记mock，不能通过真实证据门；禁止用测试的合成校准指标宣传性能。`--asof`要求带offset的ISO时间，代表研究截止声明而非已拥有相应历史数据。内部时间归一化Asia/Shanghai；缺交易日历目标窗口就保留未验证，不猜节假日。

Direction与trade_action不同：合格模型方向偏多，但成本/风险/可成交性不合格时操作仍观望；本阶段连模型都没有时返回数据不足。P0仅有研究close-to-close标签，不将其扣费后冒充下一开盘可执行交易收益；即使调用方传executable=true也不能绕过。普通A股不发做空指令。没有全局有效独立样本数；原始OOS条数单列，不能冒充最近邻匹配数或独立样本量。

冻结记录有run_id、快照哈希、模型/规则/时点；SQLite禁止更新删除，同样数据/模型/配置却给出不同机器字段拒绝保存。固定中文报告不接受LLM自由结论；QQ接入P0还会拒绝任何非null的预测概率或正向交易意见，直到P1完成真实验证和模型登记。

未完成：真实历史数据许可/时点/证券主数据、1/3/5独立训练、walk-forward/purge/embargo、真实独立校准、实测费用/无法成交及OOS基准结果、到期自动结算与漂移。详见根目录IMPLEMENTATION_PLAN.md。CLI跑通和单元测试通过不等于完整统计MVP完成。
