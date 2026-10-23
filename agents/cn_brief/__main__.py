"""命令行入口 (在 agents 目录下运行): python -m cn_brief <命令>

  run --slot morning|close|evening|now [--no-push] [--no-ai] [--json]
        生成一份汇报; 默认打印 markdown 到标准输出并写 signals/cn_brief/latest_<slot>.md/.json
        --push 才会推送到已配置的渠道 (机器人自己转发时不用加)
  calendar [--days N] [--all] [--json]
        经济日历: 默认只列中美日重大数据, 时间为日本时间; --all 列全部
  tick [--dry-run]
        看门狗每 5 分钟调用: 到点汇报 + 公布前 30 分钟提醒 + 公布后补发实际值
退出码: 0 成功; 1 参数错误; 2 生成时出错 (仍会输出能拿到的部分, 错误写在 json 的 errors 里)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import brief as B  # noqa: E402

JST = timezone(timedelta(hours=9))


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=1, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cn_brief", description="A股研报 + 中美日重大经济数据")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--slot", default="now", choices=B.SLOTS)
    r.add_argument("--push", action="store_true", help="推送到已配置渠道")
    r.add_argument("--no-ai", action="store_true", help="不调用 AI 摘要 (更快, 不耗额度)")
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是 markdown")
    c = sub.add_parser("calendar")
    c.add_argument("--days", type=int, default=1)
    c.add_argument("--all", action="store_true")
    c.add_argument("--json", action="store_true")
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        b = B.build_brief(a.slot, use_ai=not a.no_ai)
        if a.push:
            from .push import push
            b["push"] = push(b["title"], b["markdown"], slot=a.slot)
        print(_dump(b) if a.json else b["markdown"])
        return 2 if b["errors"] else 0
    if a.cmd == "calendar":
        from . import sources
        today_bjt = datetime.now(JST).astimezone(B.BJT).date()
        rows = sources.calendar_range(today_bjt, max(1, a.days))
        errs = [x["_error"] for x in rows if "_error" in x]
        rows = [x for x in rows if "_error" not in x and (a.all or B.is_important(x))]
        rows.sort(key=lambda e: e.get("time_jst") or datetime.max.replace(tzinfo=JST))
        if a.json:
            print(_dump({"events": rows, "errors": errs}))
        else:
            print("\n".join(B.fmt_event(x) for x in rows) or "（没有符合条件的数据）")
            for e in errs:
                print(f"[错误] {e}", file=sys.stderr)
        return 2 if errs else 0
    if a.cmd == "tick":
        from .tick import run_tick
        print(_dump(run_tick(dry_run=a.dry_run)))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
