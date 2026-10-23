"""A股每日研报调查 + 中美日重大经济数据汇报/提醒 (2026-10-19).

设计: 生成 (sources/brief) 与推送 (push) 分离, 方便以后接任意机器人库.
  - Python:  from cn_brief import build_brief; build_brief("morning") -> dict (含 markdown)
  - 命令行:  python -m cn_brief run --slot morning|close|evening [--no-push] [--json]
             python -m cn_brief calendar [--days 1]      # 只看经济日历
             python -m cn_brief tick                     # 看门狗每 5 分钟调用: 到点汇报 + 公布前 30 分钟提醒
  - 文件:    signals/cn_brief/latest_<slot>.md / .json   # 机器人可以直接读
只读: 不下单, 不改交易系统任何状态.
"""
from .brief import build_brief  # noqa: F401
